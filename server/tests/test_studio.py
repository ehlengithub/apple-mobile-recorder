import hashlib
import json
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError

from hidagent import studio
from hidagent.studio import DeviceProfile, Jobs, Project, Store, TapStep, capture, render_header


def plan(**changes):
    data = {
        "name": "iPad 试验",
        "device": DeviceProfile(name="测试iPad", family="ipad", width=754, height=1080),
        "steps": [TapStep(x=0.2, y=0.3, wait_ms=500), TapStep(x=0.8, y=0.7, wait_ms=2000)],
        "loops": 1,
    }
    return Project(**(data | changes))


def test_plans_bind_device_geometry_and_reject_invalid_timing():
    with pytest.raises(ValidationError):
        DeviceProfile(name="bad", family="iphone", orientation="landscape", width=390, height=844)
    for changes in [
        {"loops": True},
        {"startup_ms": 0},
        {"release_ms": 300},
        {"steps": [dict(do="tap", x=0.2, y=0.4, wait_ms=499)]},
        {"steps": [dict(do="swipe", x=0.2, y=0.4, wait_ms=500)]},
    ]:
        with pytest.raises(ValidationError):
            plan(**changes)


def test_capture_rejects_cross_device_aspect_and_unsupported_actions():
    device = plan().device
    with pytest.raises(ValueError, match="比例"):
        capture("wrong", device, [{"do": "tap", "x": 0.3, "y": 0.4, "guard": {"aspect": 0.46}}])
    with pytest.raises(ValueError, match="仅支持点击"):
        capture("wrong", device, [{"do": "key", "text": "enter"}])
    result = capture("ok", device, [{"do": "tap", "x": 0.3, "y": 0.4, "wait": 1.0001}])
    assert result.steps[0].wait_ms == 1001


def test_save_guards_stay_local_and_changed_targets_cannot_reuse_them(tmp_path):
    store = Store(tmp_path)
    raw = [{"do": "tap", "x": 0.3, "y": 0.4, "wait": 1, "guard": {"png": "PRIVATE_SCREEN", "aspect": 754 / 1080}}]
    project = capture("ok", plan().device, raw)
    store.save(project, raw)
    assert "PRIVATE_SCREEN" not in (store.projects / project.id / "project.json").read_text()
    project.steps[0].wait_ms = 500
    assert store.guarded_steps(project)[0]["wait"] == 0.5
    project.steps[0].x = 0.9
    with pytest.raises(ValueError, match="坐标"):
        store.guarded_steps(project)
    with pytest.raises(ValueError):
        store.get("../../outside")


def test_header_is_generic_and_contains_only_the_plan(tmp_path):
    project = plan(loops=2, startup_ms=5000)
    header = render_header(project, "a" * 32)
    assert "{500, 90, 5000, 2}" in header
    assert "REPLAY_CYCLE_MS = 2500" in header
    assert "6553, 9830, 500" in header
    assert "WIFI_PASS" not in header and "png" not in header
    with pytest.raises(ValueError):
        render_header(project, "../escape")
    phone = plan(device=DeviceProfile(name="iPhone", family="iphone", width=390, height=844))
    assert len(render_header(phone, "b" * 32)) > 0


def prepare_flash(tmp_path, monkeypatch, mismatch=False, fail_backup=False):
    store = Store(tmp_path)
    manager = Jobs(store)
    build_id, flash_id = "a" * 32, "b" * 32
    out = store.jobs / build_id / "firmware"
    out.mkdir(parents=True)
    app = out / "OfflinePlayer.ino.bin"
    app.write_bytes(b"new-application")
    table = struct.pack("<HBBII16sI", 0x50AA, 0, 0, 0x10000, 8192, b"factory", 0)
    (out / "OfflinePlayer.ino.partitions.bin").write_bytes(table)
    studio.atomic_json(
        store.jobs / build_id / "manifest.json",
        {"application_file": app.name, "application_sha256": hashlib.sha256(app.read_bytes()).hexdigest()},
    )
    studio.atomic_json(store.jobs / build_id / "job.json", {"id": build_id, "kind": "build", "status": "succeeded"})
    monkeypatch.setattr(studio, "ports", lambda: [{"port": "/dev/test-board"}])
    monkeypatch.setattr(studio, "tools_status", lambda: {"esptool": "esptool"})
    monkeypatch.setattr(studio.shutil, "which", lambda _: None)
    calls = []

    def run(job, argv, timeout=600):
        calls.append(argv)
        if "chip-id" in argv:
            return "MAC: 7c:e8:b1:a4:85:94"
        if "read-flash" in argv:
            if fail_backup:
                raise RuntimeError("simulated backup failure")
            offset = argv[argv.index("read-flash") + 1]
            if offset == "0x0":
                data = bytearray(b"\xff" * 65536)
                data[0x8000 : 0x8000 + len(table)] = b"x" * len(table) if mismatch else table
            else:
                data = b"old-application" + b"\xff" * 8177
            Path(argv[-1]).write_bytes(data)
        return "Hash of data verified."

    monkeypatch.setattr(manager, "_run", run)
    job = {"id": flash_id, "kind": "flash"}
    return manager, job, build_id, calls


def test_flash_backs_up_before_write_and_does_not_reset_between_backups(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    assert job["backup_ready"] and job["flash_hash_verified"]
    assert all(argv[argv.index("--after") + 1] == "no-reset" for argv in calls[:-1])
    assert "write-flash" in calls[-1] and "hard-reset" in calls[-1]
    assert len([argv for argv in calls if "read-flash" in argv]) == 2
    backup = manager.store.jobs / job["id"] / "backup"
    info = json.loads((backup / "restore.json").read_text())
    assert info["application_sha256"] == hashlib.sha256((backup / "application.bin").read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["layout", "backup"])
def test_flash_never_writes_when_preflight_or_backup_fails(tmp_path, monkeypatch, failure):
    manager, job, build_id, calls = prepare_flash(
        tmp_path, monkeypatch, mismatch=failure == "layout", fail_backup=failure == "backup"
    )
    with pytest.raises((ValueError, RuntimeError)):
        manager._flash(job, build_id, "/dev/test-board")
    assert not any("write-flash" in argv for argv in calls)
    assert not any("hard-reset" in argv for argv in calls)


def test_tampered_binary_rejected_before_serial_access(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    (manager.store.jobs / build_id / "firmware/OfflinePlayer.ino.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="变化"):
        manager._flash(job, build_id, "/dev/test-board")
    assert not calls


def test_changed_project_cannot_flash_an_older_build(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    project = plan()
    manager.store.save(project)
    path = manager.store.jobs / build_id / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest.update(
        project_id=project.id,
        project_sha256=hashlib.sha256(json.dumps(project.model_dump(), sort_keys=True).encode()).hexdigest(),
    )
    studio.atomic_json(path, manifest)
    project.steps[0].wait_ms = 600
    manager.store.save(project)
    with pytest.raises(ValueError, match="参数已修改"):
        manager._flash(job, build_id, "/dev/test-board")
    assert not calls


def test_restore_rejects_another_board_and_tampered_backups(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    original_run = manager._run

    def another_board(state, argv, timeout=600):
        if "chip-id" in argv:
            calls.append(argv)
            return "MAC: aa:bb:cc:dd:ee:ff"
        return original_run(state, argv, timeout)

    monkeypatch.setattr(manager, "_run", another_board)
    calls.clear()
    with pytest.raises(ValueError, match="MAC"):
        manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert not any("write-flash" in argv for argv in calls)
    (manager.store.jobs / job["id"] / "backup/application.bin").write_bytes(b"bad backup")
    calls.clear()
    with pytest.raises(ValueError, match="校验失败"):
        manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert not calls


def test_restore_original_application_on_the_original_board(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    calls.clear()
    restored = {"id": "c" * 32}
    manager._restore(restored, job["id"], "/dev/test-board")
    assert restored["flash_hash_verified"]
    assert "write-flash" in calls[-1] and "application.bin" in calls[-1][-1]


def test_process_lock_prevents_parallel_cli_and_web_jobs(tmp_path):
    a, b = Jobs(Store(tmp_path)), Jobs(Store(tmp_path))
    a._process_lock()
    try:
        with pytest.raises(RuntimeError, match="另一个"):
            b._process_lock()
    finally:
        a._release_process_lock()
    b._process_lock()
    b._release_process_lock()


def test_restore_rejects_same_board_after_external_repartition(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    old_run = manager._run

    def changed_layout(state, argv, timeout=600):
        result = old_run(state, argv, timeout)
        if "read-flash" in argv:
            path = Path(argv[-1])
            data = bytearray(path.read_bytes())
            # Same MAC, but data now occupies the old application address.
            struct.pack_into("<HBBII16sI", data, 0x8000, 0x50AA, 1, 2, 0x10000, 8192, b"userdata", 0)
            path.write_bytes(data)
        return result

    monkeypatch.setattr(manager, "_run", changed_layout)
    calls.clear()
    with pytest.raises(ValueError, match="分区"):
        manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert any("read-flash" in call for call in calls)
    assert not any("write-flash" in call or "hard-reset" in call for call in calls)


def test_restore_old_backup_without_installed_hash_checks_original_table(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    path = manager.store.jobs / job["id"] / "backup/restore.json"
    info = json.loads(path.read_text())
    info.pop("installed_partition_sha256")
    studio.atomic_json(path, info)
    calls.clear()
    manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert ["read-flash" in c for c in calls] == [False, True, False]


def test_restore_refuses_truncated_current_partition_read(tmp_path, monkeypatch):
    manager, job, build_id, calls = prepare_flash(tmp_path, monkeypatch)
    manager._flash(job, build_id, "/dev/test-board")
    old_run = manager._run

    def truncated(state, argv, timeout=600):
        result = old_run(state, argv, timeout)
        if "read-flash" in argv:
            Path(argv[-1]).write_bytes(b"\xff" * 100)
        return result

    monkeypatch.setattr(manager, "_run", truncated)
    calls.clear()
    with pytest.raises(ValueError, match="不完整"):
        manager._restore({"id": "c" * 32}, job["id"], "/dev/test-board")
    assert not any("write-flash" in c for c in calls)


def test_universal_build_does_not_embed_project_or_invalidate_with_plan_edits(tmp_path, monkeypatch):
    manager = Jobs(Store(tmp_path))
    monkeypatch.setattr(studio, "tools_status", lambda: {"arduino": "mock-compiler"})

    def compiler(job, argv, timeout):
        out = Path(argv[argv.index("--output-dir") + 1])
        out.mkdir()
        (out / "UniversalPlayer.ino.bin").write_bytes(b"mock-executable")
        return "compiled"

    monkeypatch.setattr(manager, "_run", compiler)
    project = plan()
    job = {"id": "d" * 32}
    manager._save(job)
    manager._build(job, project)
    folder = manager.store.jobs / job["id"]
    assert job["manifest"]["recording_protocol"] == 2
    assert job["manifest"]["project_id"] is None
    assert not (folder / "project.json").exists()
    assert not (folder / "UniversalPlayer/macro_data.h").exists()
    assert not any(project.id in p.read_text() for p in (folder / "UniversalPlayer").iterdir() if p.is_file())
    assert (folder / "UniversalPlayer/replay_engine.h").read_bytes() == (
        studio.ROOT / "hardware/offline_replay/replay_engine.h"
    ).read_bytes()


def test_player_and_legacy_use_identical_replay_engine():
    assert (studio.ROOT / "hardware/player/replay_engine.h").read_bytes() == (
        studio.ROOT / "hardware/offline_replay/replay_engine.h"
    ).read_bytes()
