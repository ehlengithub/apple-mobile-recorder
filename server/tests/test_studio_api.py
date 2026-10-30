import asyncio
import io
import json
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from hidagent.macro import Macro
from hidagent.studio import atomic_json
from hidagent.studio_api import register


@pytest.fixture
def studio_client(tmp_path):
    app = FastAPI()
    macro = types.SimpleNamespace(
        recording=False,
        playing=False,
        steps=[{"do": "tap", "x": 0.3, "y": 0.4, "wait": 1, "guard": {"aspect": 754 / 1080, "png": "PRIVATE"}}],
    )
    screen = types.SimpleNamespace(live=False, frame=None)
    store, jobs = register(
        app, tmp_path, types.SimpleNamespace(connected=False), screen, types.SimpleNamespace(), macro, lambda: None
    )
    return TestClient(app), store, jobs, macro


def request_data():
    return {"name": "我的录制", "device": {"name": "我的iPad", "family": "ipad", "width": 754, "height": 1080}}


def test_capture_edit_and_portable_export_have_no_screenshots(studio_client):
    client, store, jobs, macro = studio_client
    response = client.post("/api/studio/capture", json=request_data())
    assert response.status_code == 200
    project = response.json()
    assert "PRIVATE" not in response.text
    project["steps"][0]["wait_ms"] = 500
    response = client.put(f"/api/studio/projects/{project['id']}", json=project)
    assert response.status_code == 200
    assert store.get(project["id"]).cycle_ms == 500
    assert (store.projects / project["id"] / "previous.json").exists()
    assert client.get("/api/studio/projects").json()[0]["device"]["family"] == "ipad"


def test_busy_capture_and_cross_device_rebinding_are_refused(studio_client):
    client, store, jobs, macro = studio_client
    macro.recording = True
    assert client.post("/api/studio/capture", json=request_data()).status_code == 409
    macro.recording = False
    project = client.post("/api/studio/capture", json=request_data()).json()
    project["device"].update(family="iphone", name="另一台iPhone", width=390, height=844)
    assert client.put(f"/api/studio/projects/{project['id']}", json=project).status_code == 422
    assert store.get(project["id"]).device.family == "ipad"


def test_import_cannot_overwrite_existing_guards_or_replay_on_wrong_device(studio_client):
    client, store, jobs, macro = studio_client
    project = client.post("/api/studio/capture", json=request_data()).json()
    response = client.post("/api/studio/import", json=project)
    assert response.status_code == 200
    imported = response.json()
    assert imported["id"] != project["id"]
    assert not (store.projects / imported["id"] / "recording.json").exists()
    assert client.post(f"/api/studio/projects/{imported['id']}/play").status_code == 409


def test_unsupported_actions_and_subsecond_physical_limit_are_not_silently_dropped(studio_client):
    client, store, jobs, macro = studio_client
    project = client.post("/api/studio/capture", json=request_data()).json()
    project["steps"][0]["wait_ms"] = 100
    assert client.put(f"/api/studio/projects/{project['id']}", json=project).status_code == 422
    project["steps"][0].update(do="swipe", wait_ms=1000)
    assert client.post("/api/studio/import", json=project).status_code == 422


def test_portable_plan_cannot_expose_arbitrary_files(studio_client):
    client, store, jobs, macro = studio_client
    assert client.get("/api/studio/jobs/not-a-valid-id").status_code == 404
    assert client.get("/api/studio/projects/not-a-valid-id").status_code == 404


def test_external_websites_cannot_start_local_firmware_jobs():
    from hidagent.app import app

    client = TestClient(app)
    response = client.post(
        "/api/studio/build", json={"mode": "desktop"}, headers={"origin": "https://external.example"}
    )
    assert response.status_code == 403
    response = client.post(
        "/api/studio/build",
        json={"mode": "desktop"},
        headers={"origin": "http://127.0.0.1:8990", "sec-fetch-site": "cross-site"},
    )
    assert response.status_code == 403


def test_project_play_runs_real_macro_on_request_event_loop(tmp_path):
    async def run():
        image = Image.new("RGB", (754, 1080), "white")
        frame = io.BytesIO()
        image.save(frame, "PNG")
        events = []

        async def settled(**kwargs):
            events.append("settled")

        async def tap(x, y, *, aim, strict):
            assert aim and strict
            events.append(("tap", x, y))
            return x, y, 0, 0

        async def send(command):
            events.append(command)

        device = types.SimpleNamespace(connected=True, absolute=True, send=send)
        screen = types.SimpleNamespace(live=True, frame=frame.getvalue(), aspect=754 / 1080, settled=settled)
        phone = types.SimpleNamespace(
            device=device, screen=screen, calibrated=True, precision_calibrated=True, tap=tap
        )
        macro = Macro(tmp_path / "macro.json")
        macro.steps = [{"do": "tap", "x": 0.3, "y": 0.4, "wait": 0.5,
                        "guard": Macro._make_guard(image, (0.3, 0.4))}]
        app = FastAPI()
        register(app, tmp_path, device, screen, phone, macro, lambda: None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            project = (await client.post("/api/studio/capture", json=request_data())).json()
            response = await client.post(f"/api/studio/projects/{project['id']}/play")
            assert response.status_code == 200, response.text
            assert response.json() == {"ok": True, "loops": 1}
            assert macro._task.get_loop() is asyncio.get_running_loop()
            await asyncio.wait_for(macro.wait_stopped(), 3)
        assert not macro.playing and not macro.last_error
        assert events == ["settled", ("tap", 0.3, 0.4), "release"]
        trace = [json.loads(line) for line in (Path(macro.trace) / "steps.jsonl").read_text().splitlines()]
        assert len(trace) == 1 and trace[0]["guarded"] and trace[0]["status"] == "ok"

    asyncio.run(run())


def history_job(store, identity, *, kind="flash", status="succeeded", created=1, backup=False):
    job = {"id": identity, "kind": kind, "status": status, "created": created, "stage": "完成",
           "backup_ready": backup, "board_mac": "00:11:22:33:44:55", "log": "PRIVATE-LOG",
           "network": {"password": "PRIVATE-PASSWORD"}, "manifest": {"mode": "offline"}}
    atomic_json(store.jobs / identity / "job.json", job)
    if backup:
        folder = store.jobs / identity / "backup"
        folder.mkdir()
        for name in ("restore.json", "application.bin", "boot-and-partitions.bin"):
            (folder / name).write_text("mock-backup")
    return job


def test_history_survives_fresh_api_registration_and_restores_selected_backup(studio_client, monkeypatch):
    client, store, jobs, macro = studio_client
    older = history_job(store, "a" * 32, created=1, backup=True)
    newer = history_job(store, "b" * 32, status="failed", created=2, backup=True)
    history_job(store, "c" * 32, kind="build", created=3)
    first = client.get("/api/studio/jobs")
    assert first.status_code == 200
    assert [job["id"] for job in first.json()] == ["c" * 32, newer["id"], older["id"]]
    assert "PRIVATE" not in first.text

    fresh_app = FastAPI()
    _, fresh_jobs = register(
        fresh_app, store.root.parent, types.SimpleNamespace(connected=False),
        types.SimpleNamespace(live=False, frame=None), types.SimpleNamespace(), macro, lambda: None
    )
    fresh = TestClient(fresh_app)
    records = fresh.get("/api/studio/jobs").json()
    assert records == first.json()
    assert all(job["backup_available"] for job in records if job["kind"] == "flash")
    calls = []

    def start(kind, **options):
        calls.append((kind, options))
        return {"id": "d" * 32, "kind": kind, "status": "queued"}

    monkeypatch.setattr(fresh_jobs, "start", start)
    response = fresh.post("/api/studio/restore", json={"flash_id": older["id"], "port": "/dev/MOCK-ONLY"})
    assert response.status_code == 200
    assert calls == [("restore", {"flash_id": older["id"], "port": "/dev/MOCK-ONLY"})]
    assert fresh.get(f"/api/studio/jobs/{newer['id']}").json()["status"] == "failed"


def test_history_skips_corruption_and_reports_missing_backups_and_live_workers(studio_client):
    client, store, jobs, macro = studio_client
    missing = history_job(store, "a" * 32, backup=True)
    (store.jobs / missing["id"] / "backup/application.bin").unlink()
    running = history_job(store, "b" * 32, kind="build", status="running", created=2)
    history_job(store, "c" * 32, kind="upload", created=3)
    history_job(store, "d" * 32, kind="player-control", created=4)
    atomic_json(store.jobs / ("e" * 32) / "job.json", [])
    atomic_json(store.jobs / "invalid" / "job.json", {"id": "invalid", "kind": "flash"})
    broken = store.jobs / ("f" * 32) / "job.json"
    broken.parent.mkdir()
    broken.write_text("{")
    jobs.states[running["id"]] = running
    jobs.lock.acquire()
    try:
        records = {job["id"]: job for job in client.get("/api/studio/jobs").json()}
        assert len(records) == 4
        assert records[running["id"]]["running_here"]
        assert records[missing["id"]]["backup_ready"]
        assert not records[missing["id"]]["backup_available"]
    finally:
        jobs.lock.release()
    assert not any(job["running_here"] for job in client.get("/api/studio/jobs").json())


def test_history_returns_player_metadata_without_recording_body(studio_client):
    client, store, jobs, macro = studio_client
    job = history_job(store, "a" * 32, kind="upload")
    job.update(recording={"size": 64, "steps": 1, "crc": "1234abcd", "data": "PRIVATE-BODY"},
               project_id="b" * 32, command="arm")
    atomic_json(store.jobs / job["id"] / "job.json", job)
    response = client.get("/api/studio/jobs")
    assert response.json()[0]["recording"] == {"size": 64, "steps": 1, "crc": "1234abcd"}
    assert response.json()[0]["project_id"] == "b" * 32
    assert response.json()[0]["command"] == "arm"
    assert "PRIVATE" not in response.text


def test_universal_build_accepts_no_project_and_optional_network(studio_client, monkeypatch):
    client, store, jobs, macro = studio_client
    calls = []

    def start(kind, project=None, **options):
        calls.append((kind, project, options))
        return {"id": "a" * 32, "kind": kind, "status": "queued"}

    monkeypatch.setattr(jobs, "start", start)
    assert client.post("/api/studio/build", json={"mode": "offline"}).status_code == 200
    network = {"ssid": "mock-wifi", "password": "mock-secret", "host": "192.0.2.10", "port": 7400}
    assert client.post("/api/studio/build", json={"mode": "offline", "network": network}).status_code == 200
    assert calls == [("build", None, {"mode": "offline", "network": None}),
                     ("build", None, {"mode": "offline", "network": network})]
