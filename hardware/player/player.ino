// Universal AMR2 player, based on ESP32 iPhone Agent by Henry Yuan (MIT).
// UART0 is the management port; native USB is one absolute mouse + keyboard.
#include <Arduino.h>
#include <Preferences.h>
#include <atomic>
#include "USB.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "player_format.h"
#include "player_store.h"
#include "player_protocol.h"
#include "player_hid.h"
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

std::atomic<uint32_t> mountEpoch{0};
uint32_t writeEpoch = 0;
struct PreferencesBackend {
  Preferences prefs;
  bool ready = false;
  bool blocked() { return tud_mounted() || mountEpoch.load() != writeEpoch; }
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
amr::Upload upload;
amr::Line serialLine;
CheckedAbsoluteMouse pointer;
CheckedKeyboard keyboard;
ReplayStep replaySteps[amr::kMaxSteps];
ReplayEngine replay(replaySteps, 0, true);
bool resetHold = true, stopLatched = false, needsRelease = true;
bool wasMounted = false, wasSuspended = false, buttonDown = false;
uint32_t buttonSince = 0;
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
  Serial0.printf("AMR2 {\"ok\":true,\"protocol\":2,\"mac\":\"%s\",\"mounted\":%s,\"suspended\":%s,"
                 "\"mode\":\"%s\",\"armed\":%s,\"size\":%u,\"crc\":\"%s\",\"hold\":%s,\"desktop_available\":%s}\n",
                 deviceMac, tud_mounted() ? "true" : "false", tud_suspended() ? "true" : "false",
                 recording.desktop ? "desktop" : "offline", recording.armed ? "true" : "false",
                 unsigned(recording.size), crc, resetHold || stopLatched ? "true" : "false",
                 AMR_HAS_WIFI ? "true" : "false");
}

// On detach/suspend a release cannot reach the host. Retain the release debt
// and send BOTH zero reports before allowing any subsequent action.
bool releaseAll() {
  needsRelease = true;
  if (!recording.desktop && !recording.size) return false;
  if (!pointer.ready() || !keyboard.ready()) return false;
  const bool mouseOK = pointer.up();
  const bool keyboardOK = keyboard.up();
  if (mouseOK && keyboardOK) needsRelease = false;
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

void serialRequest(char *line) {
  amr::Request r;
  if (const char *e = amr::parse(line, r)) { upload.abort(); replyError(e); return; }
  upload.expire(millis());
  if (upload.live && (tud_mounted() || upload.epoch != mountEpoch.load())) upload.abort();
  writeEpoch = mountEpoch.load();
  const bool mutating = r.op == amr::Op::Begin || r.op == amr::Op::Data || r.op == amr::Op::Commit ||
                        r.op == amr::Op::Arm || r.op == amr::Op::Desktop;
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
      upload.begin(r, millis(), writeEpoch);
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
      if (const char *e = recording.arm()) { replyError(e); return; }
      resetHold = false; stopLatched = false; loadReplay();
      Serial0.println("AMR2 {\"ok\":true,\"armed\":true}"); return;
    case amr::Op::Desktop:
#if !AMR_HAS_WIFI
      replyError("desktop-unavailable-no-secrets"); return;
#endif
      upload.abort(); replay.stop();
      if (const char *e = recording.setDesktop()) { replyError(e); return; }
      stopLatched = false; needsRelease = true;
      Serial0.println("AMR2 {\"ok\":true,\"mode\":\"desktop\"}"); return;
    case amr::Op::Stop:
      upload.abort(); halt();
      if (!recording.disarm()) { replyError("disarm-store"); return; }
      replyOK(); return;
    case amr::Op::Abort: upload.abort(); replyOK(); return;
  }
}

// TCP bridge deliberately shares the old hello/commands, not the UART AMR2 API.
#if AMR_HAS_WIFI
bool bridgeSafe() {
  return recording.desktop && !stopLatched && !needsRelease && digitalRead(0) != LOW &&
         WiFi.status() == WL_CONNECTED && bridge.connected() && tud_mounted() && !tud_suspended();
}
bool safePause(uint32_t ms) {
  const uint32_t start = millis();
  while (uint32_t(millis() - start) < ms) { if (!bridgeSafe()) return false; delay(1); }
  return bridgeSafe();
}
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
bool signedNumber(const String &s, int &out) {
  const bool negative = s.startsWith("-");
  uint32_t n;
  if (!amr::number(s.c_str() + (negative ? 1 : 0), negative ? 128 : 127, n)) return false;
  out = negative ? -int(n) : int(n); return true;
}
String bridgeFailure() { halt(); return "err HID stopped or disconnected"; }
String runBridge(const String &line) {
  if (line == "ping") return "ok";
  if (!bridgeSafe()) return "err HID held or asleep";
  if (line == "home") return pointer.place(0, 0) ? "ok" : bridgeFailure();
  if (line == "press") return pointer.down() ? "ok" : bridgeFailure();
  if (line == "release") return releaseAll() ? "ok" : bridgeFailure();
  if (line == "click") {
    if (!pointer.down() || !safePause(60)) return bridgeFailure();
    return pointer.up() ? "ok" : bridgeFailure();
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
      return pointer.place(x, y) ? "ok" : bridgeFailure();
    }
    int v, h;
    if (!signedNumber(a, v) || !signedNumber(b, h)) return "err scroll range";
    return pointer.scroll(v, h) ? "ok" : bridgeFailure();
  }
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
    if (!keyboard.report(report) || !safePause(30)) return bridgeFailure();
    return keyboard.up() ? "ok" : bridgeFailure();
  }
  if (cmd == "type") {
    if (!arg.length()) return "err empty text";
    for (size_t i = 0; i < arg.length(); ++i) {
      KeyReport check = {};
      if (uint8_t(arg[i]) >= 128 || !CheckedKeyboard::add(check, uint8_t(arg[i]))) return "err ASCII only";
    }
    for (size_t i = 0; i < arg.length(); ++i) {
      KeyReport value = {}; CheckedKeyboard::add(value, uint8_t(arg[i]));
      if (!bridgeSafe() || !keyboard.report(value) || !bridgeSafe() || !keyboard.up()) return bridgeFailure();
    }
    return "ok";
  }
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
  if (!recording.desktop) { if (wifiStarted) networkOff(); return; }
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
    bridge.printf("hello %s %lu absolute\n", resetReason(), (unsigned long)millis());
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
  uint8_t mac[6] = {};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  snprintf(deviceMac, sizeof(deviceMac), "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  USB.onEvent([](void *, esp_event_base_t, int32_t id, void *) {
    if (id == ARDUINO_USB_STARTED_EVENT) mountEpoch.fetch_add(1);
  });
  pointer.begin(); keyboard.begin();
  USB.productName("Apple Mobile Recorder"); USB.begin();
  // No unsolicited AMR2 response, credentials, commands or recording logs.
}

void loop() {
  const uint32_t now = millis();
  const bool mounted = tud_mounted(), suspended = tud_suspended();
  if (mounted && !wasMounted) mountEpoch.fetch_add(1); // immediate level check plus event latch
  if (!mounted && wasMounted) {
    halt(); stopLatched = false; // only physical detach permits a fresh BOOT session
  }
  if (suspended && !wasSuspended) halt();
  wasMounted = mounted; wasSuspended = suspended;
  if (upload.live && (mounted || upload.epoch != mountEpoch.load())) upload.abort();
  upload.expire(now);
  const bool down = digitalRead(0) == LOW;
  if (down && !buttonDown) { buttonSince = now; buttonDown = true; }
  if (!down) buttonDown = false;
  if (down && now - buttonSince >= 30 && !stopLatched) halt();
  for (unsigned i = 0; i < 128 && Serial0.available(); ++i) {
    const auto result = serialLine.push(char(Serial0.read()));
    if (result == amr::Line::Rejected) { upload.abort(); replyError("line-too-long-or-invalid"); }
    else if (result == amr::Line::Complete) serialRequest(serialLine.text);
  }
  // No recording in offline mode means ZERO HID reports, including zero reports.
  const bool hidEnabled = recording.desktop || recording.size;
  if (hidEnabled && tud_mounted() && !tud_suspended() && needsRelease && pointer.ready() && keyboard.ready()) {
    if (!releaseAll()) halt();
  }
  if (!tud_mounted() || tud_suspended()) needsRelease = true;
  replay.inhibited = resetHold || stopLatched || !recording.armed || recording.desktop || needsRelease;
  const auto action = replay.tick(millis(), tud_mounted(), tud_suspended(), stopLatched);
  if (hidEnabled && tud_mounted() && !tud_suspended() && !needsRelease) {
    bool ok = true;
    if (action == ReplayAction::Move) ok = pointer.place(replaySteps[replay.index].x, replaySteps[replay.index].y);
    else if (action == ReplayAction::Press) ok = pointer.down();
    else if (action == ReplayAction::Release) ok = pointer.up();
    if (!ok) halt();
  }
  serviceBridge(millis());
  delay(1);
}
