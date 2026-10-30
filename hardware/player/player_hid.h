// Based on ESP32 iPhone Agent by Henry Yuan (MIT). All sends are checked.
#pragma once
#include "USBHIDMouse.h"
#include "USBHIDKeyboard.h"
#include "tusb.h"

class CheckedAbsoluteMouse : public USBHIDAbsoluteMouse {
  int16_t x = 0, y = 0;
  uint8_t held = 0;
 public:
  bool ready() { return tud_mounted() && !tud_suspended() && hid.ready(); }
  bool report(int8_t wheel = 0, int8_t pan = 0) {
    const hid_abs_mouse_report_t value = {held, x, y, wheel, pan};
    return ready() && hid.SendReport(HID_REPORT_ID_MOUSE, &value, sizeof(value), 100);
  }
  bool place(uint16_t nx, uint16_t ny) { x = nx; y = ny; return report(); }
  bool down() { held = MOUSE_LEFT; return report(); }
  bool up() { held = 0; return report(); }
  bool scroll(int8_t wheel, int8_t pan) { return report(wheel, pan); }
};

// The one base object registers the standard keyboard descriptor. A separate
// USBHID transport exposes SendReport's bool; base press()/print() discard it.
class CheckedKeyboard : public USBHIDKeyboard {
  USBHID transport;
 public:
  bool ready() { return tud_mounted() && !tud_suspended() && transport.ready(); }
  bool report(const KeyReport &value) {
    return ready() && transport.SendReport(HID_REPORT_ID_KEYBOARD, &value, sizeof(value), 100);
  }
  bool up() { const KeyReport value = {}; return report(value); }
  static bool add(KeyReport &value, uint8_t code) {
    if (code >= 0x80 && code < 0x88) { value.modifiers |= 1u << (code - 0x80); return true; }
    uint8_t usage;
    if (code >= 0x88) usage = code - 0x88;
    else {
      if (code >= 128 || !(usage = KeyboardLayout_en_US[code])) return false;
      if (usage & 0x80) { value.modifiers |= 2; usage &= 0x7f; }
    }
    for (auto &key : value.keys) {
      if (key == usage) return true;
      if (!key) { key = usage; return true; }
    }
    return false;
  }
};
