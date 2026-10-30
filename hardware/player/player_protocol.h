// Bounded serial framing/parser/upload state, shared by firmware and host tests.
#pragma once
#include "player_format.h"

namespace amr {
inline int nibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}
inline bool number(const char *p, uint32_t limit, uint32_t &value) {
  value = 0;
  if (!*p) return false;
  for (; *p; ++p) {
    if (*p < '0' || *p > '9') return false;
    const uint32_t d = uint32_t(*p - '0');
    if (d > limit || value > (limit - d) / 10) return false;
    value = value * 10 + d;
  }
  return true;
}
inline bool hex32(const char *p, uint32_t &value) {
  if (strlen(p) != 8) return false;
  value = 0;
  for (unsigned i = 0; i < 8; ++i) {
    const int d = nibble(p[i]); if (d < 0) return false;
    value = (value << 4) | uint32_t(d);
  }
  return true;
}
enum class Op { Info, Read, Begin, Data, Commit, Arm, Desktop, Stop, Abort };
struct Request {
  Op op = Op::Info;
  uint32_t offset = 0, length = 0, crc = 0;
  uint8_t data[kChunkSize] = {};
};
// Exactly one ASCII space separates tokens; no trailing/extra tokens, signs,
// whitespace, partial integers, embedded CR/NUL, or overflow are accepted.
inline const char *parse(char *line, Request &r) {
  char *tokens[4] = {line, nullptr, nullptr, nullptr};
  unsigned count = 1;
  for (char *p = line; *p; ++p) {
    if (*p == ' ') {
      if (p == line || p[-1] == '\0' || !p[1] || count == 4) return "syntax";
      *p = '\0'; tokens[count++] = p + 1;
    } else if (uint8_t(*p) < 33 || uint8_t(*p) > 126) return "syntax";
  }
  struct Named { const char *name; Op op; };
  const Named simple[] = {{"info", Op::Info}, {"commit", Op::Commit}, {"arm", Op::Arm},
                         {"desktop", Op::Desktop}, {"stop", Op::Stop}, {"abort", Op::Abort}};
  for (const auto &v : simple) if (!strcmp(tokens[0], v.name)) {
    if (count != 1) return "syntax";
    r.op = v.op; return nullptr;
  }
  if (count != 3) return "syntax";
  if (!strcmp(tokens[0], "read")) {
    r.op = Op::Read;
    if (!number(tokens[1], kMaxSize, r.offset) || !number(tokens[2], kChunkSize, r.length) || !r.length)
      return "range";
  } else if (!strcmp(tokens[0], "begin")) {
    r.op = Op::Begin;
    if (!number(tokens[1], kMaxSize, r.length) || r.length < kMinSize ||
        (r.length - kHeaderSize - 4) % 8 || !hex32(tokens[2], r.crc)) return "range";
  } else if (!strcmp(tokens[0], "data")) {
    r.op = Op::Data;
    const size_t n = strlen(tokens[2]);
    if (!number(tokens[1], kMaxSize, r.offset) || !n || n > kChunkSize * 2 || n % 2) return "range";
    r.length = uint32_t(n / 2);
    for (size_t i = 0; i < r.length; ++i) {
      const int a = nibble(tokens[2][2*i]), b = nibble(tokens[2][2*i+1]);
      if (a < 0 || b < 0) return "hex";
      r.data[i] = uint8_t(a * 16 + b);
    }
  } else return "unknown-command";
  return nullptr;
}
struct Line {
  static constexpr size_t capacity = 288;
  char text[capacity + 1] = {};
  size_t length = 0;
  bool dropping = false;
  enum Result { Pending, Complete, Rejected };
  Result push(char c) {
    if (c == '\n') {
      const bool bad = dropping;
      if (length && text[length - 1] == '\r') --length; // CRLF only
      text[length] = '\0'; length = 0; dropping = false;
      return bad ? Rejected : Complete;
    }
    if (dropping) return Pending;
    if (!c || length == capacity) { dropping = true; return Pending; }
    text[length++] = c;
    return Pending;
  }
};
struct Upload {
  uint8_t bytes[kMaxSize] = {};
  size_t size = 0, offset = 0;
  uint32_t crc = 0, last = 0, epoch = 0;
  bool live = false;
  void abort() { live = false; size = offset = 0; }
  bool expire(uint32_t now) {
    if (live && uint32_t(now - last) >= 5000) { abort(); return true; }
    return false;
  }
  void begin(const Request &r, uint32_t now, uint32_t mountEpoch) {
    size = r.length; crc = r.crc; offset = 0; last = now; epoch = mountEpoch; live = true;
  }
  const char *append(const Request &r, uint32_t now) {
    expire(now);
    if (!live) return "no-upload";
    if (r.offset != offset || !r.length || r.length > kChunkSize || r.length > size - offset) {
      abort(); return "offset-length";
    }
    memcpy(bytes + offset, r.data, r.length); offset += r.length; last = now;
    return nullptr;
  }
  const char *check(uint32_t now) {
    expire(now);
    if (!live) return "no-upload";
    if (offset != size) return "incomplete";
    if (const char *e = validate(bytes, size)) return e;
    // begin CRCHEX is the file trailer CRC: zlib CRC over all preceding bytes.
    if (crc != u32(bytes + size - 4)) return "upload-crc";
    return nullptr;
  }
};
}  // namespace amr
