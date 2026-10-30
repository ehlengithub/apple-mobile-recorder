// Pure C++ persistence policy. Backend writes are synchronous/atomic per key.
#pragma once
#include "player_format.h"

namespace amr {
// Backend: ready, blocked(), active(), armed(), desktop(), read(slot,p,cap),
// writeArmed(bool), writeDesktop(bool), write(slot,p,n), activate(slot).
// blocked() includes a USB mount-generation guard, not just the current level.
template<class Backend> class Store {
 public:
  explicit Store(Backend &backend) : io(backend) {}
  Backend &io;
  uint8_t bytes[kMaxSize] = {};
  size_t size = 0;
  int active = -1;
  bool armed = false, desktop = false;
  Header header;

  bool load() {
    size = 0; armed = false; active = -1; header = Header{};
    if (!io.ready) return false;
    desktop = io.desktop();
    const int slot = io.active();
    if (slot != 0 && slot != 1) return false;
    active = slot;
    const size_t n = io.read(slot, bytes, sizeof(bytes));
    if (validate(bytes, n, &header)) return false; // never fall back to inactive
    size = n; armed = io.armed() && !desktop;
    return true;
  }
  bool disarm() {
    armed = false; // fail closed in RAM even if persistence fails
    return io.ready && io.writeArmed(false);
  }
  const char *commit(const uint8_t *data, size_t n) {
    if (io.blocked()) return "hid-connected";
    if (const char *e = validate(data, n)) return e;
    // This must be durable and read back BEFORE touching either slot.
    if (!disarm()) return "disarm-store";
    if (io.blocked()) return "hid-connected";
    const int target = active == 0 ? 1 : 0;
    if (!io.write(target, data, n)) return "slot-write";
    if (io.blocked()) return "hid-connected";
    uint8_t check[kMaxSize];
    const size_t got = io.read(target, check, sizeof(check));
    if (got != n || validate(check, got) || memcmp(check, data, n)) return "slot-readback";
    if (io.blocked()) return "hid-connected";
    if (!io.writeDesktop(false)) return "mode-store";
    desktop = false;
    if (io.blocked()) return "hid-connected";
    // A SINGLE NVS key is the commit point. No fallible storage writes after it.
    if (!io.activate(target)) return "active-store";
    memcpy(bytes, check, n); size = n; active = target;
    validate(bytes, size, &header);
    return nullptr;
  }
  const char *arm() {
    if (io.blocked()) return "hid-connected";
    if (!size || validate(bytes, size)) return "no-recording";
    if (desktop) return "desktop-mode";
    if (!io.ready || !io.writeArmed(true)) { disarm(); return "arm-store"; }
    if (io.blocked()) { disarm(); return "hid-connected"; }
    armed = true;
    return nullptr;
  }
  const char *setDesktop() {
    if (io.blocked()) return "hid-connected";
    if (!disarm()) return "disarm-store";
    if (io.blocked()) return "hid-connected";
    if (!io.writeDesktop(true)) return "mode-store";
    desktop = true;
    return io.blocked() ? "hid-connected" : nullptr;
  }
};
}  // namespace amr
