import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
