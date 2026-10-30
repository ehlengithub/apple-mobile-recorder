// Based on ESP32 iPhone Agent by Henry Yuan; see LICENSE and NOTICE.
#pragma once
#include <stddef.h>
#include <stdint.h>

struct ReplayStep { uint16_t x, y; uint32_t waitMs; };
struct ReplayTiming {
  uint32_t minimumIntervalMs = 1000, minimumReleaseMs = 300;
  uint32_t startupMs = 10000, loopLimit = 0;
};
enum class ReplayAction { None, Move, Press, Release };
enum class ReplayPhase { WaitingUSB, Countdown, WaitingStep, Moving, Pressed, Stopped };

// No USB or Arduino dependencies: the same scheduler runs in host verification.
class ReplayEngine {
 public:
  const ReplayStep *steps;
  size_t count, index = 0;
  uint32_t rounds = 0, deadline = 0, previousMove = 0;
  ReplayPhase phase = ReplayPhase::WaitingUSB;
  bool mountedBefore = false, inhibited = false;
  ReplayTiming timing;
  explicit ReplayEngine(const ReplayStep *s, size_t n, bool hold = false, ReplayTiming limits = {})
      : steps(s), count(n), inhibited(hold), timing(limits) {}
  static bool due(uint32_t now, uint32_t at) { return int32_t(now - at) >= 0; }
  uint32_t interval(uint32_t value) const {
    // The floor must accommodate the unchanged 350ms settle + 60ms press.
    const uint32_t physical = 410 + timing.minimumReleaseMs;
    const uint32_t floor = timing.minimumIntervalMs > physical ? timing.minimumIntervalMs : physical;
    return value < floor ? floor : value;
  }
  void stop() { phase = ReplayPhase::Stopped; }
  ReplayAction tick(uint32_t now, bool mounted, bool suspended, bool stopButton) {
    // A real cable detach starts a new session; suspend does not.
    if (!mounted) {
      const bool release = phase == ReplayPhase::Pressed;
      if (mountedBefore) { inhibited = false; phase = ReplayPhase::WaitingUSB; }
      mountedBefore = false;
      return release ? ReplayAction::Release : ReplayAction::None;
    }
    mountedBefore = true;
    if (suspended && phase == ReplayPhase::WaitingUSB && !stopButton) return ReplayAction::None;
    if (stopButton || suspended || !count) {
      const bool release = phase == ReplayPhase::Pressed;
      stop();
      return release ? ReplayAction::Release : ReplayAction::None;
    }
    if (phase == ReplayPhase::Stopped || inhibited) return ReplayAction::None;
    if (phase == ReplayPhase::WaitingUSB) {
      index = 0; rounds = 0; deadline = now + timing.startupMs;
      phase = ReplayPhase::Countdown;
    } else if (phase == ReplayPhase::Countdown && due(now, deadline)) {
      deadline = now + interval(steps[0].waitMs); phase = ReplayPhase::WaitingStep;
    } else if (phase == ReplayPhase::WaitingStep && due(now, deadline)) {
      previousMove = now; deadline = now + 350; phase = ReplayPhase::Moving;
      return ReplayAction::Move;
    } else if (phase == ReplayPhase::Moving && due(now, deadline)) {
      deadline = now + 60; phase = ReplayPhase::Pressed; return ReplayAction::Press;
    } else if (phase == ReplayPhase::Pressed && due(now, deadline)) {
      if (++index == count) { index = 0; ++rounds; }
      if (timing.loopLimit && rounds >= timing.loopLimit) {
        phase = ReplayPhase::Stopped;
        return ReplayAction::Release;
      }
      // Keep start-to-start intervals and the profile's release cooldown.
      const uint32_t elapsed = now - previousMove;
      const uint32_t wait = interval(steps[index].waitMs);
      deadline = now + (wait > elapsed + timing.minimumReleaseMs ? wait - elapsed : timing.minimumReleaseMs);
      phase = ReplayPhase::WaitingStep; return ReplayAction::Release;
    }
    return ReplayAction::None;
  }
};
