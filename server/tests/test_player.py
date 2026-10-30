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


class BleWire(Wire):
    """UART-only model of the firmware's maintenance/arm/stop transitions."""

    def __init__(self, *, fault=None, recovery="stopped", **kwargs):
        super().__init__(active=encode_recording(plan()), **kwargs)
        self.armed = False
        self.maintenance = True
        self.connected = False
        self.fault, self.recovery = fault, recovery
        self.triggered = False

    def write(self, raw):
        cmd = raw.decode().split()[0]
        if self.triggered and self.recovery == "disconnected":
            raise OSError("simulated cable lost")
        super().write(raw)
        reply = json.loads(self.replies[-1][5:])
        if cmd == "info":
            reply.update(
                transport="ble",
                chip="esp32",
                maintenance=self.maintenance,
                maintenance_saved=True,
                connected=self.connected,
            )
            if self.triggered and self.recovery == "wrong-mac":
                reply["mac"] = "aa:bb:cc:dd:ee:ff"
            if self.triggered and self.recovery == "wrong-chip":
                reply["chip"] = "esp32s3"
        elif cmd in ("arm", "desktop"):
            self.maintenance = False
        elif cmd == "stop" and self.recovery != "ineffective":
            self.armed, self.maintenance, self.connected = False, True, False
            reply.update(armed=False, maintenance=True)
        self.replies[-1] = b"AMR2 " + json.dumps(reply).encode() + b"\n"
        if cmd in ("arm", "desktop") and self.fault:
            self.triggered = True
            if self.fault == "malformed":
                self.replies[-1] = b"AMR2 broken\n"
            elif self.fault == "negative":
                self.replies[-1] = b'AMR2 {"ok":false,"error":"store-error"}\n'
            else:
                self.replies.pop()
                if self.fault == "write":
                    raise OSError("simulated write failed after delivery")
        if cmd == "stop" and self.recovery == "lost-stop-ack":
            self.replies.pop()

    def flush(self):
        if self.triggered and self.fault == "flush":
            self.fault = "timeout"
            raise OSError("simulated flush failed after delivery")

    def read_until(self, *_):
        return self.replies.pop(0) if self.replies else b""


def ble_connect(wire, **kwargs):
    return Player("/dev/mock", MAC, serial_factory=lambda **_: wire, controller="ble-esp32", **kwargs)


@pytest.fixture
def fast_uart_clock(monkeypatch):
    import itertools

    from hidagent import player

    counter = itertools.count(0, 0.1)
    monkeypatch.setattr(player.time, "monotonic", lambda: next(counter))


@pytest.mark.parametrize("fault", ["timeout", "malformed", "negative", "write", "flush"])
@pytest.mark.parametrize("command", ["arm", "desktop"])
def test_uncertain_enable_never_retries_and_verifies_safe_compensation(fast_uart_clock, fault, command):
    from hidagent.player import ControlUncertain

    wire = BleWire(fault=fault)
    player = ble_connect(wire)
    sent = []
    with pytest.raises(ControlUncertain, match="状态未知.*核验软件停止") as result:
        player.control(command, validate_recording(wire.active)["crc"], wire.active, on_send=lambda: sent.append(True))
    assert sent == [True]
    assert wire.commands.count(command) == 1 and wire.commands.count("stop") == 1
    assert not wire.armed and wire.maintenance
    assert result.value.reconciliation == {"status": "stopped", "stop_attempted": True}
    assert result.value.info["maintenance_saved"] is True


@pytest.mark.parametrize("recovery", ["disconnected", "wrong-mac", "wrong-chip", "ineffective"])
def test_uncertain_arm_keeps_may_running_when_reconciliation_is_not_proof(fast_uart_clock, recovery):
    from hidagent.player import ControlUncertain

    wire = BleWire(fault="timeout", recovery=recovery)
    player = ble_connect(wire)
    with pytest.raises(ControlUncertain, match="可能已运行.*长按BOOT两秒") as result:
        player.control("arm", validate_recording(wire.active)["crc"], wire.active)
    assert result.value.reconciliation["status"] == "unknown" and wire.armed
    assert wire.commands.count("arm") == 1
    if recovery != "ineffective":
        assert "stop" not in wire.commands


def test_lost_stop_ack_still_reads_back_maintenance_without_rearming(fast_uart_clock):
    from hidagent.player import ControlUncertain

    wire = BleWire(fault="timeout", recovery="lost-stop-ack")
    player = ble_connect(wire)
    with pytest.raises(ControlUncertain) as result:
        player.control("arm", validate_recording(wire.active)["crc"], wire.active)
    assert result.value.reconciliation["status"] == "stopped"
    assert wire.commands.count("arm") == 1 and wire.commands.count("stop") == 1


def test_stop_can_open_live_ble_but_connection_remains_stop_only():
    wire = BleWire()
    wire.maintenance, wire.armed, wire.connected = False, True, True
    player = ble_connect(wire, stop_only=True)
    for command in ("arm", "desktop"):
        with pytest.raises(ValueError, match="停止专用"):
            player.control(command)
    with pytest.raises(ValueError, match="停止专用"):
        player.upload(wire.active)
    assert player.control("stop")["armed"] is False
    assert wire.commands == ["info", "info", "stop", "info"]


@pytest.mark.parametrize("change", ["mac", "transport", "chip"])
def test_stop_still_checks_board_identity_before_sending(change):
    wire = BleWire()
    wire.maintenance = False
    if change == "mac":
        wire.mac = "aa:bb:cc:dd:ee:ff"
    else:
        original = wire.write

        def changed(raw):
            original(raw)
            reply = json.loads(wire.replies[-1][5:])
            reply[change] = "usb" if change == "transport" else "esp32s3"
            wire.replies[-1] = b"AMR2 " + json.dumps(reply).encode() + b"\n"

        wire.write = changed
    with pytest.raises(ValueError):
        ble_connect(wire, stop_only=True)
    assert wire.commands == ["info"] and wire.closed


@pytest.mark.parametrize("command", ["upload", "arm", "desktop"])
def test_ble_live_state_still_blocks_all_unsafe_commands(command):
    wire = BleWire()
    player = ble_connect(wire)
    wire.maintenance = False
    with pytest.raises(ValueError, match="维护"):
        if command == "upload":
            player.upload(wire.active)
        else:
            player.control(command, validate_recording(wire.active)["crc"], wire.active)
    assert all(c == "info" for c in wire.commands)


@pytest.mark.parametrize("recovery", ["stopped", "disconnected"])
def test_arm_job_persists_uncertainty_and_invalidation_on_failure(tmp_path, monkeypatch, fast_uart_clock, recovery):
    from hidagent import studio

    class InlineThread:
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    wire = BleWire(fault="timeout", recovery=recovery)
    manager = studio.Jobs(studio.Store(tmp_path))
    monkeypatch.setattr(studio.threading, "Thread", InlineThread)
    monkeypatch.setattr(manager, "_open_player", lambda *a, **kw: ble_connect(wire, stop_only=kw["stop_only"]))
    job = manager.start("player-control", plan(), port="mock", expected_mac=MAC, command="arm", controller="ble-esp32")
    persisted = studio.Jobs(manager.store).get(job["id"])
    assert persisted["status"] == "failed" and persisted["command"] == "arm"
    assert persisted["command_sent"] and persisted["maintenance_confirmation_invalidated"]
    assert persisted["operation_state"] == "unknown"
    assert persisted["control_uncertain"] is True
    assert persisted["may_be_running"] == (recovery != "stopped")
    assert wire.closed and wire.commands.count("arm") == 1


def test_failed_preflight_does_not_claim_arm_was_sent(tmp_path, monkeypatch):
    from hidagent import studio

    wire = BleWire()
    manager = studio.Jobs(studio.Store(tmp_path))
    monkeypatch.setattr(manager, "_open_player", lambda *a, **kw: ble_connect(wire))
    job = {"id": "a" * 32}
    with pytest.raises(ValueError, match="不同"):
        manager._player_control(job, plan(loops=3), "mock", MAC, "arm", controller="ble-esp32")
    assert job["command_sent"] is False and not job["maintenance_confirmation_invalidated"]
    assert job["control_uncertain"] is False
    assert wire.closed and "arm" not in wire.commands


def test_stop_job_opens_stop_only_connection_and_rechecks_mac(tmp_path, monkeypatch):
    from hidagent import player as transport
    from hidagent import studio

    wire = BleWire()
    wire.armed, wire.maintenance, wire.connected = True, False, True
    factory = transport.Player
    opened = []

    def mocked_player(port, expected_mac, **kwargs):
        opened.append(kwargs)
        return factory(port, expected_mac, serial_factory=lambda **_: wire, **kwargs)

    monkeypatch.setattr(transport, "Player", mocked_player)
    monkeypatch.setattr(studio, "ports", lambda: [{"port": "mock"}])
    monkeypatch.setattr(studio.shutil, "which", lambda _: None)
    manager = studio.Jobs(studio.Store(tmp_path))
    job = {"id": "a" * 32}
    manager._player_control(job, None, "mock", MAC, "stop", controller="ble-esp32")
    assert opened == [{"controller": "ble-esp32", "stop_only": True}]
    assert job["command_sent"] and job["maintenance_confirmation_invalidated"]
    assert job["operation_state"] == "confirmed" and not job["may_be_running"]
    assert job["control_uncertain"] is False and wire.closed and not wire.armed

    wire.mac = "aa:bb:cc:dd:ee:ff"
    wire.commands.clear()
    with pytest.raises(ValueError, match="MAC"):
        manager._player_control({"id": "b" * 32}, None, "mock", MAC, "stop", controller="ble-esp32")
    assert wire.commands == ["info"]
