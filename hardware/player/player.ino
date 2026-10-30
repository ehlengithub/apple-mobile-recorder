// Universal AMR2 player, based on ESP32 iPhone Agent by Henry Yuan (MIT).
// UART0 management and one scheduler/store; compile AMR_BLE=1 for classic ESP32.
#include <Arduino.h>
#include <Preferences.h>
#include "esp_mac.h"
#include "esp_system.h"
#include "player_format.h"
#include "player_store.h"
#include "player_protocol.h"
#include "player_hid.h"
#include "player_control.h"
#if __has_include("replay_engine.h")
#include "replay_engine.h" // integration copies the original engine into sketch
#else
#include "../offline_replay/replay_engine.h"
#endif
#if __has_include("secrets.h")
#include "secrets.h"
#include <WiFi.h>
#define AMR_HAS_WIFI 1
#else
#define AMR_HAS_WIFI 0
#endif

PlayerHid hid;
amr::AccessGate accessGate{AMR_BLE != 0, true, 0};
struct PreferencesBackend {
  Preferences prefs;
  bool ready = false;
  bool blocked() { return accessGate.blocked(hid.mounted(), hid.epoch()); }
  bool maintenance() { return prefs.getBool("maintenance", true); }
  bool writeMaintenance(bool value) {
    return prefs.putBool("maintenance", value) == 1 && prefs.getBool("maintenance", !value) == value;
  }
  int active() { return prefs.getUChar("active", 255); }
  bool armed() { return prefs.getBool("armed", false); }
  bool desktop() { return prefs.getBool("desktop", false); }
  const char *key(int slot) { return slot ? "slot1" : "slot0"; }
  size_t read(int slot, uint8_t *p, size_t cap) {
    const size_t n = prefs.getBytesLength(key(slot));
    if (!n || n > cap) return n;
    return prefs.getBytes(key(slot), p, cap) == n ? n : 0;
  }
  bool writeArmed(bool value) {
    return prefs.putBool("armed", value) == 1 && prefs.getBool("armed", !value) == value;
  }
  bool writeDesktop(bool value) {
    return prefs.putBool("desktop", value) == 1 && prefs.getBool("desktop", !value) == value;
  }
  bool write(int slot, const uint8_t *p, size_t n) { return prefs.putBytes(key(slot), p, n) == n; }
  bool activate(int slot) { return prefs.putUChar("active", uint8_t(slot)) == 1; }
} backend;
amr::Store<PreferencesBackend> recording(backend);
amr::Maintenance<PreferencesBackend> maintenance(backend, accessGate);
amr::Upload upload;
amr::Line serialLine;
ReplayStep replaySteps[amr::kMaxSteps];
ReplayEngine replay(replaySteps, 0, true);
bool resetHold = true, stopLatched = false, needsRelease = true;
bool wasSuspended = false, wasReady = false;
uint32_t sessionEpoch = 0;
amr::BootButton bootButton;
char deviceMac[18] = {};

#if AMR_HAS_WIFI
WiFiClient bridge;
amr::Line bridgeLine;
bool wifiStarted = false, bridgeWasConnected = false;
uint32_t nextConnect = 0;
#endif

void replyError(const char *error) { Serial0.printf("AMR2 {\"ok\":false,\"error\":\"%s\"}\n", error); }
void replyOK() { Serial0.println("AMR2 {\"ok\":true}"); }
void crcText(char *out) {
  if (recording.size) snprintf(out, 9, "%08lx", (unsigned long)amr::u32(recording.bytes + recording.size - 4));
  else out[0] = '\0';
}
void info() {
  char crc[9]; crcText(crc);
  char extra[180] = {};
#if AMR_BLE
  snprintf(extra, sizeof(extra), ",\"maintenance\":%s,\"maintenance_saved\":%s,\"connected\":%s,\"encrypted\":%s,\"subscribed\":%s",
           accessGate.maintenance ? "true" : "false", maintenance.durable ? "true" : "false",
           hid.mounted() ? "true" : "false", hid.encrypted() ? "true" : "false", hid.subscribed() ? "true" : "false");
#endif
  Serial0.printf("AMR2 {\"ok\":true,\"protocol\":2,\"transport\":\"%s\",\"chip\":\"%s\",\"mac\":\"%s\",\"mounted\":%s,\"suspended\":%s,"
                 "\"mode\":\"%s\",\"armed\":%s,\"size\":%u,\"crc\":\"%s\",\"hold\":%s,\"desktop_available\":%s%s}\n",
                 hid.transport(), hid.chip(), deviceMac, hid.mounted() ? "true" : "false", hid.suspended() ? "true" : "false",
                 recording.desktop ? "desktop" : "offline", recording.armed ? "true" : "false",
                 unsigned(recording.size), crc, resetHold || stopLatched ? "true" : "false",
                 AMR_HAS_WIFI ? "true" : "false", extra);
}

// On detach/suspend a release cannot reach the host. Retain the release debt
// and send BOTH zero reports before allowing any subsequent action.
bool releaseAll() {
  needsRelease = true;
  if (!recording.desktop && !recording.size) return false;
  if (!hid.ready()) return false;
  if (hid.releaseAll()) needsRelease = false;
  else { replay.stop(); stopLatched = true; }
  return !needsRelease;
}
void halt() {
  replay.stop(); stopLatched = true; needsRelease = true;
  releaseAll(); // checked; if unavailable, release debt remains queued
}
void loadReplay() {
  for (size_t i = 0; i < recording.header.count && recording.size; ++i) {
    const amr::Step s = amr::step(recording.bytes, i);
    replaySteps[i] = {s.x, s.y, s.wait};
  }
  ReplayTiming timing;
  timing.minimumIntervalMs = 500; timing.minimumReleaseMs = recording.header.release;
  timing.startupMs = recording.header.startup; timing.loopLimit = recording.header.loops;
  replay = ReplayEngine(replaySteps, recording.size ? recording.header.count : 0, true, timing);
}
void networkOff() {
#if AMR_HAS_WIFI
  bridge.stop(); bridgeWasConnected = false; bridgeLine = amr::Line{};
  if (wifiStarted) { WiFi.disconnect(true); WiFi.mode(WIFI_OFF); wifiStarted = false; }
#endif
}

const char *enterMaintenance() {
  upload.abort(); halt(); networkOff(); resetHold = true;
#if AMR_BLE
  hid.disable();
  return maintenance.enter(recording);
#else
  return recording.disarm() ? nullptr : "disarm-store";
#endif
}

const char *enablePlayback(bool desktop) {
#if AMR_BLE
  const char *error = maintenance.enable(recording, desktop);
  if (error) return error;
#else
  const char *error = desktop ? recording.setDesktop() : recording.arm();
  if (error) return error;
#endif
  loadReplay(); stopLatched = false; resetHold = false; needsRelease = true;
  if (!hid.enable()) { enterMaintenance(); return "advertising-failed"; }
  return nullptr;
}

void serialRequest(char *line) {
  amr::Request r;
  if (const char *e = amr::parse(line, r)) { upload.abort(); replyError(e); return; }
  upload.expire(millis());
  if (upload.live && (hid.mounted() || upload.epoch != hid.epoch())) upload.abort();
  accessGate.expectedEpoch = hid.epoch();
  const bool mutating = r.op == amr::Op::Begin || r.op == amr::Op::Data || r.op == amr::Op::Commit ||
                        r.op == amr::Op::Arm || r.op == amr::Op::Desktop;
  if (mutating && AMR_BLE && (!accessGate.maintenance || !maintenance.durable)) {
    upload.abort(); replyError("maintenance-required"); return;
  }
  if (mutating && backend.blocked()) { upload.abort(); replyError("hid-connected"); return; }
  switch (r.op) {
    case amr::Op::Info: info(); return;
    case amr::Op::Read: {
      if (!recording.size || r.offset > recording.size || r.length > recording.size - r.offset) {
        replyError("read-range"); return;
      }
      char hex[amr::kChunkSize * 2 + 1];
      const char digits[] = "0123456789abcdef";
      for (size_t i = 0; i < r.length; ++i) {
        const uint8_t b = recording.bytes[r.offset + i];
        hex[i*2] = digits[b >> 4]; hex[i*2+1] = digits[b & 15];
      }
      hex[r.length * 2] = '\0';
      Serial0.printf("AMR2 {\"ok\":true,\"offset\":%lu,\"data\":\"%s\"}\n", (unsigned long)r.offset, hex);
      return;
    }
    case amr::Op::Begin:
      upload.abort(); replay.stop();
      if (!recording.disarm()) { replyError("disarm-store"); return; }
      if (backend.blocked()) { replyError("hid-connected"); return; }
      upload.begin(r, millis(), accessGate.expectedEpoch);
      Serial0.println("AMR2 {\"ok\":true,\"offset\":0}"); return;
    case amr::Op::Data:
      if (const char *e = upload.append(r, millis())) { replyError(e); return; }
      if (backend.blocked()) { upload.abort(); replyError("hid-connected"); return; }
      Serial0.printf("AMR2 {\"ok\":true,\"offset\":%u}\n", unsigned(upload.offset)); return;
    case amr::Op::Commit: {
      const char *e = upload.check(millis());
      if (!e) e = recording.commit(upload.bytes, upload.size);
      upload.abort();
      if (e) { replyError(e); return; }
      networkOff(); loadReplay(); resetHold = true;
      char crc[9]; crcText(crc);
      Serial0.printf("AMR2 {\"ok\":true,\"size\":%u,\"crc\":\"%s\",\"armed\":false}\n",
                     unsigned(recording.size), crc); return;
    }
    case amr::Op::Arm:
      if (upload.live) { replyError("upload-active"); return; }
      if (const char *e = enablePlayback(false)) { replyError(e); return; }
      Serial0.println("AMR2 {\"ok\":true,\"armed\":true}"); return;
    case amr::Op::Desktop:
#if !AMR_HAS_WIFI
      replyError("desktop-unavailable-no-secrets"); return;
#endif
      upload.abort(); replay.stop();
      if (const char *e = enablePlayback(true)) { replyError(e); return; }
      Serial0.println("AMR2 {\"ok\":true,\"mode\":\"desktop\"}"); return;
    case amr::Op::Stop:
#if AMR_BLE
      if (const char *e = enterMaintenance()) { replyError(e); return; }
      Serial0.println("AMR2 {\"ok\":true,\"maintenance\":true,\"armed\":false}"); return;
#else
      upload.abort(); halt();
      if (!recording.disarm()) { replyError("disarm-store"); return; }
      replyOK(); return;
#endif
    case amr::Op::Maintenance:
      if (const char *e = enterMaintenance()) { replyError(e); return; }
      Serial0.println("AMR2 {\"ok\":true,\"maintenance\":true,\"armed\":false}"); return;
    case amr::Op::Abort: upload.abort(); replyOK(); return;
  }
}

// TCP bridge deliberately shares the old hello/commands, not the UART AMR2 API.
#if AMR_HAS_WIFI
bool bridgeSafe() {
  return recording.desktop && !stopLatched && !needsRelease && digitalRead(0) != LOW &&
         WiFi.status() == WL_CONNECTED && bridge.connected() && hid.ready();
}
bool safePause(uint32_t ms) {
  const uint32_t start = millis();
  while (uint32_t(millis() - start) < ms) { if (!bridgeSafe()) return false; delay(1); }
  return bridgeSafe();
}
#if !AMR_BLE
uint8_t keyCode(const String &k) {
  if (k == "enter") return KEY_RETURN;
  if (k == "esc") return KEY_ESC;
  if (k == "tab") return KEY_TAB;
  if (k == "backspace") return KEY_BACKSPACE;
  if (k == "space") return ' ';
  if (k == "up") return KEY_UP_ARROW;
  if (k == "down") return KEY_DOWN_ARROW;
  if (k == "left") return KEY_LEFT_ARROW;
  if (k == "right") return KEY_RIGHT_ARROW;
  if (k == "cmd") return KEY_LEFT_GUI;
  if (k == "ctrl") return KEY_LEFT_CTRL;
  if (k == "alt") return KEY_LEFT_ALT;
  if (k == "shift") return KEY_LEFT_SHIFT;
  return k.length() == 1 && uint8_t(k[0]) < 128 ? uint8_t(k[0]) : 0;
}
#endif
bool signedNumber(const String &s, int &out) {
  const bool negative = s.startsWith("-");
  uint32_t n;
  if (!amr::number(s.c_str() + (negative ? 1 : 0), 127, n)) return false;
  out = negative ? -int(n) : int(n); return true;
}
String bridgeFailure() { halt(); return "err HID stopped or disconnected"; }
String runBridge(const String &line) {
  if (line == "ping") return "ok";
#if AMR_BLE
  if (line == "key" || line.startsWith("key ") || line == "type" || line.startsWith("type "))
    return "err keyboard unsupported on ble";
#endif
  if (!bridgeSafe()) return "err HID held or asleep";
  if (line == "home") return hid.place(0, 0) ? "ok" : bridgeFailure();
  if (line == "press") return hid.down() ? "ok" : bridgeFailure();
  if (line == "release") return releaseAll() ? "ok" : bridgeFailure();
  if (line == "click") {
    if (!hid.down() || !safePause(60)) return bridgeFailure();
    return hid.up() ? "ok" : bridgeFailure();
  }
  const int split = line.indexOf(' ');
  if (split < 0) return "err unknown command";
  const String cmd = line.substring(0, split), arg = line.substring(split + 1);
  if (cmd == "abs" || cmd == "scroll") {
    const int space = arg.indexOf(' ');
    if (space < 1 || space == int(arg.length())-1 || arg.indexOf(' ', space + 1) >= 0) return "err arguments";
    const String a = arg.substring(0, space), b = arg.substring(space + 1);
    if (cmd == "abs") {
      uint32_t x, y;
      if (!amr::number(a.c_str(), 32767, x) || !amr::number(b.c_str(), 32767, y)) return "err coordinates";
      return hid.place(x, y) ? "ok" : bridgeFailure();
    }
    int v, h;
    if (!signedNumber(a, v) || !signedNumber(b, h)) return "err scroll range";
    return hid.scroll(v, h) ? "ok" : bridgeFailure();
  }
#if AMR_BLE
  if (cmd == "key" || cmd == "type") return "err keyboard unsupported on ble";
#else
  if (cmd == "key") {
    KeyReport report = {};
    size_t at = 0;
    while (at < arg.length()) {
      const int end = arg.indexOf('+', at);
      const String token = end < 0 ? arg.substring(at) : arg.substring(at, end);
      const uint8_t code = keyCode(token);
      if (!code || !CheckedKeyboard::add(report, code)) return "err unknown key";
      if (end < 0) { at = arg.length(); break; }
      at = end + 1;
      if (at == arg.length()) return "err key arguments";
    }
    if (!at) return "err key arguments";
    if (!hid.keyboardReport(report) || !safePause(30)) return bridgeFailure();
    return hid.keyboardUp() ? "ok" : bridgeFailure();
  }
  if (cmd == "type") {
    if (!arg.length()) return "err empty text";
    for (size_t i = 0; i < arg.length(); ++i) {
      KeyReport check = {};
      if (uint8_t(arg[i]) >= 128 || !CheckedKeyboard::add(check, uint8_t(arg[i]))) return "err ASCII only";
    }
    for (size_t i = 0; i < arg.length(); ++i) {
      KeyReport value = {}; CheckedKeyboard::add(value, uint8_t(arg[i]));
      if (!bridgeSafe() || !hid.keyboardReport(value) || !bridgeSafe() || !hid.keyboardUp()) return bridgeFailure();
    }
    return "ok";
  }
#endif
  return "err unknown command";
}
const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power-on";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_TASK_WDT: case ESP_RST_INT_WDT: case ESP_RST_WDT: return "watchdog";
    case ESP_RST_PANIC: return "crash";
    case ESP_RST_SW: return "software";
    case ESP_RST_USB: return "usb";
    default: return "other";
  }
}
void serviceBridge(uint32_t now) {
  if (!recording.desktop || (AMR_BLE && accessGate.maintenance)) { if (wifiStarted) networkOff(); return; }
  if (!wifiStarted) {
    WiFi.mode(WIFI_STA); WiFi.setHostname("amr-player"); WiFi.setSleep(false);
    WiFi.setAutoReconnect(true); WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifiStarted = true; nextConnect = now;
  }
  const bool connected = WiFi.status() == WL_CONNECTED && bridge.connected();
  if (bridgeWasConnected && !connected) { releaseAll(); bridgeLine = amr::Line{}; bridge.stop(); }
  bridgeWasConnected = connected;
  if (WiFi.status() != WL_CONNECTED) return;
  if (!connected) {
    if (!ReplayEngine::due(now, nextConnect)) return;
    nextConnect = now + 1000;
    if (!bridge.connect(SERVER_HOST, SERVER_PORT, 100)) return;
    bridge.setNoDelay(true); bridgeWasConnected = true; bridgeLine = amr::Line{};
    releaseAll();
    bridge.printf("hello %s %lu absolute %s\n", resetReason(), (unsigned long)millis(), hid.transport());
  }
  // Bounded incremental reads; never block on a partial TCP line.
  for (unsigned i = 0; i < 128 && bridge.available(); ++i) {
    const auto result = bridgeLine.push(char(bridge.read()));
    if (result == amr::Line::Rejected) { bridge.println("err line too long or invalid"); halt(); }
    else if (result == amr::Line::Complete) bridge.println(runBridge(String(bridgeLine.text)));
  }
}
#else
void serviceBridge(uint32_t) {} // no credentials: no Wi-Fi dependency or attempts
#endif

void setup() {
  Serial0.begin(115200); pinMode(0, INPUT_PULLUP);
  backend.ready = backend.prefs.begin("amr-player", false);
  recording.load(); loadReplay();
  const auto reason = esp_reset_reason();
  resetHold = reason != ESP_RST_POWERON && reason != ESP_RST_EXT;
#if AMR_BLE
  maintenance.load(recording);
  if (resetHold || (!AMR_HAS_WIFI && recording.desktop)) maintenance.enter(recording);
#endif
  uint8_t mac[6] = {};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(deviceMac, sizeof(deviceMac), "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  if (!hid.begin(!AMR_BLE || (!accessGate.maintenance && maintenance.durable))) enterMaintenance();
  sessionEpoch = hid.sessionEpoch();
  // No unsolicited AMR2 response, credentials, commands or recording logs.
}

void loop() {
  const uint32_t now = millis();
  const uint32_t epoch = hid.sessionEpoch();
  if (epoch != sessionEpoch) {
    sessionEpoch = epoch; loadReplay(); needsRelease = true; stopLatched = false; wasReady = false;
  }
  const bool ready = hid.ready(), suspended = hid.suspended();
  if ((wasReady && !ready) || (suspended && !wasSuspended)) halt();
  wasReady = ready; wasSuspended = suspended;
  if (upload.live && (hid.mounted() || upload.epoch != hid.epoch())) upload.abort();
  upload.expire(now);
  const auto button = bootButton.tick(now, digitalRead(0) == LOW, AMR_BLE != 0);
  if (button == amr::BootButton::Stop || (bootButton.stopHeld() && !stopLatched)) halt();
  if (button == amr::BootButton::EnterMaintenance) enterMaintenance();
  for (unsigned i = 0; i < 128 && Serial0.available(); ++i) {
    const auto result = serialLine.push(char(Serial0.read()));
    if (result == amr::Line::Rejected) { upload.abort(); replyError("line-too-long-or-invalid"); }
    else if (result == amr::Line::Complete) serialRequest(serialLine.text);
  }
  // No recording in offline mode means ZERO HID reports, including zero reports.
  const bool hidEnabled = recording.desktop || recording.size;
  if (hidEnabled && hid.ready() && needsRelease) {
    if (!releaseAll()) halt();
  }
  if (!hid.ready()) needsRelease = true;
  replay.inhibited = resetHold || stopLatched || !recording.armed || recording.desktop || needsRelease ||
                     (AMR_BLE && accessGate.maintenance);
  const auto action = replay.tick(millis(), hid.linkReady(), hid.suspended(), stopLatched);
  if (hidEnabled && hid.ready() && !needsRelease) {
    bool ok = true;
    if (action == ReplayAction::Move) ok = hid.place(replaySteps[replay.index].x, replaySteps[replay.index].y);
    else if (action == ReplayAction::Press) ok = hid.down();
    else if (action == ReplayAction::Release) ok = hid.up();
    if (!ok) halt();
  }
  serviceBridge(millis());
  delay(1);
}
