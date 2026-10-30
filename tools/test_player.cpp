// Host only: c++ -std=c++17 -Wall -Wextra -Werror -pedantic -Ihardware/player
//             tools/test_player.cpp -o /tmp/amr-test-player && /tmp/amr-test-player
#include "player_format.h"
#include "player_store.h"
#include "player_protocol.h"
#include "player_control.h"
#include "player_ble_report.h"
#include "replay_engine.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <stdexcept>
#include <string>
#include <vector>

using Bytes = std::vector<uint8_t>;
static void put16(Bytes &b, size_t p, uint16_t n) { b[p] = n; b[p+1] = n >> 8; }
static void put32(Bytes &b, size_t p, uint32_t n) {
  for (size_t i = 0; i < 4; ++i) b[p+i] = uint8_t(n >> (i*8));
}
static void seal(Bytes &b) { put32(b, b.size()-4, amr::crc32(b.data(), b.size()-4)); }
static Bytes fixture(size_t count = 1) {
  Bytes b(44 + 8*count + 4, 0);
  memcpy(b.data(), "AMR2", 4); put16(b, 4, 2); put16(b, 6, uint16_t(count));
  put32(b, 8, 3000); put32(b, 12, 1); put32(b, 16, 90);
  put16(b, 20, 100); put16(b, 22, 8192); b[25] = 2;
  for (size_t i = 0; i < 16; ++i) b[28+i] = uint8_t(i);
  for (size_t i = 0; i < count; ++i) {
    put16(b, 44+8*i, i ? 32767 : 0); put16(b, 46+8*i, 32767);
    put32(b, 48+8*i, 500);
  }
  seal(b); return b;
}

static void formatTests() {
  assert(amr::crc32(reinterpret_cast<const uint8_t *>("123456789"), 9) == 0xcbf43926u);
  assert(amr::crc32(nullptr, 0) == 0);
  Bytes good = fixture(); amr::Header h;
  assert(!amr::validate(good.data(), good.size(), &h));
  assert(h.count == 1 && h.startup == 3000 && h.release == 90 && h.width == 100 && h.height == 8192);
  assert(h.uuid[15] == 15 && amr::step(good.data(), 0).y == 32767);
  // Golden value independently encoded by Python struct + zlib, not this codec.
  assert(amr::u32(good.data() + good.size()-4) == 0xbda599b1u);
  assert(!amr::validate(fixture(256).data(), 2096));
  auto invalid = [&](size_t at, uint32_t n, unsigned width) {
    Bytes b = good;
    if (width == 4) put32(b, at, n); else if (width == 2) put16(b, at, uint16_t(n)); else b[at] = uint8_t(n);
    seal(b); assert(amr::validate(b.data(), b.size()));
  };
  invalid(0, 'X', 1); invalid(4, 1, 2); invalid(6, 0, 2); invalid(6, 257, 2);
  invalid(8, 2999, 4); invalid(8, 60001, 4); invalid(12, 100001, 4);
  invalid(16, 89, 4); invalid(16, 1001, 4); invalid(16, 0x1000005a, 4);
  invalid(20, 99, 2); invalid(22, 8193, 2); invalid(24, 1, 1); invalid(24, 2, 1);
  invalid(25, 0, 1); invalid(25, 4, 1); invalid(26, 1, 2);
  invalid(44, 32768, 2); invalid(46, 65535, 2); invalid(48, 499, 4); invalid(48, 3600001, 4);
  for (size_t n = 0; n < good.size(); ++n) assert(amr::validate(good.data(), n));
  Bytes b = good; b.push_back(0); assert(amr::validate(b.data(), b.size()));
  b = good; b.back() ^= 1; assert(amr::validate(b.data(), b.size()));
  b = good; b[28] ^= 1; assert(amr::validate(b.data(), b.size())); // UUID covered by CRC
  assert(amr::validate(nullptr, 56));
  b = fixture(256); put32(b, 8, 60000); put32(b, 12, 100000); put32(b, 16, 1000);
  put16(b, 20, 8192); put16(b, 22, 100); b[24] = 1; b[25] = 3;
  for (size_t i = 0; i < 256; ++i) put32(b, 48+8*i, i ? 3600000 : 1410);
  seal(b); assert(!amr::validate(b.data(), b.size()));
  put32(b, 48, 1409); seal(b); assert(amr::validate(b.data(), b.size()));
  b = good; put32(b, 12, 0); seal(b); assert(!amr::validate(b.data(), b.size()));
  // Match Python's portrait=(height>width); square belongs to landscape.
  put16(b, 22, 100); seal(b); assert(amr::validate(b.data(), b.size()));
  b[24] = 1; seal(b); assert(!amr::validate(b.data(), b.size()));
}

struct PowerCut {};
struct Mock {
  bool ready = true, savedArmed = true, savedDesktop = false, mounted = false;
  int selected = 0, calls = 0, failAt = -1, cutAt = -1, mountAt = -1;
  bool corrupt = false, shortRead = false;
  Bytes slots[2] = {fixture(), fixture(2)};
  std::vector<std::string> writes;
  bool blocked() { return mounted; }
  int active() { return selected; }
  bool armed() { return savedArmed; }
  bool desktop() { return savedDesktop; }
  size_t read(int i, uint8_t *p, size_t cap) {
    const size_t n = slots[i].size();
    if (n <= cap) memcpy(p, slots[i].data(), n);
    if (shortRead && i == 1) return n ? n-1 : 0;
    if (corrupt && i == 1 && n <= cap) p[28] ^= 1;
    return n;
  }
  bool before(const char *name) { writes.emplace_back(name); return ++calls != failAt; }
  void after() {
    if (calls == mountAt) mounted = true;
    if (calls == cutAt) throw PowerCut{};
  }
  bool writeArmed(bool value) {
    if (!before(value ? "arm" : "disarm")) return false;
    savedArmed = value; after(); return true;
  }
  bool writeDesktop(bool value) {
    if (!before("mode")) return false;
    savedDesktop = value; after(); return true;
  }
  bool write(int i, const uint8_t *p, size_t n) {
    assert(!savedArmed); // no slot writes allowed before durable disarm
    if (!before("slot")) return false;
    slots[i].assign(p, p+n); after(); return true;
  }
  bool activate(int i) {
    if (!before("active")) return false;
    selected = i; after(); return true;
  }
};

static void storeTests() {
  const Bytes old = fixture(), next = fixture(3);
  for (int failed = 1; failed <= 4; ++failed) {
    Mock io; io.failAt = failed; amr::Store<Mock> store(io); assert(store.load());
    assert(store.commit(next.data(), next.size()));
    assert(io.selected == 0 && io.slots[0] == old && !store.armed);
    assert(std::find(io.writes.begin(), io.writes.end(), "active") == io.writes.end() || failed == 4);
    io.failAt = -1; amr::Store<Mock> reboot(io); assert(reboot.load());
    assert(reboot.size == old.size() && memcmp(reboot.bytes, old.data(), old.size()) == 0);
  }
  for (int cut = 1; cut <= 4; ++cut) {
    Mock io; io.cutAt = cut; amr::Store<Mock> store(io); assert(store.load());
    bool cutSeen = false;
    try { store.commit(next.data(), next.size()); } catch (const PowerCut &) { cutSeen = true; }
    assert(cutSeen); io.cutAt = -1;
    amr::Store<Mock> reboot(io); assert(reboot.load() && !reboot.armed);
    const Bytes &expected = cut == 4 ? next : old;
    assert(reboot.size == expected.size() && !memcmp(reboot.bytes, expected.data(), expected.size()));
    assert(io.slots[0] == old);
  }
  for (int mounted = 1; mounted <= 3; ++mounted) {
    Mock io; io.mountAt = mounted; amr::Store<Mock> store(io); assert(store.load());
    assert(store.commit(next.data(), next.size())); assert(io.selected == 0 && !store.armed);
  }
  for (bool truncated : {false, true}) {
    Mock io; io.corrupt = !truncated; io.shortRead = truncated;
    amr::Store<Mock> store(io); assert(store.load()); assert(store.commit(next.data(), next.size()));
    assert(io.selected == 0 && io.slots[0] == old);
  }
  Mock io; amr::Store<Mock> store(io); assert(store.load());
  assert(!store.commit(next.data(), next.size()));
  assert(io.selected == 1 && !store.armed && !io.savedArmed && !store.desktop);
  assert(io.writes == std::vector<std::string>({"disarm", "slot", "mode", "active"}));
  assert(!store.arm() && store.armed);
  io.mounted = true; assert(store.arm()); assert(store.setDesktop());
  const int writes = io.calls; assert(store.commit(old.data(), old.size())); assert(writes == io.calls);
  io.mounted = false; assert(!store.setDesktop() && store.desktop && !store.armed && !io.savedArmed);
  assert(store.arm()); assert(!store.commit(old.data(), old.size()));
  assert(io.selected == 0 && !store.desktop && !store.armed);
  io.slots[0][28] ^= 1; amr::Store<Mock> bad(io); assert(!bad.load() && !bad.size && !bad.armed);
  assert(!amr::validate(io.slots[1].data(), io.slots[1].size())); // valid inactive is NOT loaded
  io.selected = 255; amr::Store<Mock> missing(io); assert(!missing.load() && !missing.size);
  io.ready = false; assert(!missing.load() && !missing.disarm());
  Mock armIO; amr::Store<Mock> armStore(armIO); assert(armStore.load());
  armIO.mountAt = 1; assert(armStore.arm() && !armStore.armed && !armIO.savedArmed);
  // Torn inactive blob at ANY byte boundary cannot replace the active record.
  for (size_t n = 0; n <= next.size(); ++n) {
    Mock torn; torn.savedArmed = false; torn.slots[1].assign(next.begin(), next.begin()+n);
    amr::Store<Mock> reboot(torn);
    assert(reboot.load() && reboot.active == 0 && !reboot.armed && reboot.size == old.size());
    assert(!memcmp(reboot.bytes, old.data(), old.size()));
  }
  // Invalid pointer is fail-closed even when BOTH blobs happen to be valid.
  Mock pointer; pointer.selected = 2; amr::Store<Mock> noFallback(pointer);
  assert(!noFallback.load() && !noFallback.armed && !noFallback.size);
  // Format errors must not even attempt to disarm/write the backend.
  Mock malformed; amr::Store<Mock> intact(malformed); assert(intact.load());
  Bytes invalid = next; invalid.back() ^= 1;
  assert(intact.commit(invalid.data(), invalid.size()) && malformed.calls == 0);
}

static const char *parseText(const std::string &text, amr::Request &r) {
  std::vector<char> b(text.begin(), text.end()); b.push_back('\0'); return amr::parse(b.data(), r);
}
static void protocolTests() {
  amr::Request r;
  for (const char *s : {"info", "commit", "arm", "desktop", "stop", "abort", "maintenance", "read 0 128",
                        "begin 56 1234aBcD", "begin 2096 00000000", "data 0 0012aBff"}) assert(!parseText(s, r));
  for (const char *s : {"", " info", "info ", "info x", "stop now", "maintenance now", "info\r", "info\t", "read 0 0",
       "read 0 129", "read -1 1", "read 1x 1", "read 429496729600 1", "read 0 1 extra",
       "begin 55 00000000", "begin 57 00000000", "begin 2104 00000000", "begin 56 123",
       "begin 56 0x123456", "data 0 0", "data 0 gg", "data 0 ", "data  0 00", "wat"}) assert(parseText(s, r));
  assert(parseText("data 0 " + std::string(258, '0'), r));
  assert(!parseText("data 2096 " + std::string(256, 'a'), r) && r.length == 128);
  assert(!parseText("read 2096 128", r)); // syntax is legal; storage must still reject out-of-range
  amr::Line line;
  for (char c : std::string("arm") + std::string(400, ' ') + "stop") assert(line.push(c) == amr::Line::Pending);
  assert(line.push('\n') == amr::Line::Rejected);
  for (char c : std::string("info\r")) assert(line.push(c) == amr::Line::Pending);
  assert(line.push('\n') == amr::Line::Complete && !strcmp(line.text, "info"));
  assert(!parseText(line.text, r));
  line.push('a'); line.push('\0'); line.push('r'); line.push('m');
  assert(line.push('\n') == amr::Line::Rejected);
  for (size_t i = 0; i < amr::Line::capacity; ++i) line.push('x');
  assert(line.push('\n') == amr::Line::Complete);
  for (size_t i = 0; i <= amr::Line::capacity; ++i) line.push('x');
  assert(line.push('\n') == amr::Line::Rejected);
}

static void uploadTests() {
  const Bytes b = fixture(256);
  amr::Upload upload; amr::Request r; r.length = uint32_t(b.size());
  r.crc = amr::u32(b.data() + b.size()-4);
  upload.begin(r, UINT32_MAX - 100, 7);
  assert(!upload.expire(100) && upload.epoch == 7);
  assert(upload.check(100)); // incomplete
  for (size_t at = 0; at < b.size(); at += 128) {
    r.offset = uint32_t(at); r.length = uint32_t(std::min<size_t>(128, b.size()-at));
    memcpy(r.data, b.data()+at, r.length);
    assert(!upload.append(r, 200 + uint32_t(at)));
    assert(upload.offset == at + r.length);
  }
  assert(!upload.check(3000));
  upload.crc ^= 1; assert(upload.check(3000)); upload.crc ^= 1;
  upload.bytes[100] ^= 1; assert(upload.check(3000)); upload.bytes[100] ^= 1;
  assert(!upload.expire(upload.last + 4999));
  assert(upload.expire(upload.last + 5000) && !upload.live && upload.check(9000));
  r.length = uint32_t(b.size()); upload.begin(r, 0, 0);
  r.offset = 1; r.length = 1; assert(upload.append(r, 1) && !upload.live);
  r.length = 56; upload.begin(r, 0, 0); r.offset = 0; r.length = 128;
  assert(upload.append(r, 1) && !upload.live);
  r.length = 56; upload.begin(r, 0, 0); r.offset = 0; r.length = 1;
  assert(upload.append(r, 5000) && !upload.live);
}

static void uploadSafetyTests() {
  // Model the actual begin/disarm -> upload -> abort/timeout/HID paths. None of
  // them persists an arm or touches the active slot without explicit commit.
  for (int reason = 0; reason < 4; ++reason) {
    Mock io; amr::Store<Mock> store(io); amr::Upload upload;
    assert(store.load() && store.armed && store.disarm());
    amr::Request begin; begin.length = 56; upload.begin(begin, 0, 9);
    if (reason == 0) upload.abort();
    if (reason == 1) assert(upload.expire(5000));
    if (reason == 2) { io.mounted = true; upload.abort(); }
    if (reason == 3) { // malformed data also aborts without rearming
      amr::Request data; data.offset = 1; data.length = 1;
      assert(upload.append(data, 1));
    }
    assert(!upload.live && !store.armed && !io.savedArmed && io.selected == 0 && io.calls == 1);
    amr::Store<Mock> reboot(io); assert(reboot.load() && !reboot.armed);
  }
}

static void mutationTests() {
  // Deterministic byte mutations + truncations under ASan/UBSan exercise the
  // hostile-file boundary. Format acceptance always implies an exact CRC/size.
  uint32_t seed = 0x414d5232;
  const Bytes original = fixture(256);
  for (unsigned i = 0; i < 6000; ++i) {
    seed = seed * 1664525u + 1013904223u;
    Bytes b = original;
    const size_t at = seed % b.size();
    b[at] ^= uint8_t((seed >> 24) | 1);
    if (i % 2) seal(b); // exercise semantic checks after a correct CRC as well
    if (i % 3 == 0) b.resize((seed >> 8) % b.size());
    if (!amr::validate(b.data(), b.size())) {
      assert(b.size() == 44 + 8u*amr::u16(b.data()+6) + 4);
      assert(amr::u32(b.data()+b.size()-4) == amr::crc32(b.data(), b.size()-4));
    }
  }
  amr::Request request;
  for (unsigned i = 0; i < 4000; ++i) {
    amr::Line line;
    seed = seed * 1664525u + 1013904223u;
    const size_t n = seed % 600;
    for (size_t j = 0; j < n; ++j) {
      seed = seed * 1664525u + 1013904223u;
      // Exclude LF until the explicit final delimiter.
      char c = char(seed >> 24); if (c == '\n') c = ' ';
      assert(line.push(c) == amr::Line::Pending);
    }
    if (line.push('\n') == amr::Line::Complete) amr::parse(line.text, request);
  }
}

static void timingTests() {
  const ReplayStep s[] = {{0, 32767, 500}, {32767, 0, 500}};
  ReplayEngine engine(s, 2, false, {500, 90, 3000, 2});
  unsigned moves = 0, presses = 0, releases = 0; uint32_t moved = 0; bool held = false;
  for (uint32_t t = 0; t < 7000; ++t) {
    const auto a = engine.tick(t, true, false, false);
    if (a == ReplayAction::Move) { assert(!held); assert(t == 3500 + 500*moves); moved = t; ++moves; }
    if (a == ReplayAction::Press) { assert(t-moved == 350); held = true; ++presses; }
    if (a == ReplayAction::Release) { assert(t-moved == 410); held = false; ++releases; }
  }
  assert(moves == 4 && presses == 4 && releases == 4 && !held && engine.rounds == 2);
  assert(engine.phase == ReplayPhase::Stopped);
  for (int trigger = 0; trigger < 3; ++trigger) {
    ReplayEngine e(s, 2, false, {500, 90, 3000, 0});
    e.phase = ReplayPhase::Pressed; e.mountedBefore = true;
    const auto a = e.tick(0, trigger != 2, trigger == 1, trigger == 0);
    assert(a == ReplayAction::Release);
    if (trigger != 2) { e.tick(1000, true, false, false); assert(e.phase == ReplayPhase::Stopped); }
  }
  ReplayEngine heldEngine(s, 2, true, {500, 90, 3000, 0});
  for (uint32_t t = 0; t < 10000; ++t) assert(heldEngine.tick(t, true, false, false) == ReplayAction::None);
  assert(ReplayEngine::due(2, UINT32_MAX-1));
}

struct ControlMock : Mock {
  amr::AccessGate gate{true, true, 0};
  bool savedMaintenance = true, ambiguousEnableFailure = false;
  uint32_t epoch = 0;
  int transientAt = -1;
  bool blocked() {
    if (transientAt >= 0 && calls >= transientAt) { ++epoch; mounted = false; transientAt = -1; }
    return gate.blocked(mounted, epoch);
  }
  bool maintenance() { return savedMaintenance; }
  bool writeMaintenance(bool value) {
    const bool ok = before(value ? "maintenance" : "enable");
    if (ok || (!value && ambiguousEnableFailure)) savedMaintenance = value;
    if (ok) after();
    return ok;
  }
};

static void gateTests() {
  amr::LinkEpochs epochs;
  epochs.event(true); // accepted primary connection
  const uint32_t primary = epochs.session.load();
  epochs.event(false); epochs.event(false); // reject + disconnect unrelated peer
  assert(epochs.management.load() == 3 && epochs.session.load() == primary);
  epochs.event(true); // primary disconnect
  assert(epochs.session.load() == primary + 1);
  for (bool ble : {false, true}) {
    amr::AccessGate gate{ble, true, 17};
    assert(!gate.blocked(false, 17));
    assert(gate.blocked(true, 17) && gate.blocked(false, 18));
    gate.maintenance = false;
    assert(gate.blocked(false, 17) == ble);
    assert(gate.blocked(true, 17) && gate.blocked(false, 18));
    gate.maintenance = true; gate.expectedEpoch = UINT32_MAX;
    assert(!gate.blocked(false, UINT32_MAX) && gate.blocked(false, 0));
  }
}

static void maintenanceTests() {
  {
    ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
    amr::Maintenance<ControlMock> state(io, io.gate);
    assert(!state.load(store) && io.gate.maintenance && state.durable);
    assert(io.savedMaintenance && !io.savedArmed && !store.armed);
    assert(io.writes == std::vector<std::string>({"maintenance", "disarm"}));
    io.calls = 0; io.writes.clear();
    assert(!state.enable(store, false));
    assert(!io.gate.maintenance && !io.savedMaintenance && state.durable && store.armed && io.savedArmed);
    assert(io.writes == std::vector<std::string>({"arm", "enable"}));
    assert(state.enable(store, false)); // already active is not a management state
    amr::Store<ControlMock> coldStore(io); assert(coldStore.load());
    amr::Maintenance<ControlMock> cold(io, io.gate);
    assert(!cold.load(coldStore) && !io.gate.maintenance && coldStore.armed);
    assert(!state.enter(store) && io.gate.maintenance && !io.savedArmed);
    io.calls = 0; io.writes.clear();
    assert(!state.enable(store, true) && store.desktop && !store.armed && !io.savedArmed);
    assert(io.writes == std::vector<std::string>({"disarm", "mode", "enable"}));
    assert(!state.enter(store));
    assert(!state.enable(store, false) && !store.desktop && store.armed);
    // Commit is forbidden while active even after a connection has gone away.
    const Bytes next = fixture(3); assert(store.commit(next.data(), next.size()));
    assert(!state.enter(store)); io.gate.expectedEpoch = io.epoch;
    assert(!store.commit(next.data(), next.size()));
    assert(io.gate.maintenance && io.savedMaintenance && !io.savedArmed && !store.armed);
    assert(!state.enable(store, false)); // subsequent explicit enable still works
  }
  for (bool desktop : {false, true}) {
    const int stages = desktop ? 3 : 2;
    for (int failed = 1; failed <= stages; ++failed) {
      ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
      amr::Maintenance<ControlMock> state(io, io.gate); assert(!state.load(store));
      io.calls = 0; io.failAt = failed;
      assert(state.enable(store, desktop));
      assert(io.gate.maintenance && state.durable && io.savedMaintenance && !store.armed && !io.savedArmed);
      io.failAt = -1;
      amr::Store<ControlMock> rebootStore(io); rebootStore.load();
      amr::Maintenance<ControlMock> reboot(io, io.gate); assert(!reboot.load(rebootStore));
      assert(io.gate.maintenance && !rebootStore.armed);
    }
    for (int cut = 1; cut <= stages; ++cut) {
      ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
      amr::Maintenance<ControlMock> state(io, io.gate); assert(!state.load(store));
      io.calls = 0; io.cutAt = cut;
      bool seen = false;
      try { state.enable(store, desktop); } catch (const PowerCut &) { seen = true; }
      assert(seen); io.cutAt = -1;
      amr::Store<ControlMock> rebootStore(io); rebootStore.load();
      amr::Maintenance<ControlMock> reboot(io, io.gate); assert(!reboot.load(rebootStore));
      // Only the LAST, fully committed enable is allowed to survive power loss.
      assert(io.gate.maintenance == (cut != stages));
      if (cut != stages) assert(!rebootStore.armed && io.savedMaintenance);
      else assert(desktop ? rebootStore.desktop : rebootStore.armed);
    }
  }
  // A readback failure after a write actually reached flash must roll back.
  {
    ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
    amr::Maintenance<ControlMock> state(io, io.gate); assert(!state.load(store));
    io.calls = 0; io.failAt = 2; io.ambiguousEnableFailure = true;
    assert(state.enable(store, false) && io.savedMaintenance && !io.savedArmed && io.gate.maintenance);
  }
  // Each maintenance persistence failure blocks future enables. No "saved"
  // claim is made until BOTH maintenance and disarm are confirmed.
  for (int failed : {1, 2}) {
    ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
    amr::Maintenance<ControlMock> state(io, io.gate); io.failAt = failed;
    assert(state.enter(store) && io.gate.maintenance && !state.durable && !store.armed);
    assert(state.enable(store, false));
  }
  for (bool transient : {false, true}) {
    ControlMock io; amr::Store<ControlMock> store(io); assert(store.load());
    amr::Maintenance<ControlMock> state(io, io.gate); assert(!state.load(store));
    io.calls = 0;
    if (transient) io.transientAt = 1; else io.mountAt = 1;
    assert(state.enable(store, false));
    assert(io.gate.maintenance && io.savedMaintenance && !io.savedArmed && !store.armed);
  }
  {
    ControlMock io; io.ready = false;
    amr::Store<ControlMock> store(io); amr::Maintenance<ControlMock> state(io, io.gate);
    assert(state.load(store) && io.gate.maintenance && !state.durable && !store.armed);
    assert(state.enable(store, false));
  }
  {
    ControlMock io; io.savedMaintenance = false; io.savedArmed = false;
    amr::Store<ControlMock> store(io); assert(store.load());
    amr::Maintenance<ControlMock> state(io, io.gate); assert(!state.load(store));
    assert(io.gate.maintenance && io.savedMaintenance); // impossible enabled-offline state fails closed
  }
}

static void bleReportAndRetryTests() {
  assert(sizeof(BLE_ABSOLUTE_MAP) == 83);
  assert(amr::crc32(BLE_ABSOLUTE_MAP, sizeof(BLE_ABSOLUTE_MAP)) == 0xf80985fau);
  const auto value = playerBleReport(0x1234, 32767, 1, -127, 127);
  const std::array<uint8_t, 7> expected = {{1, 0x34, 0x12, 0xff, 0x7f, 0x81, 0x7f}};
  assert(value == expected);
  assert(playerBleReport(32767, 0, 0) == absoluteReport(32767, 0, 0));
  for (int failures = 0; failures <= 4; ++failures) {
    int calls = 0; unsigned pauses = 0;
    const bool result = amr::notifyBounded([] { return true; }, [&] { return ++calls > failures; },
                                        [&](uint32_t ms) { assert(ms == 2); pauses += ms; });
    assert(result == (failures < 4)); assert(calls == std::min(4, failures+1));
    assert(pauses == unsigned(std::min(3, failures)*2));
  }
  for (bool accepted : {false, true}) {
    bool valid = true; unsigned calls = 0, pauses = 0;
    assert(!amr::notifyBounded([&] { return valid; }, [&] { ++calls; valid = false; return accepted; },
                             [&](uint32_t ms) { pauses += ms; }));
    assert(calls == 1 && pauses <= 2); // epoch/handle/ready loss never retries a replacement peer
  }
  bool called = false;
  assert(!amr::notifyBounded([] { return false; }, [&] { called = true; return true; }, [](uint32_t) {}));
  assert(!called);
}

static void bootButtonTests() {
  for (bool ble : {false, true}) {
    amr::BootButton button;
    assert(button.tick(0, true, ble) == amr::BootButton::None);
    assert(button.tick(29, true, ble) == amr::BootButton::None);
    assert(button.tick(30, true, ble) == amr::BootButton::Stop);
    assert(button.stopHeld());
    assert(button.tick(1499, true, ble) == amr::BootButton::None);
    assert(button.tick(1500, true, ble) == (ble ? amr::BootButton::EnterMaintenance : amr::BootButton::None));
    assert(button.tick(5000, true, ble) == amr::BootButton::None);
    assert(button.tick(5001, false, ble) == amr::BootButton::None);
    assert(!button.stopHeld());
    assert(button.tick(5010, true, ble) == amr::BootButton::None);
    assert(button.tick(5040, true, ble) == amr::BootButton::Stop);
  }
  amr::BootButton wrap;
  assert(wrap.tick(UINT32_MAX-20, true, true) == amr::BootButton::None);
  assert(wrap.tick(9, true, true) == amr::BootButton::Stop);
  assert(wrap.tick(1479, true, true) == amr::BootButton::EnterMaintenance);
}

int main() {
  formatTests(); puts("PASS AMR2 LE format, zlib vector, bounds, device/timing, all truncations and CRC");
  storeTests(); puts("PASS durable disarm, slot failures/readback, power cuts, atomic switch, active-only load, USB gates");
  protocolTests(); puts("PASS strict tokens/numbers/hex, chunk bounds, overflow discard-to-newline, CRLF/NUL");
  uploadTests(); puts("PASS upload offsets/size/CRC, 5s timeout, uint32 clock wrap");
  uploadSafetyTests(); puts("PASS begin -> abort/timeout/HID/malformed upload stays durably disarmed");
  timingTests(); puts("PASS original ReplayEngine 350/60/90ms, 500ms cadence, loops, BOOT/suspend/detach/hold");
  mutationTests(); puts("PASS 6000 file mutations and 4000 bounded serial byte streams");
  gateTests(); puts("PASS USB/BLE level + epoch + maintenance access gates");
  maintenanceTests(); puts("PASS BLE default hold, enter/arm/desktop, NVS failure/rollback, enable power cuts and connection races");
  bleReportAndRetryTests(); puts("PASS accepted BLE descriptor/7-byte report, wheels preserve coordinates, bounded notify retries");
  bootButtonTests(); puts("PASS BOOT 30ms stop, BLE-only 1500ms persistent-maintenance event, debounce and clock wrap");
}
