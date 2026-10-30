// Apple Mobile Recorder; based on ESP32 iPhone Agent by Henry Yuan (MIT).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace amr {
constexpr size_t kHeaderSize = 44, kMaxSteps = 256, kMaxSize = 2096;
constexpr size_t kMinSize = 56, kChunkSize = 128;
struct Header {
  uint16_t count = 0, width = 0, height = 0, release = 0;
  uint32_t startup = 0, loops = 0;
  uint8_t orientation = 0, family = 0, uuid[16] = {};
};
struct Step { uint16_t x, y; uint32_t wait; };
inline uint16_t u16(const uint8_t *p) { return uint16_t(p[0]) | uint16_t(p[1]) << 8; }
inline uint32_t u32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
inline uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t crc = 0xffffffffu;
  while (n--) {
    crc ^= *p++;
    for (unsigned i = 0; i < 8; ++i) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
  }
  return ~crc;
}
inline Step step(const uint8_t *p, size_t i) {
  p += kHeaderSize + 8 * i;
  return {u16(p), u16(p + 2), u32(p + 4)};
}
// No casts to packed structs: deterministic LE decoding on every host/alignment.
inline const char *validate(const uint8_t *p, size_t n, Header *out = nullptr) {
  if (!p || n < kMinSize || n > kMaxSize) return "size";
  if (memcmp(p, "AMR2", 4) || u16(p + 4) != 2) return "magic-version";
  Header h;
  h.count = u16(p + 6); h.startup = u32(p + 8); h.loops = u32(p + 12);
  const uint32_t release = u32(p + 16);
  h.width = u16(p + 20); h.height = u16(p + 22);
  h.orientation = p[24]; h.family = p[25];
  memcpy(h.uuid, p + 28, 16);
  if (!h.count || h.count > kMaxSteps || n != kHeaderSize + 8u * h.count + 4) return "count-length";
  if (h.startup < 3000 || h.startup > 60000 || h.loops > 100000 || release < 90 || release > 1000)
    return "timing";
  h.release = uint16_t(release);
  if (h.width < 100 || h.width > 8192 || h.height < 100 || h.height > 8192 ||
      h.orientation > 1 || (h.orientation == 0) != (h.height > h.width) ||
      h.family < 1 || h.family > 3 || u16(p + 26)) return "device";
  for (size_t i = 0; i < h.count; ++i) {
    const Step s = step(p, i);
    if (s.x > 32767 || s.y > 32767 || s.wait < 500 || s.wait > 3600000 || s.wait < 410u + release)
      return "step";
  }
  if (crc32(p, n - 4) != u32(p + n - 4)) return "crc";
  if (out) *out = h;
  return nullptr;
}
}  // namespace amr
