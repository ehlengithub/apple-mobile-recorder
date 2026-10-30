import json
import struct
import zlib

import pytest

from hidagent.player import HEADER, Player, encode_recording, validate_recording
from hidagent.studio import DeviceProfile, Project, TapStep

MAC = "02:00:00:00:00:01"


def plan(**changes):
    data = dict(
        id="ab" * 16,
        name="isolated",
        device=DeviceProfile(name="test", family="ipad", width=754, height=1080),
        steps=[TapStep(x=0.2, y=0.3, wait_ms=500)],
        loops=2,
    )
    return Project(**(data | changes))


class Wire:
    def __init__(self, *, mounted=False, mac=MAC, active=None, fail=None):
        self.mounted, self.mac, self.active, self.fail = mounted, mac, active, fail
        self.commands, self.replies = [], []
        self.armed, self.mode, self.closed = True, "offline", False
        self.staging = None

    def open(self):
        pass

    def close(self):
        self.closed = True

    def flush(self):
        pass

    def write(self, raw):
        line = raw.decode().strip()
        self.commands.append(line)
        cmd, *args = line.split()
        meta = validate_recording(self.active) if self.active else {"size": 0, "crc": ""}
        reply = {"ok": True}
        if cmd == "info":
            reply.update(
                protocol=2,
                mac=self.mac,
                mounted=self.mounted,
                suspended=False,
                mode=self.mode,
                armed=self.armed,
                size=meta["size"],
                crc=meta["crc"],
            )
        elif self.fail == cmd:
            reply = {"ok": False, "error": "simulated interrupted upload"}
        elif cmd == "read":
            offset, length = map(int, args)
            reply.update(offset=offset, data=self.active[offset : offset + length].hex())
        elif cmd == "begin":
            self.staging = bytearray()
            self.armed = False
            reply["offset"] = 0
        elif cmd == "data":
            offset = int(args[0])
            assert offset == len(self.staging)
            self.staging.extend(bytes.fromhex(args[1]))
            reply["offset"] = len(self.staging)
        elif cmd == "commit":
            self.active = bytes(self.staging)
            self.mode, self.armed = "offline", False
            reply.update(validate_recording(self.active), armed=False)
        elif cmd == "arm":
            self.armed = True
            reply["armed"] = True
        elif cmd == "desktop":
            self.mode, self.armed = "desktop", False
            reply["mode"] = "desktop"
        elif cmd == "abort":
            self.staging = None
        self.replies.append(b"AMR2 " + json.dumps(reply).encode() + b"\n")

    def read_until(self, *_):
        return self.replies.pop(0)


def connect(wire):
    return Player("/dev/mock", MAC, serial_factory=lambda **_: wire)


def test_upload_reads_back_exact_data_and_requires_separate_arm():
    old = encode_recording(plan())
    wire = Wire(active=old)
    player = connect(wire)
    assert player.read_recording() == old
    new = encode_recording(plan(steps=[TapStep(x=0.8, y=0.7, wait_ms=600)]))
    meta = player.upload(new)
    assert wire.active == new and not wire.armed
    assert "arm" not in wire.commands
    player.control("arm", meta["crc"])
    assert wire.armed
    player.close()
    assert wire.closed


@pytest.mark.parametrize("kwargs,error", [({"mounted": True}, "USB/HID"), ({"mac": "aa:bb:cc:dd:ee:ff"}, "MAC")])
def test_wrong_board_or_attached_ipad_never_receives_write(kwargs, error):
    wire = Wire(**kwargs)
    with pytest.raises(ValueError, match=error):
        connect(wire)
    assert wire.commands == ["info"] and wire.closed


@pytest.mark.parametrize("phase", ["begin", "data", "commit"])
def test_upload_failure_never_arms_or_replaces_old_recording(phase):
    old = encode_recording(plan())
    wire = Wire(active=old, fail=phase)
    player = connect(wire)
    new = encode_recording(plan(startup_ms=5000))
    with pytest.raises(RuntimeError, match="interrupted"):
        player.upload(new)
    assert wire.active == old and "arm" not in wire.commands
    assert wire.commands[-1] == "abort"


def test_stale_project_cannot_arm_existing_recording():
    wire = Wire(active=encode_recording(plan()))
    player = connect(wire)
    crc = validate_recording(encode_recording(plan(loops=3)))["crc"]
    with pytest.raises(ValueError, match="不同"):
        player.control("arm", crc)
    assert "arm" not in wire.commands


def test_hid_reconnected_after_handshake_refuses_upload():
    wire = Wire(active=encode_recording(plan()))
    player = connect(wire)
    wire.mounted = True
    with pytest.raises(ValueError, match="USB/HID"):
        player.upload(encode_recording(plan()))
    assert not any(cmd.startswith("begin") for cmd in wire.commands)


def test_readback_tampering_cannot_be_reported_as_uploaded():
    class Corrupt(Wire):
        def write(self, raw):
            super().write(raw)
            if raw.startswith(b"read "):
                reply = json.loads(self.replies[-1][5:])
                data = bytearray.fromhex(reply["data"])
                data[0] ^= 1
                reply["data"] = data.hex()
                self.replies[-1] = b"AMR2 " + json.dumps(reply).encode() + b"\n"

    player = connect(Corrupt())
    with pytest.raises(ValueError, match="版本"):
        player.upload(encode_recording(plan()))


@pytest.mark.parametrize(
    "change", ["version", "count", "truncated", "extra", "crc", "timing", "coordinate", "orientation"]
)
def test_malformed_recordings_fail_closed(change):
    raw = bytearray(encode_recording(plan()))
    if change == "version":
        struct.pack_into("<H", raw, 4, 999)
    elif change == "count":
        struct.pack_into("<H", raw, 6, 0)
    elif change == "truncated":
        raw = raw[:-1]
    elif change == "extra":
        raw.append(0)
    elif change == "crc":
        raw[-1] ^= 1
    elif change == "timing":
        struct.pack_into("<I", raw, HEADER.size + 4, 499)
    elif change == "coordinate":
        struct.pack_into("<H", raw, HEADER.size, 32768)
    elif change == "orientation":
        raw[24] = 1
    if change in ("version", "count", "timing", "coordinate", "orientation"):
        struct.pack_into("<I", raw, len(raw) - 4, zlib.crc32(raw[:-4]))
    with pytest.raises(ValueError):
        validate_recording(raw)


def test_format_maximum_and_quantization_round_trip():
    project = plan(steps=[TapStep(x=1, y=0, wait_ms=500)] * 256)
    raw = encode_recording(project)
    assert len(raw) == 2096 and validate_recording(raw)["steps"] == 256
    assert struct.unpack_from("<HHI", raw, HEADER.size) == (32767, 0, 500)


def test_upload_job_preserves_local_old_recording_and_closes_port(tmp_path, monkeypatch):
    from hidagent.studio import Jobs, Store

    old = encode_recording(plan())
    wire = Wire(active=old)
    player = connect(wire)
    manager = Jobs(Store(tmp_path))
    monkeypatch.setattr(manager, "_open_player", lambda *args: player)
    job = {"id": "a" * 32}
    manager._save(job)
    updated = plan(loops=3)
    manager._upload(job, updated, "/dev/mock", MAC)
    folder = manager.store.jobs / job["id"]
    assert (folder / "previous.amr").read_bytes() == old
    assert (folder / "recording.amr").read_bytes() == encode_recording(updated) == wire.active
    assert wire.closed and job["armed"] is False and job["previous_recording"]


def test_backup_failure_blocks_job_before_any_data_write(tmp_path, monkeypatch):
    from hidagent.studio import Jobs, Store

    wire = Wire(active=encode_recording(plan()), fail="read")
    player = connect(wire)
    manager = Jobs(Store(tmp_path))
    monkeypatch.setattr(manager, "_open_player", lambda *args: player)
    job = {"id": "a" * 32}
    manager._save(job)
    with pytest.raises(RuntimeError, match="interrupted"):
        manager._upload(job, plan(loops=3), "/dev/mock", MAC)
    assert wire.closed and not any(c.startswith("begin") for c in wire.commands)
