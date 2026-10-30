// Native UART emulator for Python<->firmware integration. Never links USB.
#include "player_format.h"
#include "player_protocol.h"
#include "player_store.h"
#include "player_control.h"
#include <cstring>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>
#ifndef AMR_HOST_BLE
#define AMR_HOST_BLE 0
#endif
struct Backend {
  bool ready = true, enabled = false, mode = false;
  bool maintenanceFlag = true;
  amr::AccessGate gate{AMR_HOST_BLE != 0, true, 0};
  int slot = -1;
  std::vector<uint8_t> slots[2];
  bool blocked() { return gate.blocked(false, 0); }
  bool maintenance() { return maintenanceFlag; }
  bool writeMaintenance(bool value) { maintenanceFlag = value; return true; }
  int active() { return slot; }
  bool armed() { return enabled; }
  bool desktop() { return mode; }
  bool writeArmed(bool value) { enabled = value; return true; }
  bool writeDesktop(bool value) { mode = value; return true; }
  size_t read(int index, uint8_t *out, size_t cap) {
    const auto &v = slots[index]; if (v.size() > cap) return v.size();
    if (!v.empty()) memcpy(out, v.data(), v.size()); return v.size();
  }
  bool write(int index, const uint8_t *p, size_t n) { slots[index].assign(p,p+n); return true; }
  bool activate(int index) { slot = index; return true; }
};
int main() {
  Backend backend; amr::Store<Backend> store(backend); amr::Upload upload; amr::Line line;
  amr::Maintenance<Backend> maintenance(backend, backend.gate);
  if (AMR_HOST_BLE) maintenance.load(store);
  uint32_t now = 0;
  auto fail=[](const char *e){std::cout<<"AMR2 {\"ok\":false,\"error\":\""<<e<<"\"}"<<std::endl;};
  char c;
  while (std::cin.get(c)) {
    auto result = line.push(c);
    if (result == amr::Line::Pending) continue;
    if (result == amr::Line::Rejected) {upload.abort();fail("framing");continue;}
    amr::Request r;
    if (auto e=amr::parse(line.text,r)) {upload.abort();fail(e);continue;}
    ++now;
    if (AMR_HOST_BLE && (r.op == amr::Op::Begin || r.op == amr::Op::Data || r.op == amr::Op::Commit ||
        r.op == amr::Op::Arm || r.op == amr::Op::Desktop) && backend.blocked()) {
      upload.abort();fail("maintenance-required");continue;
    }
    if (r.op==amr::Op::Info) {
      std::cout<<"AMR2 {\"ok\":true,\"protocol\":2,\"mac\":\"02:00:00:00:00:01\",\"mounted\":false,\"size\":"<<store.size
      <<",\"transport\":\""<<(AMR_HOST_BLE?"ble":"usb")<<"\",\"chip\":\""<<(AMR_HOST_BLE?"esp32":"esp32s3")
      <<"\",\"connected\":false,\"maintenance\":"<<(backend.gate.maintenance?"true":"false")
      <<",\"maintenance_saved\":"<<(maintenance.durable?"true":"false")
      <<",\"armed\":"<<(store.armed?"true":"false")<<",\"mode\":\""<<(store.desktop?"desktop":"offline")<<"\",\"crc\":\"";
      if(store.size) std::cout<<std::hex<<std::setw(8)<<std::setfill('0')<<amr::u32(store.bytes+store.size-4)<<std::dec;
      std::cout<<"\"}"<<std::endl;
    } else if (r.op==amr::Op::Read) {
      if(!store.size || r.offset>store.size || r.length>store.size-r.offset){fail("range");continue;}
      std::cout<<"AMR2 {\"ok\":true,\"offset\":"<<r.offset<<",\"data\":\"";
      for(size_t i=0;i<r.length;++i)std::cout<<std::hex<<std::setw(2)<<std::setfill('0')<<unsigned(store.bytes[r.offset+i]);
      std::cout<<std::dec<<"\"}"<<std::endl;
    } else if (r.op==amr::Op::Begin) {
      if(!store.disarm()){fail("disarm");continue;}
      upload.begin(r,now,0);std::cout<<"AMR2 {\"ok\":true,\"offset\":0}"<<std::endl;
    } else if(r.op==amr::Op::Data){
      if(auto e=upload.append(r,now)){fail(e);continue;}
      std::cout<<"AMR2 {\"ok\":true,\"offset\":"<<upload.offset<<"}"<<std::endl;
    } else if(r.op==amr::Op::Commit){
      const char *e=upload.check(now);if(!e)e=store.commit(upload.bytes,upload.size);upload.abort();
      if(e){fail(e);continue;}
      std::cout<<"AMR2 {\"ok\":true,\"size\":"<<store.size<<",\"crc\":\""<<std::hex<<std::setw(8)<<std::setfill('0')
      <<amr::u32(store.bytes+store.size-4)<<std::dec<<"\",\"armed\":false}"<<std::endl;
    } else if(r.op==amr::Op::Arm){
      if(auto e=AMR_HOST_BLE?maintenance.enable(store,false):store.arm()){fail(e);continue;}
      std::cout<<"AMR2 {\"ok\":true,\"armed\":true}"<<std::endl;
    } else if(r.op==amr::Op::Desktop){
      if(auto e=AMR_HOST_BLE?maintenance.enable(store,true):store.setDesktop()){fail(e);continue;}
      std::cout<<"AMR2 {\"ok\":true,\"mode\":\"desktop\"}"<<std::endl;
    } else if(r.op==amr::Op::Maintenance){
      upload.abort();if(auto e=maintenance.enter(store)){fail(e);continue;}
      std::cout<<"AMR2 {\"ok\":true,\"maintenance\":true}"<<std::endl;
    } else {upload.abort();std::cout<<"AMR2 {\"ok\":true}"<<std::endl;}
  }
}
