#include "../hardware/offline_replay/replay_engine.h"
#include <cassert>
#include <cstdio>

int main() {
  const ReplayStep steps[] = {{100, 200, 500}, {30000, 30000, 500}};
  ReplayEngine finite(steps, 2, false, {500, 90, 3000, 2});
  unsigned moves=0, presses=0, releases=0;
  uint32_t last=0;
  bool pressed=false;
  for (uint32_t t=0; t<12000; ++t) {
    const auto action=finite.tick(t,true,false,false);
    if (action==ReplayAction::Move) {
      assert(!pressed);
      if (moves) assert(t-last==500);
      else assert(t==3500);
      last=t; ++moves;
    }
    if (action==ReplayAction::Press) { assert(t-last==350); pressed=true; ++presses; }
    if (action==ReplayAction::Release) { assert(t-last==410); pressed=false; ++releases; }
  }
  assert(moves==4 && presses==4 && releases==4 && finite.rounds==2);
  assert(finite.phase==ReplayPhase::Stopped && !pressed);
  finite.tick(12001,false,false,false);
  finite.tick(12002,true,false,false);
  assert(finite.phase==ReplayPhase::Countdown && finite.rounds==0);
  ReplayEngine stop(steps,2,false,{500,90,3000,0});
  stop.phase=ReplayPhase::Pressed;
  assert(stop.tick(10,true,false,true)==ReplayAction::Release);
  stop.tick(10000,true,false,false);
  assert(stop.phase==ReplayPhase::Stopped);
  ReplayEngine sleep(steps,2);
  sleep.tick(0,true,false,false);sleep.tick(1,true,true,false);
  assert(sleep.phase==ReplayPhase::Stopped);
  ReplayEngine legacy(steps,2);
  assert(legacy.interval(500)==1000);
  ReplayEngine physical(steps,2,false,{100,90});
  assert(physical.interval(100)==500);
  assert(ReplayEngine::due(2, UINT32_MAX-1));
  std::puts("PASS: configurable startup/finite loops, 500ms cadence, settle/press/release, stop, suspend, reconnect, timing floor, wrap");
}
