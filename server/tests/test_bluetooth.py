"""BLE workbench integration; never open a physical serial port."""

import json
from pathlib import Path

import pytest
from test_player import MAC, Wire, plan
from test_player_api import setup
from test_studio import prepare_flash

from hidagent.controllers import PROFILES
from hidagent.player import Player, encode_recording
from hidagent.studio import Jobs, Store, atomic_json


class BleWire(Wire):
    def __init__(
        self, *, maintenance=True, connected=False, chip="esp32", transport="ble", maintenance_saved=True, **kwargs
    ):
        super().__init__(**kwargs)
        self.maintenance, self.connected, self.chip, self.transport = maintenance, connected, chip, transport
        self.maintenance_saved = maintenance_saved

    def write(self, raw):
        super().write(raw)
        if raw == b"info\n":
            reply = json.loads(self.replies[-1][5:])
            reply.update(
                transport=self.transport,
                chip=self.chip,
                maintenance=self.maintenance,
                maintenance_saved=self.maintenance_saved,
                connected=self.connected,
                mounted=self.connected,
            )
            self.replies[-1] = b"AMR2 " + json.dumps(reply).encode() + b"\n"


def test_ble_upload_reuses_identical_recording_and_keeps_maintenance():
    wire = BleWire(active=encode_recording(plan()))
    player = Player("mock", MAC, serial_factory=lambda **_: wire, controller="ble-esp32")
    data = encode_recording(plan(loops=3))
    player.upload(data)
    assert wire.active == data and wire.maintenance and not wire.armed
    assert not any("flash" in cmd for cmd in wire.commands)
    player.close()


@pytest.mark.parametrize(
    "changes",
    [
        dict(maintenance=False),
        dict(connected=True),
        dict(chip="esp32s3"),
        dict(transport="usb"),
        dict(maintenance_saved=False),
    ],
)
def test_ble_missing_maintenance_or_wrong_controller_never_writes(changes):
    wire = BleWire(**changes)
    with pytest.raises(ValueError):
        Player("mock", MAC, serial_factory=lambda **_: wire, controller="ble-esp32")
    assert wire.commands == ["info"] and wire.closed


def test_maintenance_loss_after_serial_handshake_blocks_upload():
    wire = BleWire()
    player = Player("mock", MAC, serial_factory=lambda **_: wire, controller="ble-esp32")
    wire.maintenance = False
    with pytest.raises(ValueError, match="维护"):
        player.upload(encode_recording(plan()))
    assert not any(cmd.startswith("begin") for cmd in wire.commands)
    player.close()


def test_ble_api_requires_its_own_confirmation_and_rejects_unknown_target(tmp_path, monkeypatch):
    client, calls = setup(tmp_path, monkeypatch)
    body = dict(project_id=plan().id, port="mock", expected_mac=MAC, controller="ble-esp32", hid_disconnected=True)
    assert client.post("/api/studio/player/upload", json=body).status_code == 422
    assert not calls
    body["maintenance_confirmed"] = True
    assert client.post("/api/studio/player/upload", json=body).status_code == 200
    assert calls[-1][1]["controller"] == "ble-esp32"
    body["controller"] = "unknown"
    assert client.post("/api/studio/player/upload", json=body).status_code == 422


def test_build_ble_uses_its_chip_profile_and_no_user_recording(tmp_path, monkeypatch):
    from hidagent import studio

    manager = Jobs(Store(tmp_path))
    monkeypatch.setattr(studio, "tools_status", lambda: {"arduino": "mock-compiler"})
    seen = []

    def compiler(job, argv, timeout):
        seen.append(argv)
        out = Path(argv[argv.index("--output-dir") + 1])
        out.mkdir()
        (out / "UniversalPlayer.ino.bin").write_bytes(b"mock-ble-app")
        return "compiled"

    monkeypatch.setattr(manager, "_run", compiler)
    job = {"id": "a" * 32}
    manager._save(job)
    manager._build(job, plan(), controller="ble-esp32")
    assert seen[0][seen[0].index("--fqbn") + 1] == PROFILES["ble-esp32"]["fqbn"]
    assert "compiler.cpp.extra_flags=-DAMR_BLE=1" in seen[0]
    assert job["manifest"]["chip"] == "esp32"
    assert job["manifest"]["transport"] == "ble"
    assert job["manifest"]["project_id"] is None
    assert job["manifest"]["bootloader_offset"] == 4096


def test_wrong_selected_controller_rejected_before_esptool(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="板型"):
        manager._flash(job, build_id, "/dev/test-board", controller="ble-esp32")
    assert not calls


def test_ble_flash_and_restore_use_classic_chip_not_s3(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    path = manager.store.jobs / build_id / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(controller="ble-esp32", chip="esp32")
    atomic_json(path, manifest)
    manager._flash(job, build_id, "/dev/test-board", controller="ble-esp32")
    assert calls and all(argv[argv.index("--chip") + 1] == "esp32" for argv in calls)
    info = json.loads((manager.store.jobs / job["id"] / "backup/restore.json").read_text())
    assert info["controller"] == "ble-esp32" and info["chip"] == "esp32"
    calls.clear()
    manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert all(argv[argv.index("--chip") + 1] == "esp32" for argv in calls)


def test_blank_classic_board_bootloader_starts_at_0x1000(tmp_path, monkeypatch):
    from hidagent import studio

    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    folder = manager.store.jobs / build_id
    path = folder / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(controller="ble-esp32", chip="esp32")
    atomic_json(path, manifest)
    (folder / "firmware/OfflinePlayer.ino.bootloader.bin").write_bytes(b"test-bootloader")
    boot_app = tmp_path / "Library/Arduino15/packages/esp32/hardware/esp32/test/tools/partitions/boot_app0.bin"
    boot_app.parent.mkdir(parents=True)
    boot_app.write_bytes(b"test-ota")
    monkeypatch.setattr(studio.Path, "home", lambda: tmp_path)
    original = manager._run

    def empty_board(state, argv, timeout=600):
        result = original(state, argv, timeout)
        if "read-flash" in argv and argv[argv.index("read-flash") + 1] == "0x0":
            Path(argv[-1]).write_bytes(b"\xff" * 65536)
        return result

    monkeypatch.setattr(manager, "_run", empty_board)
    manager._flash(job, build_id, "/dev/test-board", initialize=True, controller="ble-esp32")
    write = calls[-1]
    assert write[write.index("write-flash") + 1] == "0x1000"
    assert write[write.index("--chip") + 1] == "esp32"
    assert job["backup_ready"] and job["flash_hash_verified"]


@pytest.mark.parametrize(
    "field,value",
    [("chip", "esp32s3"), ("transport", "usb"), ("fqbn", PROFILES["usb-s3"]["fqbn"]), ("bootloader_offset", 0)],
)
def test_inconsistent_ble_manifest_is_rejected_before_any_serial_command(tmp_path, monkeypatch, field, value):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    path = manager.store.jobs / build_id / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        controller="ble-esp32",
        **{key: PROFILES["ble-esp32"][key] for key in ("chip", "transport", "fqbn", "bootloader_offset")},
    )
    manifest[field] = value
    atomic_json(path, manifest)
    with pytest.raises(ValueError, match="板型"):
        manager._flash(job, build_id, "/dev/test-board", controller="ble-esp32")
    assert not calls
