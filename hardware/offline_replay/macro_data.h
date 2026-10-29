// Inert source template. The workbench generates a private header per build.
#pragma once
#include "replay_engine.h"
constexpr char MACRO_SHA256[] = "template-no-recording";
constexpr char DEPLOYMENT_ID[] = "template-no-recording";
constexpr char REPLAY_PROFILE[] = "template-no-recording";
constexpr ReplayTiming REPLAY_TIMING = {500, 90, 10000, 1};
constexpr uint32_t REPLAY_CYCLE_MS = 0;
constexpr uint32_t RECORDED_CYCLE_MS = 0;
constexpr ReplayStep MACRO_STEPS[] = {{0, 0, 1000}};
constexpr size_t MACRO_COUNT = 0;
