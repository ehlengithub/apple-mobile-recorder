// BLE driver adapted from accepted offline_ble at a790fd2 (Henry Yuan, MIT).
#pragma once
#include <atomic>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include "player_ble_report.h"
#include "player_control.h"

class PlayerHid : public NimBLEServerCallbacks, public NimBLECharacteristicCallbacks {
  NimBLEServer *server = nullptr;
  NimBLECharacteristic *input = nullptr;
  std::atomic<bool> enabled{false}, connected_{false}, secured{false}, subscribed_{false};
  std::atomic<bool> suspended_{false}, reportMode{true};
  std::atomic<uint16_t> handle{BLE_HS_CONN_HANDLE_NONE};
  amr::LinkEpochs epochs;
  uint16_t x = 0, y = 0;
  uint8_t buttons = 0;
  bool report(int8_t wheel = 0, int8_t pan = 0) {
    if (!ready()) return false;
    const auto value = playerBleReport(x, y, buttons, wheel, pan);
    const uint32_t before = sessionEpoch();
    const uint16_t connection = handle.load();
    // Preserve the accepted bounded queue-jitter retry. Every attempt belongs
    // to the SAME primary session/handle; unrelated rejected peers do not stop it.
    return amr::notifyBounded(
        [&] { return ready() && sessionEpoch() == before && handle.load() == connection; },
        [&] { return input->notify(value.data(), value.size(), connection); },
        [](uint32_t ms) { delay(ms); });
  }
 public:
  static constexpr bool ble = true;
  const char *transport() const { return "ble"; }
  const char *chip() const { return "esp32"; }
  bool mounted() const { return connected_.load() || (server && server->getConnectedCount() != 0); }
  bool encrypted() const { return secured.load(); }
  bool subscribed() const { return subscribed_.load(); }
  bool suspended() const { return suspended_.load(); }
  uint32_t epoch() const { return epochs.management.load(); }
  uint32_t sessionEpoch() const { return epochs.session.load(); }
  bool linkReady() const { return mounted() && encrypted() && subscribed() && reportMode.load(); }
  bool ready() const { return enabled.load() && linkReady() && !suspended(); }
  bool begin(bool active) {
    constexpr char BLE_NAME[] = "iPad BLE Recorder";
    NimBLEDevice::init(BLE_NAME);
    NimBLEDevice::setSecurityAuth(true, false, true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    server = NimBLEDevice::createServer(); server->setCallbacks(this);
    server->advertiseOnDisconnect(false); // callbacks own maintenance-aware advertising
    auto hid = new NimBLEHIDDevice(server);
    input = hid->getInputReport(1);
    const auto initial = absoluteReport(0, 0, 0);
    input->setValue(initial.data(), initial.size()); input->setCallbacks(this);
    hid->setManufacturer("ESP32 iPad Recorder");
    hid->setPnp(0x02, 0xe502, 0xa111, 0x0210);
    hid->setHidInfo(0x00, 0x02);
    hid->setReportMap((uint8_t*)BLE_ABSOLUTE_MAP, sizeof(BLE_ABSOLUTE_MAP));
    hid->setBatteryLevel(100);
    hid->getHidService()->getCharacteristic(uint16_t(0x2A4C))->setCallbacks(this);
    hid->getHidService()->getCharacteristic(uint16_t(0x2A4E))->setCallbacks(this);
    server->start();
    auto adv = NimBLEDevice::getAdvertising();
    adv->setName(BLE_NAME); adv->setAppearance(0x03C2);
    adv->addServiceUUID(hid->getHidService()->getUUID()); adv->enableScanResponse(true);
    return !active || enable(); // default/new/maintenance build never advertises
  }
  bool enable() {
    enabled = true;
    if (NimBLEDevice::startAdvertising()) return true;
    enabled = false; return false;
  }
  void disable() {
    enabled = false; epochs.event(false);
    NimBLEDevice::stopAdvertising();
    if (server) for (const auto connection : server->getPeerDevices()) server->disconnect(connection);
  }
  bool place(uint16_t nx, uint16_t ny) { x = nx; y = ny; return report(); }
  bool down() { buttons = 1; return report(); }
  bool up() { buttons = 0; return report(); }
  bool scroll(int8_t wheel, int8_t pan) { return report(wheel, pan); }
  bool releaseAll() { return up(); }
  void onConnect(NimBLEServer *s, NimBLEConnInfo &info) override {
    const bool accepted = enabled.load() && !connected_.exchange(true);
    epochs.event(accepted);
    if (!accepted) { s->disconnect(info.getConnHandle()); return; }
    handle = info.getConnHandle();
    secured = false; subscribed_ = false; suspended_ = false; reportMode = true;
    s->updateConnParams(info.getConnHandle(), 6, 12, 0, 200);
    NimBLEDevice::startSecurity(info.getConnHandle());
  }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &info, int) override {
    const bool primary = info.getConnHandle() == handle.load();
    epochs.event(primary);
    if (!primary) return;
    connected_ = false; secured = false; subscribed_ = false; suspended_ = false;
    handle = BLE_HS_CONN_HANDLE_NONE;
    if (enabled.load()) NimBLEDevice::startAdvertising();
  }
  void onAuthenticationComplete(NimBLEConnInfo &info) override {
    if (info.getConnHandle() == handle.load()) secured = info.isEncrypted();
  }
  void onSubscribe(NimBLECharacteristic *c, NimBLEConnInfo &info, uint16_t value) override {
    if (c == input && info.getConnHandle() == handle.load()) subscribed_ = (value & 1) != 0;
  }
  void onWrite(NimBLECharacteristic *c, NimBLEConnInfo &info) override {
    if (info.getConnHandle() != handle.load()) return;
    const auto value = c->getValue(); if (value.size() != 1) return;
    if (c->getUUID() == NimBLEUUID(uint16_t(0x2A4C))) suspended_ = value[0] == 0;
    if (c->getUUID() == NimBLEUUID(uint16_t(0x2A4E))) reportMode = value[0] == 1;
  }
};
