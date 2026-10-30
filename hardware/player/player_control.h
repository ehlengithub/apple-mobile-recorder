// Transport-independent safety policy. No Arduino, USB or NimBLE dependencies.
#pragma once
#include <atomic>
#include <stdint.h>

namespace amr {
struct LinkEpochs {
  std::atomic<uint32_t> management{0}, session{0};
  void event(bool primary) {
    ++management;
    if (primary) ++session;
  }
};

template<class Ready, class Notify, class Pause>
bool notifyBounded(Ready ready, Notify notify, Pause pause) {
  for (unsigned attempt = 0; attempt < 4; ++attempt) {
    if (!ready()) return false;
    if (notify()) return ready();
    if (attempt != 3) pause(2);
  }
  return false;
}

struct AccessGate {
  bool ble = false, maintenance = true;
  uint32_t expectedEpoch = 0;
  bool blocked(bool connected, uint32_t epoch) const {
    return connected || epoch != expectedEpoch || (ble && !maintenance);
  }
};

// BLE's maintenance key is the LAST durable enable operation. Slots, armed
// and desktop remain the shared Store's responsibility. No radio starts here.
// Backend additionally supplies maintenance() and writeMaintenance(bool).
template<class Backend> class Maintenance {
 public:
  Backend &io;
  AccessGate &gate;
  bool durable = false;
  Maintenance(Backend &backend, AccessGate &access) : io(backend), gate(access) {}

  template<class Recording> const char *enter(Recording &recording) {
    gate.maintenance = true; durable = false; // runtime block precedes all I/O
    const bool held = io.ready && io.writeMaintenance(true);
    const bool disarmed = recording.disarm(); // try BOTH writes, even on failure
    durable = held && disarmed;
    return !held ? "maintenance-store" : !disarmed ? "disarm-store" : nullptr;
  }
  template<class Recording> const char *load(Recording &recording) {
    gate.maintenance = true; durable = false;
    if (!io.ready) { recording.armed = false; return "maintenance-store"; }
    // Missing key defaults to maintenance. Never migrate legacy ble-replay/auto
    // into permission to send input. Pairing/bond storage is left untouched.
    if (io.maintenance() || (!recording.desktop && (!recording.size || !recording.armed)))
      return enter(recording);
    gate.maintenance = false; durable = true;
    return nullptr;
  }
  template<class Recording> const char *enable(Recording &recording, bool desktop) {
    if (!gate.maintenance || !durable) return "maintenance-required";
    if (io.blocked()) return "hid-connected";
    const char *error = nullptr;
    if (!desktop && recording.desktop) {
      if (!io.writeDesktop(false)) error = "mode-store";
      else recording.desktop = false;
    }
    if (!error) error = desktop ? recording.setDesktop() : recording.arm();
    if (!error && io.blocked()) error = "hid-connected";
    if (!error && !io.writeMaintenance(false)) error = "maintenance-store";
    if (!error && io.blocked()) error = "hid-connected";
    if (error) { enter(recording); return error; }
    // All persistent prerequisites succeeded while the radio was still off.
    gate.maintenance = false; durable = true;
    return nullptr;
  }
};

struct BootButton {
  bool down = false, stopped = false, maintained = false;
  uint32_t since = 0;
  enum Event { None, Stop, EnterMaintenance };
  bool stopHeld() const { return down && stopped; }
  Event tick(uint32_t now, bool pressed, bool ble) {
    if (!pressed) { down = stopped = maintained = false; return None; }
    if (!down) { down = true; since = now; }
    const uint32_t elapsed = now - since;
    if (ble && elapsed >= 1500 && !maintained) {
      maintained = stopped = true; return EnterMaintenance;
    }
    if (elapsed >= 30 && !stopped) { stopped = true; return Stop; }
    return None;
  }
};
}  // namespace amr
