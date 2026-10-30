// Based on ESP32 iPhone Agent by Henry Yuan (MIT). All sends are checked.
#pragma once
#ifndef AMR_BLE
#define AMR_BLE 0
#endif
#if AMR_BLE
#include "player_ble.h"
#else
#include <atomic>
#include "USB.h"
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

class PlayerHid {
  CheckedAbsoluteMouse pointer;
  CheckedKeyboard keyboard;
  std::atomic<uint32_t> generation{0};
 public:
  static constexpr bool ble = false;
  const char *transport() const { return "usb"; }
  const char *chip() const { return "esp32s3"; }
  bool mounted() const { return tud_mounted(); }
  bool suspended() const { return tud_suspended(); }
  bool encrypted() const { return false; }
  bool subscribed() const { return false; }
  uint32_t epoch() const { return generation.load(); }
  uint32_t sessionEpoch() const { return epoch(); }
  bool linkReady() const { return mounted(); }
  bool ready() { return pointer.ready() && keyboard.ready(); }
  bool begin(bool) {
    static PlayerHid *instance = this;
    USB.onEvent([](void *, esp_event_base_t, int32_t id, void *) {
      if (id == ARDUINO_USB_STARTED_EVENT || id == ARDUINO_USB_STOPPED_EVENT)
        ++instance->generation;
    });
    pointer.begin(); keyboard.begin();
    USB.productName("Apple Mobile Recorder"); USB.begin(); return true;
  }
  bool enable() { return true; }
  void disable() {}
  bool place(uint16_t x, uint16_t y) { return pointer.place(x, y); }
  bool down() { return pointer.down(); }
  bool up() { return pointer.up(); }
  bool scroll(int8_t wheel, int8_t pan) { return pointer.scroll(wheel, pan); }
  bool releaseAll() {
    const bool mouseOK = pointer.up();
    const bool keyboardOK = keyboard.up();
    return mouseOK && keyboardOK;
  }
  bool keyboardReport(const KeyReport &value) { return keyboard.report(value); }
  bool keyboardUp() { return keyboard.up(); }
};
#endif
