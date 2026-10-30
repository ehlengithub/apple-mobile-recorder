"""Exercise real launcher decisions without servers, serial ports or browsers."""

import hashlib
import importlib.util
import io
import json
import shutil
import sys
import types
import urllib.error
from unittest.mock import Mock

import pytest
import uvicorn
from fastapi import FastAPI

from hidagent import runtime_version, studio_api, workbench


def load_runtime(root):
    spec = importlib.util.spec_from_file_location("isolated_runtime_version", root / "runtime_version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "package"
    root.mkdir()
    shutil.copyfile(runtime_version.__file__, root / "runtime_version.py")
    (root / "backend.py").write_text("VERSION = 1\n")
    (root / "static").mkdir()
    (root / "static" / "studio.js").write_text("const version = 1;\n")
    return load_runtime(root)


@pytest.fixture
def launcher(tmp_path, monkeypatch, runtime):
    data = tmp_path / "data"
    monkeypatch.setenv("HIDAGENT_DATA", str(data))
    monkeypatch.setenv("DEVICE_PORT", "7400")
    monkeypatch.setenv("AIRPLAY_NAME", "test")
    monkeypatch.setenv("MIRROR_STREAM", "")
    monkeypatch.setattr(workbench, "runtime_version", runtime)
    monkeypatch.delitem(sys.modules, "hidagent.app", raising=False)
    monkeypatch.setattr(workbench, "load_dotenv", Mock())
    probe = Mock()
    monkeypatch.setattr(workbench.urllib.request, "urlopen", probe)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    browser = Mock()
    monkeypatch.setattr(workbench.webbrowser, "open", browser)
    forbidden = Mock(side_effect=AssertionError("must not start a receiver, timer or touch hardware"))
    monkeypatch.setattr(workbench.subprocess, "Popen", forbidden)
    monkeypatch.setattr(workbench.threading, "Timer", forbidden)
    monkeypatch.setattr(workbench, "tools_status", forbidden)
    monkeypatch.setattr(workbench, "ports", forbidden)
    status = {
        "product": "AppleMobileRecorder",
        "data_fingerprint": hashlib.sha256(str(data.resolve()).encode()).hexdigest(),
        "runtime": runtime.runtime_status(),
    }

    def respond(payload):
        probe.side_effect = lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode())

    respond(status)
    return types.SimpleNamespace(
        runtime=runtime, data=data, status=status, respond=respond, probe=probe, run=run,
        browser=browser, forbidden=forbidden,
    )


def test_matching_runtime_and_directory_reuse_real_main(launcher, capsys):
    assert workbench.main(["serve"]) == 0
    launcher.browser.assert_called_once_with("http://127.0.0.1:8990/")
    launcher.run.assert_not_called()
    launcher.forbidden.assert_not_called()
    assert "版本校验通过" in capsys.readouterr().out


def test_compatible_extra_capability_and_no_browser(launcher):
    launcher.status["runtime"]["capabilities"].append("future-optional-feature")
    launcher.respond(launcher.status)
    assert workbench.main(["serve", "--no-browser"]) == 0
    launcher.browser.assert_not_called()
    launcher.run.assert_not_called()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"api_version": None}, "API版本"),
        ({"api_version": 0}, "API版本"),
        ({"api_version": 2}, "API版本"),
        ({"api_version": True}, "API版本"),
        ({"build_fingerprint": "old-build"}, "后台运行源码"),
        ({"build_fingerprint": None}, "后台运行源码"),
        ({"source_root_fingerprint": "another-checkout"}, "另一个代码目录"),
        ({"capabilities": []}, "API能力"),
        ({"capabilities": "player-control"}, "API能力"),
        ({"capabilities": [{}]}, "API能力"),
        ({"installed_matches_running": False}, "后台导入后源码已变更"),
        ({"installed_matches_running": None}, "无法核验"),
        ({"installed_matches_running": 1}, "无法核验"),
    ],
)
def test_incompatible_service_requires_controlled_restart(launcher, capsys, changes, expected):
    launcher.respond({**launcher.status, "runtime": {**launcher.status["runtime"], **changes}})
    assert workbench.main(["serve"]) == 2
    error = capsys.readouterr().err
    assert expected in error
    assert "原服务终端按Ctrl+C" in error
    assert "未自动停止任何服务" in error
    launcher.run.assert_not_called()
    launcher.browser.assert_not_called()
    launcher.forbidden.assert_not_called()


def test_pre_handshake_legacy_service_is_rejected(launcher, capsys):
    launcher.respond({key: launcher.status[key] for key in ("product", "data_fingerprint")})
    assert workbench.main(["serve", "--no-browser"]) == 2
    assert "缺少运行时版本" in capsys.readouterr().err
    launcher.run.assert_not_called()


def test_data_directory_isolation_precedes_version_check(launcher, capsys):
    launcher.respond({"product": "AppleMobileRecorder", "data_fingerprint": "different-data"})
    assert workbench.main(["serve"]) == 2
    assert "另一个数据目录" in capsys.readouterr().err
    launcher.browser.assert_not_called()
    launcher.run.assert_not_called()


@pytest.mark.parametrize("payload", [{"product": "unrelated"}, [], None])
def test_other_or_malformed_services_are_not_reused_or_stopped(launcher, capsys, payload):
    launcher.respond(payload)
    assert workbench.main(["serve"]) == 2
    assert "--port" in capsys.readouterr().err
    launcher.browser.assert_not_called()
    launcher.run.assert_not_called()
    launcher.forbidden.assert_not_called()


@pytest.mark.parametrize("failure", [ValueError("not JSON"), urllib.error.HTTPError("url", 404, "missing", {}, None)])
def test_invalid_handshake_does_not_start_another_server(launcher, capsys, failure):
    launcher.probe.side_effect = failure
    assert workbench.main(["serve"]) == 2
    assert "握手" in capsys.readouterr().err
    launcher.run.assert_not_called()


def test_no_listener_starts_mock_server_only(launcher):
    launcher.probe.side_effect = urllib.error.URLError(ConnectionRefusedError())
    assert workbench.main(["serve", "--no-browser", "--without-receiver"]) == 0
    launcher.run.assert_called_once_with("hidagent.app:app", host="127.0.0.1", port=8990, access_log=False)
    launcher.browser.assert_not_called()
    launcher.forbidden.assert_not_called()


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionResetError(), urllib.error.URLError("unreachable")])
def test_uncertain_listener_failure_does_not_start_another_service(launcher, capsys, failure):
    launcher.probe.side_effect = failure
    assert workbench.main(["serve"]) == 2
    assert "无法确认此端口的后台版本" in capsys.readouterr().err
    launcher.run.assert_not_called()
    launcher.browser.assert_not_called()
    launcher.forbidden.assert_not_called()


@pytest.mark.parametrize("target", ["backend.py", "static/studio.js"])
@pytest.mark.parametrize("listener", [True, False])
def test_current_process_cannot_reuse_or_serve_after_its_source_changes(launcher, capsys, target, listener):
    if not listener:
        launcher.probe.side_effect = ConnectionRefusedError()
    (launcher.runtime.SOURCE_ROOT / target).write_text("updated after import")
    assert workbench.main(["serve"]) == 2
    assert "导入后源码已变更" in capsys.readouterr().err
    launcher.probe.assert_not_called()
    launcher.browser.assert_not_called()
    launcher.run.assert_not_called()


def test_new_launcher_rejects_old_backend_after_same_directory_update(launcher, capsys, monkeypatch):
    (launcher.runtime.SOURCE_ROOT / "backend.py").write_text("VERSION = 2\n")
    new_runtime = load_runtime(launcher.runtime.SOURCE_ROOT)
    monkeypatch.setattr(workbench, "runtime_version", new_runtime)
    assert new_runtime.SOURCE_ROOT_FINGERPRINT == launcher.status["runtime"]["source_root_fingerprint"]
    assert workbench.main(["serve"]) == 2
    assert "后台运行源码" in capsys.readouterr().err
    launcher.run.assert_not_called()
    launcher.browser.assert_not_called()


def test_changed_source_with_cached_app_cannot_start_mixed_runtime(launcher, capsys, monkeypatch):
    cached_app = types.ModuleType("hidagent.app")
    cached_app.runtime_version = launcher.runtime
    cached_app.DATA_DIR = launcher.data
    monkeypatch.setitem(sys.modules, "hidagent.app", cached_app)
    (launcher.runtime.SOURCE_ROOT / "backend.py").write_text("VERSION = 2\n")
    assert workbench.main(["serve", "--no-browser", "--without-receiver"]) == 2
    assert "源码已变更" in capsys.readouterr().err
    launcher.probe.assert_not_called()
    launcher.run.assert_not_called()


@pytest.mark.parametrize("mismatch", ["unversioned", "old-runtime", "other-data"])
def test_new_launcher_refuses_incompatible_cached_app(launcher, capsys, monkeypatch, mismatch):
    cached_app = types.ModuleType("hidagent.app")
    cached_app.DATA_DIR = launcher.data
    if mismatch != "unversioned":
        cached_app.runtime_version = launcher.runtime if mismatch == "other-data" else object()
    if mismatch == "other-data":
        cached_app.DATA_DIR = launcher.data / "other"
    monkeypatch.setitem(sys.modules, "hidagent.app", cached_app)
    launcher.probe.side_effect = ConnectionRefusedError()
    assert workbench.main(["serve", "--no-browser", "--without-receiver"]) == 2
    assert "已缓存旧版后台或其他数据目录" in capsys.readouterr().err
    launcher.run.assert_not_called()
    launcher.forbidden.assert_not_called()


def test_compatible_cached_app_can_start_mock_server(launcher, monkeypatch):
    cached_app = types.ModuleType("hidagent.app")
    cached_app.runtime_version = launcher.runtime
    cached_app.DATA_DIR = launcher.data
    monkeypatch.setitem(sys.modules, "hidagent.app", cached_app)
    launcher.probe.side_effect = ConnectionRefusedError()
    assert workbench.main(["serve", "--no-browser", "--without-receiver"]) == 0
    launcher.run.assert_called_once()


def test_fresh_launcher_does_not_reuse_old_cached_app_from_same_directory(launcher, capsys, monkeypatch):
    cached_app = types.ModuleType("hidagent.app")
    cached_app.runtime_version = launcher.runtime
    cached_app.DATA_DIR = launcher.data
    monkeypatch.setitem(sys.modules, "hidagent.app", cached_app)
    (launcher.runtime.SOURCE_ROOT / "backend.py").write_text("VERSION = 2\n")
    fresh_runtime = load_runtime(launcher.runtime.SOURCE_ROOT)
    assert fresh_runtime.runtime_is_current()
    assert fresh_runtime.RUNTIME_FINGERPRINT != cached_app.runtime_version.RUNTIME_FINGERPRINT
    monkeypatch.setattr(workbench, "runtime_version", fresh_runtime)
    launcher.probe.side_effect = ConnectionRefusedError()
    assert workbench.main(["serve", "--no-browser", "--without-receiver"]) == 2
    assert "已缓存旧版后台" in capsys.readouterr().err
    launcher.run.assert_not_called()


@pytest.mark.parametrize("listener", [True, False])
def test_source_change_during_probe_is_rejected(launcher, capsys, listener):
    def probe(*args, **kwargs):
        (launcher.runtime.SOURCE_ROOT / "static" / "studio.js").write_text("updated during request")
        if not listener:
            raise ConnectionRefusedError()
        return io.BytesIO(json.dumps(launcher.status).encode())

    launcher.probe.side_effect = probe
    assert workbench.main(["serve"]) == 2
    assert "源码已变更" in capsys.readouterr().err
    launcher.run.assert_not_called()
    launcher.browser.assert_not_called()


def test_equal_content_from_different_checkout_is_not_reused(launcher, tmp_path, capsys):
    other = tmp_path / "other-package"
    shutil.copytree(launcher.runtime.SOURCE_ROOT, other)
    other_runtime = load_runtime(other)
    assert other_runtime.RUNTIME_FINGERPRINT == launcher.runtime.RUNTIME_FINGERPRINT
    launcher.respond({**launcher.status, "runtime": other_runtime.runtime_status()})
    assert workbench.main(["serve"]) == 2
    assert "另一个代码目录" in capsys.readouterr().err
    launcher.run.assert_not_called()


def test_real_status_keeps_import_snapshot_after_disk_changes(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(studio_api, "runtime_version", runtime)
    monkeypatch.setattr(studio_api, "tools_status", lambda: {})
    monkeypatch.setattr(studio_api, "ports", lambda: [])
    monkeypatch.setattr(studio_api.socket, "socket", Mock(side_effect=OSError("isolated")))
    app = FastAPI()
    studio_api.register(
        app, tmp_path / "data", types.SimpleNamespace(), types.SimpleNamespace(live=False),
        types.SimpleNamespace(), types.SimpleNamespace(), lambda: None,
    )
    status = next(route.endpoint for route in app.routes if route.path == "/api/studio/status")
    before = status()
    (runtime.SOURCE_ROOT / "backend.py").write_text("VERSION = 2\n")
    assert not runtime.runtime_is_current()
    # Disk is consulted only for the match flag, never to replace the build ID.
    monkeypatch.setattr(runtime, "source_fingerprint", Mock(return_value="new-disk-fingerprint"))
    after = status()
    for key in ("api_version", "build_fingerprint", "capabilities", "source_root_fingerprint"):
        assert before["runtime"][key] == after["runtime"][key] == runtime.runtime_status()[key]
    assert before["runtime"]["installed_matches_running"] is True
    assert after["runtime"]["installed_matches_running"] is False
    assert after["data_fingerprint"] == before["data_fingerprint"]


def test_fingerprint_tracks_names_additions_deletions_and_ignores_generated_files(runtime):
    root = runtime.SOURCE_ROOT
    original = runtime.source_fingerprint()
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "backend.pyc").write_bytes(b"generated")
    (root / "recording.json").write_text("private data")
    assert runtime.source_fingerprint() == original
    new = root / "static" / "new.css"
    new.write_text("body {}")
    assert runtime.source_fingerprint() != original
    new.unlink()
    assert runtime.source_fingerprint() == original
    source = root / "static" / "studio.js"
    source.rename(root / "static" / "renamed.js")
    assert runtime.source_fingerprint() != original


def test_unreadable_source_fails_closed(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "source_fingerprint", Mock(side_effect=OSError("unreadable")))
    assert not runtime.runtime_is_current()


@pytest.mark.parametrize("controller", ["usb-s3", "ble-esp32"])
def test_cli_stop_without_physical_confirmation_reaches_jobs(launcher, monkeypatch, controller):
    manager = Mock()
    manager.start.return_value = {"id": "stop-job"}
    manager.get.return_value = {"stage": "已停止", "status": "succeeded"}
    jobs = Mock(return_value=manager)
    monkeypatch.setattr(workbench, "Jobs", jobs)
    assert workbench.main([
        "player", "stop", "--controller", controller, "--port", "mock-port",
        "--mac", "aa:bb:cc:dd:ee:ff",
    ]) == 0
    jobs.assert_called_once()
    manager.start.assert_called_once_with(
        "player-control", None, port="mock-port", expected_mac="aa:bb:cc:dd:ee:ff",
        command="stop", controller=controller,
    )
    manager.get.assert_called_once_with("stop-job")
    launcher.probe.assert_not_called()
    launcher.run.assert_not_called()
    launcher.forbidden.assert_not_called()


@pytest.mark.parametrize(
    ("controller", "required_flag"),
    [("usb-s3", "--hid-disconnected"), ("ble-esp32", "--maintenance-confirmed")],
)
@pytest.mark.parametrize("action", ["upload", "arm", "desktop"])
def test_cli_non_stop_commands_still_require_physical_confirmation(
    launcher, monkeypatch, capsys, controller, required_flag, action,
):
    jobs = Mock(side_effect=AssertionError("unconfirmed action must not reach Jobs"))
    monkeypatch.setattr(workbench, "Jobs", jobs)
    project = str(launcher.data / "must-not-be-read.json")
    command = ["upload", project] if action == "upload" else ["player", action, "--project", project]
    assert workbench.main([
        *command, "--controller", controller, "--port", "mock-port", "--mac", "aa:bb:cc:dd:ee:ff",
    ]) == 2
    assert required_flag in capsys.readouterr().err
    jobs.assert_not_called()
    launcher.probe.assert_not_called()
    launcher.run.assert_not_called()
    launcher.forbidden.assert_not_called()
