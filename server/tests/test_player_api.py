import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_player import MAC, plan

from hidagent.player import validate_recording
from hidagent.player_api import register
from hidagent.studio import Jobs, Store


def setup(tmp_path, monkeypatch):
    app = FastAPI()
    store = Store(tmp_path / "studio")
    jobs = Jobs(store)
    calls = []
    monkeypatch.setattr(jobs, "start", lambda *args, **kwargs: calls.append((args, kwargs)) or {"id": "b" * 32})
    store.save(plan())
    register(app, store, jobs, lambda: None)
    return TestClient(app), calls


def test_upload_only_queues_explicitly_confirmed_identity_and_project(tmp_path, monkeypatch):
    client, calls = setup(tmp_path, monkeypatch)
    body = dict(project_id=plan().id, port="/dev/mock", expected_mac=MAC)
    assert client.post("/api/studio/player/upload", json=body).status_code == 422
    body["hid_disconnected"] = False
    assert client.post("/api/studio/player/upload", json=body).status_code == 422
    assert not calls
    body["hid_disconnected"] = True
    assert client.post("/api/studio/player/upload", json=body).status_code == 200
    assert calls[0][0][0] == "upload"
    assert calls[0][1]["expected_mac"] == MAC


def test_arm_requires_selected_plan_and_unknown_commands_rejected(tmp_path, monkeypatch):
    client, calls = setup(tmp_path, monkeypatch)
    body = dict(command="arm", port="/dev/mock", expected_mac=MAC, hid_disconnected=True)
    assert client.post("/api/studio/player/control", json=body).status_code == 422
    body["project_id"] = plan().id
    assert client.post("/api/studio/player/control", json=body).status_code == 200
    body["command"] = "erase"
    assert client.post("/api/studio/player/control", json=body).status_code == 422
    assert len(calls) == 1


def test_recording_download_contains_no_screen_or_network_configuration(tmp_path, monkeypatch):
    client, calls = setup(tmp_path, monkeypatch)
    response = client.get(f"/api/studio/projects/{plan().id}/recording.amr")
    assert response.status_code == 200
    assert validate_recording(response.content)["steps"] == 1
    assert not calls


@pytest.mark.parametrize("controller", ["usb-s3", "ble-esp32"])
def test_stop_does_not_require_preexisting_maintenance_confirmation(tmp_path, monkeypatch, controller):
    client, calls = setup(tmp_path, monkeypatch)
    body = dict(command="stop", port="/dev/mock", expected_mac=MAC, controller=controller)
    assert client.post("/api/studio/player/control", json=body).status_code == 200
    assert calls[-1][1]["command"] == "stop" and calls[-1][1]["expected_mac"] == MAC
    body["expected_mac"] = "invalid"
    assert client.post("/api/studio/player/control", json=body).status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize("command", ["upload", "arm", "desktop"])
def test_ble_stop_exception_does_not_weaken_other_api_gates(tmp_path, monkeypatch, command):
    client, calls = setup(tmp_path, monkeypatch)
    body = dict(port="/dev/mock", expected_mac=MAC, controller="ble-esp32", project_id=plan().id)
    endpoint = "/api/studio/player/upload" if command == "upload" else "/api/studio/player/control"
    if command != "upload":
        body["command"] = command
    assert client.post(endpoint, json=body).status_code == 422
    assert not calls
    body["maintenance_confirmed"] = True
    assert client.post(endpoint, json=body).status_code == 200
