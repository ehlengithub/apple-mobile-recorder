// Standalone absolute replay, based on ESP32 iPhone Agent by Henry Yuan (MIT).
// No network, screen reader or companion app. BOOT stops until cable reconnect.
#include "USB.h"
#include "USBHIDMouse.h"
#include "tusb.h"
#include "esp_system.h"
#include <Preferences.h>
#include "macro_data.h"

// Arduino's move/press/release discard SendReport's result. Use the same
// descriptor and report layout, but stop on transmission failure.
class CheckedAbsoluteMouse : public USBHIDAbsoluteMouse {
  int16_t x = 0, y = 0;
 public:
  bool ready() { return hid.ready(); }
  bool report(uint8_t buttons) {
    const hid_abs_mouse_report_t value = {buttons, x, y, 0, 0};
    return hid.ready() && hid.SendReport(HID_REPORT_ID_MOUSE, &value, sizeof(value), 100);
  }
  bool place(uint16_t nx, uint16_t ny) { x = nx; y = ny; return report(0); }
  bool down() { return report(MOUSE_LEFT); }
  bool up() { return report(0); }
};
CheckedAbsoluteMouse pointer;
ReplayEngine replay(MACRO_STEPS, MACRO_COUNT, true, REPLAY_TIMING);
String command;
bool stopLatched = false;
uint32_t buttonSince = 0;
bool buttonDown = false;
int lastPhase = -1;
bool needsRelease = true;
bool deploymentHeld = true;
bool deploymentStoreReady = false;

const char *phaseName(ReplayPhase phase) {
  switch (phase) {
    case ReplayPhase::WaitingUSB: return "waiting-usb";
    case ReplayPhase::Countdown: return "countdown";
    case ReplayPhase::WaitingStep: return "waiting-step";
    case ReplayPhase::Moving: return "moving";
    case ReplayPhase::Pressed: return "pressed";
    default: return "stopped";
  }
}

void status() {
  Serial0.printf("offline-v1 phase=%s hold=%d mounted=%d suspended=%d step=%u rounds=%lu steps=%u sha=%s profile=%s cycle_ms=%lu\n",
      phaseName(replay.phase), replay.inhibited, tud_mounted(), tud_suspended(),
      unsigned(replay.index + 1), (unsigned long)replay.rounds, unsigned(MACRO_COUNT), MACRO_SHA256,
      REPLAY_PROFILE, (unsigned long)REPLAY_CYCLE_MS);
}

// Run the actual scheduler against a virtual USB clock, with ZERO HID reports.
void dryrun() {
  if (!MACRO_COUNT) { Serial0.println("err no recording"); return; }
  ReplayTiming testTiming = REPLAY_TIMING;
  testTiming.loopLimit = 2;
  ReplayEngine test(MACRO_STEPS, MACRO_COUNT, false, testTiming);
  unsigned moves = 0, presses = 0, releases = 0;
  for (uint32_t now = 0; now < 10000000 && test.rounds < 2; now += 10) {
    ReplayAction action = test.tick(now, true, false, false);
    if (action == ReplayAction::Move) {
      ++moves;
      const ReplayStep &s = MACRO_STEPS[test.index];
      Serial0.printf("dry move step=%u x=%u y=%u virtual_ms=%lu\n", unsigned(test.index + 1), s.x, s.y, (unsigned long)now);
    }
    if (action == ReplayAction::Press) ++presses;
    if (action == ReplayAction::Release) ++releases;
  }
  Serial0.printf("dry result rounds=%lu moves=%u presses=%u releases=%u hid_reports=0\n",
      (unsigned long)test.rounds, moves, presses, releases);
}

void setup() {
  Serial0.begin(115200);
  pinMode(0, INPUT_PULLUP);
  // Flash/software/watchdog resets stay held. Cold power and EN reset auto-start.
  const auto reason = esp_reset_reason();
  replay.inhibited = reason != ESP_RST_POWERON && reason != ESP_RST_EXT;
  // Every new deployment stays persistently held across all resets until a
  // real USB detach is observed while the board remains powered.
  Preferences prefs;
  if (!prefs.begin("apple-recorder", false)) replay.inhibited = true;
  else {
    if (prefs.getString("deployment", "") != DEPLOYMENT_ID) {
      // Write the lock before the ID, so an interrupted update stays locked.
      const bool lockWritten = prefs.putBool("held", true);
      const bool idWritten = lockWritten && prefs.putString("deployment", DEPLOYMENT_ID) > 0;
      deploymentStoreReady = idWritten && prefs.getBool("held", false)
                             && prefs.getString("deployment", "") == DEPLOYMENT_ID;
      deploymentHeld = true;
    } else {
      deploymentHeld = prefs.getBool("held", true);
      deploymentStoreReady = true;
    }
    replay.inhibited = replay.inhibited || deploymentHeld || !deploymentStoreReady;
    prefs.end();
  }
  pointer.begin();
  USB.productName("Apple Mobile Recorder");
  USB.begin();
  Serial0.printf("offline-v1 boot=%d\n", int(reason));
  status();
}

void loop() {
  const uint32_t now = millis();
  const bool down = digitalRead(0) == LOW;
  if (down && !buttonDown) { buttonSince = now; buttonDown = true; }
  if (!down) buttonDown = false;
  // Ignore only switch bounce; button release never resumes a stopped macro.
  if (down && now - buttonSince >= 30) stopLatched = true;
  while (Serial0.available()) {
    const char c = Serial0.read();
    if (c == '\n') {
      if (command == "status") status();
      else if (command == "stop") {
        stopLatched = true; replay.stop();
        if (tud_mounted() && !tud_suspended()) pointer.up();
        Serial0.println("ok stopped-until-replug");
      } else if (command == "dryrun" && replay.inhibited) dryrun();
      else Serial0.println("err status | stop | dryrun (held only)");
      command = "";
    } else if (c != '\r' && command.length() < 80) command += c;
  }
  const bool mounted = tud_mounted(), suspended = tud_suspended();
  if (!mounted && replay.mountedBefore) {
    stopLatched = false;
    if (deploymentHeld && deploymentStoreReady) {
      Preferences prefs;
      if (prefs.begin("apple-recorder", false)) {
        if (prefs.putBool("held", false)) deploymentHeld = false;
        prefs.end();
      }
    }
  }
  if (!mounted || suspended) needsRelease = true;
  if (mounted && !suspended && needsRelease) {
    // USB mount can precede the interrupt endpoint becoming ready. Wait for
    // enumeration instead of treating this normal startup window as a fault.
    if (!pointer.ready()) { delay(1); return; }
    if (!pointer.up()) { replay.stop(); stopLatched = true; delay(1); return; }
    needsRelease = false;
  }
  const ReplayAction action = replay.tick(now, mounted, suspended, stopLatched);
  if (deploymentHeld || !deploymentStoreReady) replay.inhibited = true;
  // Never block trying to send a report while the host is absent or sleeping.
  if (mounted && !suspended) {
    bool ok = true;
    if (action == ReplayAction::Move) {
      const ReplayStep &s = MACRO_STEPS[replay.index];
      ok = pointer.place(s.x, s.y);
    } else if (action == ReplayAction::Press) ok = pointer.down();
    else if (action == ReplayAction::Release) ok = pointer.up();
    if (!ok) { replay.stop(); stopLatched = true; needsRelease = true; }
  }
  if (int(replay.phase) != lastPhase) { lastPhase = int(replay.phase); status(); }
  delay(1);
}
