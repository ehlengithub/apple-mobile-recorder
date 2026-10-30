// Preserve the accepted descriptor and seven-byte input layout exactly.
#pragma once
#include "absolute_report.h"
inline std::array<uint8_t, 7> playerBleReport(uint16_t x, uint16_t y, uint8_t buttons,
                                           int8_t wheel = 0, int8_t pan = 0) {
  auto value = absoluteReport(x, y, buttons);
  value[5] = uint8_t(wheel); value[6] = uint8_t(pan);
  return value;
}
