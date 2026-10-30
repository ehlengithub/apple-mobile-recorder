"""Versioned recording data and a bounded serial transport; importing opens no port."""

from __future__ import annotations

import contextlib
import json
import re
import struct
import time
import zlib

from .controllers import profile

HEADER = struct.Struct("<4sHHIIIHHBBH16s")
STEP = struct.Struct("<HHI")
MAX_BYTES = HEADER.size + 256 * STEP.size + 4
MAC = re.compile(r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
CRC = re.compile(r"^[0-9a-f]{8}$")
FAMILIES = {"iphone": 1, "ipad": 2, "other-apple": 3}


def encode_recording(project):
    device = project.device
    data = HEADER.pack(
        b"AMR2",
        2,
        len(project.steps),
        project.startup_ms,
        project.loops,
        project.release_ms,
        device.width,
        device.height,
        int(device.orientation == "landscape"),
        FAMILIES[device.family],
        0,
        bytes.fromhex(project.id),
    )
    data += b"".join(STEP.pack(round(s.x * 32767), round(s.y * 32767), s.wait_ms) for s in project.steps)
    data += struct.pack("<I", zlib.crc32(data))
    validate_recording(data)
    return data


def validate_recording(data):
    if not HEADER.size + STEP.size + 4 <= len(data) <= MAX_BYTES:
        raise ValueError("录制文件长度无效")
    magic, version, count, startup, loops, release, width, height, orientation, family, reserved, _ = (
        HEADER.unpack_from(data)
    )
    if magic != b"AMR2" or version != 2 or not 1 <= count <= 256 or len(data) != HEADER.size + count * STEP.size + 4:
        raise ValueError("录制版本、步骤数或尾部长度无效")
    if struct.unpack_from("<I", data, len(data) - 4)[0] != zlib.crc32(data[:-4]):
        raise ValueError("录制CRC校验失败")
    if (
        not 3000 <= startup <= 60000
        or not 0 <= loops <= 100000
        or not 90 <= release <= 1000
        or not 100 <= width <= 8192
        or not 100 <= height <= 8192
        or orientation not in (0, 1)
        or (orientation == 0) != (height > width)
        or family not in FAMILIES.values()
        or reserved != 0
    ):
        raise ValueError("设备档案或时序无效")
    for i in range(count):
        x, y, wait = STEP.unpack_from(data, HEADER.size + i * STEP.size)
        if x > 32767 or y > 32767 or not max(500, 410 + release) <= wait <= 3600000:
            raise ValueError("点击坐标或间隔无效")
    return {"size": len(data), "crc": f"{zlib.crc32(data[:-4]):08x}", "steps": count}


class Player:
    """Only call after the user disconnects HID. Serial open may reset a board."""

    def __init__(self, port, expected_mac, serial_factory=None, *, controller="usb-s3"):
        self.target = profile(controller)
        if not MAC.fullmatch(expected_mac):
            raise ValueError("请填写已核对的板卡MAC")
        self.expected_mac = expected_mac
        if serial_factory is None:
            from serial import Serial

            serial_factory = Serial
        self.serial = serial_factory(port=None, baudrate=115200, timeout=0.2, write_timeout=2)
        try:
            # Set before opening; OS/drivers can still glitch, hence unplug HID first.
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = port
            self.serial.open()
            self.info = self.request("info", timeout=8)
            self._identity(self.info)
        except BaseException:
            self.serial.close()
            raise

    def _identity(self, info):
        if info.get("protocol") != 2:
            raise ValueError("当前固件不是通用播放器v2，请先安装一次播放器")
        if info.get("mac") != self.expected_mac:
            raise ValueError("板卡MAC不匹配，未写入录制")
        if info.get("transport", "usb") != self.target["transport"] or (
            info.get("chip", "esp32s3") != self.target["chip"]
        ):
            raise ValueError("播放器芯片或控制方式与所选板型不一致，禁止操作")
        if self.target["transport"] == "ble" and (
            info.get("maintenance") is not True
            or info.get("maintenance_saved") is not True
            or info.get("connected") is not False
        ):
            raise ValueError("蓝牙板未进入维护模式：保持供电长按BOOT两秒后再操作")
        if info.get("mounted") is not False:
            raise ValueError("请先拔下USB/HID与iPhone/iPad的连接，再操作播放器")
        size = info.get("size")
        if type(size) is not int or size < 0 or size > MAX_BYTES:
            raise ValueError("播放器返回无效录制长度")
        if size and not CRC.fullmatch(info.get("crc", "")):
            raise ValueError("播放器返回无效录制校验码")
        return info

    def request(self, line, timeout=5):
        if "\n" in line or "\r" in line or len(line) > 400:
            raise ValueError("无效播放器命令")
        self.serial.write((line + "\n").encode("ascii"))
        self.serial.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self.serial.read_until(b"\n", 1024)
            if not raw.startswith(b"AMR2 "):
                continue
            if not raw.endswith(b"\n"):
                raise RuntimeError("播放器响应过长或不完整")
            try:
                reply = json.loads(raw[5:])
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("播放器响应格式无效") from exc
            if not isinstance(reply, dict) or reply.get("ok") is not True:
                raise RuntimeError(
                    reply.get("error", "播放器拒绝操作") if isinstance(reply, dict) else "播放器响应无效"
                )
            return reply
        raise RuntimeError("播放器响应超时；未确认成功，请保持USB/HID断开")

    def read_recording(self, info=None):
        info = self._identity(info or self.request("info"))
        size = info["size"]
        if not size:
            return b""
        data = bytearray()
        for offset in range(0, size, 128):
            length = min(128, size - offset)
            reply = self.request(f"read {offset} {length}")
            try:
                chunk = bytes.fromhex(reply["data"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("录制读回数据无效") from exc
            if reply.get("offset") != offset or len(chunk) != length:
                raise RuntimeError("录制读回偏移或长度不一致")
            data.extend(chunk)
        if validate_recording(data)["crc"] != info["crc"]:
            raise RuntimeError("板内录制与读回校验码不一致")
        return bytes(data)

    def upload(self, data):
        meta = validate_recording(data)
        self._identity(self.request("info"))
        try:
            reply = self.request(f"begin {len(data)} {meta['crc']}")
            if reply.get("offset") != 0:
                raise RuntimeError("播放器未从首字节开始接收")
            for offset in range(0, len(data), 128):
                chunk = data[offset : offset + 128]
                reply = self.request(f"data {offset} {chunk.hex()}")
                if reply.get("offset") != offset + len(chunk):
                    raise RuntimeError("播放器接收偏移不一致")
            reply = self.request("commit")
            if any(reply.get(key) != meta[key] for key in ("size", "crc")) or reply.get("armed") is not False:
                raise RuntimeError("播放器未确认新录制持久待机，禁止启用")
            info = self._identity(self.request("info"))
            if info.get("armed") is not False or info.get("mode") != "offline":
                raise RuntimeError("上传后播放器状态不正确，禁止启用")
            if self.read_recording(info) != data:
                raise RuntimeError("板内录制逐字节读回不一致，禁止启用")
            return meta
        except BaseException:
            with contextlib.suppress(Exception):
                self.request("abort", timeout=1)
            raise

    def control(self, command, expected_crc="", expected_data=None):
        info = self._identity(self.request("info"))
        if command == "arm":
            if not CRC.fullmatch(expected_crc) or info["crc"] != expected_crc or not info["size"]:
                raise ValueError("板内录制与当前项目不同，请先重新上传")
            data = self.read_recording(info)
            if expected_data is not None and data != expected_data:
                raise ValueError("板内录制与当前项目内容不一致，禁止启用")
        elif command not in ("desktop", "stop"):
            raise ValueError("无效播放器操作")
        reply = self.request(command)
        if command == "arm" and reply.get("armed") is not True:
            raise RuntimeError("播放器未确认启用")
        if command == "desktop" and reply.get("mode") != "desktop":
            raise RuntimeError("播放器未确认电脑录制模式")
        return reply

    def close(self):
        self.serial.close()
