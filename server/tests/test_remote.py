import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from hidagent import cli
from hidagent.client import IPhoneClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HIDAGENT_DATA", str(tmp_path))
    module = importlib.import_module("hidagent.app")
    calls = []

    class Phone:
        span = {"w": 400, "h": 800}
        calibrated = True
        precision_calibrated = True
        grid_points = 9
        async def tap(self, x, y, aim=False, strict=False):
            calls.append(("tap", x, y, "aim", strict) if aim else ("tap", x, y))
        async def type(self, text): calls.append(("type", text))
        async def key(self, text): calls.append(("key", text))
        async def start_calibration(self): return True
        def forget_position(self): calls.append(("forget",))
        def finish_calibration(self, x, y): calls.append(("calibrate", x, y))
        async def calibrate_grid(self): return True

    class Agent:
        running, task, steps = False, "", []
        trace = SimpleNamespace(dir=None)
        actor = planner = SimpleNamespace(model="fake")
        def start(self, goal): self.running, self.task = True, goal
        def stop(self): self.running = False

    monkeypatch.setattr(module, "phone", Phone())
    monkeypatch.setattr(module, "agent", Agent())
    monkeypatch.setattr(module, "device", SimpleNamespace(connected=False))
    monkeypatch.setattr(module, "screen", SimpleNamespace(live=True, frame=b"jpeg", aspect=.46))
    monkeypatch.setattr(module, "manual_lock", asyncio.Lock())
    monkeypatch.setattr(module, "manual_task", None)
    monkeypatch.setattr(module, "stop_task", None)
    monkeypatch.setattr(module, "manual_generation", 0)
    monkeypatch.setattr(module, "stopping", False)
    return module, calls


def test_client_uses_legacy_routes_and_single_current_task():
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path, request.content))
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"agent": {"running": False, "task": "", "steps": []}})
        return httpx.Response(200, json={"ok": True})

    with IPhoneClient("http://legacy", transport=httpx.MockTransport(respond)) as client:
        client.delegate("Open Clock")
        assert client.task_status()["running"] is False
        client.stop()
        client.action("key", text="cmd+a")
        client.calibrate(.5, .4)
        with pytest.raises(ValueError):
            client.calibrate(.5)
        with pytest.raises(ValueError):
            client.action("raw", text="home")
    assert [path for _, path, _ in requests] == [
        "/api/agent", "/api/status", "/api/agent/stop", "/api/key", "/api/calibrate/finish"]
    assert requests[0][2] == b'{"text":"Open Clock"}'


def test_cli_keeps_json_on_stdout_and_errors_on_stderr(monkeypatch, capsys):
    def respond(request):
        return httpx.Response(409, json={"detail": "phone is busy"})

    monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(url, transport=httpx.MockTransport(respond)))
    assert cli.main(["--url", "http://legacy", "type", "Calendar"]) == 2
    output = capsys.readouterr()
    assert not output.out and '"error"' in output.err


def test_cli_status_and_screenshot_do_not_import_mcp(monkeypatch, capsys, tmp_path):
    import sys

    monkeypatch.setitem(sys.modules, "mcp", None)

    def respond(request):
        if request.url.path == "/api/frame.jpg":
            return httpx.Response(200, content=b"synthetic jpeg")
        return httpx.Response(200, json={"device": True})

    monkeypatch.setattr(cli, "IPhoneClient", lambda url: IPhoneClient(url, transport=httpx.MockTransport(respond)))
    assert cli.main(["--url", "http://legacy", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"device": True}
    output = tmp_path / "phone.jpg"
    assert cli.main(["--url", "http://legacy", "screenshot", str(output)]) == 0
    assert output.read_bytes() == b"synthetic jpeg"
    assert json.loads(capsys.readouterr().out)["path"] == str(output)


@pytest.mark.parametrize("url", ["file:///tmp/phone", "http://legacy?key=secret", "http://legacy#fragment"])
def test_client_rejects_unsupported_urls(url):
    with pytest.raises(ValueError):
        IPhoneClient(url)


def test_direct_action_and_agent_start_cannot_interleave(api, monkeypatch):
    module, calls = api

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def tap(x, y):
            entered.set()
            await release.wait()
            calls.append(("tap", x, y))

        monkeypatch.setattr(module.phone, "tap", tap)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            pending = asyncio.create_task(client.post("/api/tap", json={"x": .5, "y": .4}))
            await entered.wait()
            for path, body in [("/api/key", {"text": "enter"}), ("/api/agent", {"text": "Open Clock"}),
                               ("/api/calibrate/start", None), ("/api/calibrate/finish", {"x": .5, "y": .4})]:
                assert (await client.post(path, json=body)).status_code == 409
            assert not calls
            assert (await client.post("/api/agent/stop")).status_code == 200
            release.set()
            assert (await pending).status_code == 409
            assert (await client.post("/api/agent", json={"text": "Open Clock"})).status_code == 200
            assert (await client.post("/api/type", json={"text": "Calendar"})).status_code == 409
            assert calls == []

    asyncio.run(scenario())


def test_emergency_stop_waits_for_real_pointer_action_and_never_clicks_after_reset(api, tmp_path, monkeypatch):
    import time

    from hidagent.phone import Phone

    module, _ = api

    async def scenario():
        positioned, cleanup, allow_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
        commands = []

        class Device:
            connected, absolute, links = True, True, 1

            async def send(self, line):
                commands.append(line)

        class Screen:
            live, frame, updated, aspect = True, b"frame", time.time(), .8
            settles = 0

            async def fresh(self, *_):
                return self.frame

            async def settled(self, **_):
                self.settles += 1
                if self.settles == 3:
                    positioned.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleanup.set()
                        await allow_cleanup.wait()

        device, screen = Device(), Screen()
        phone = Phone(device, tmp_path / "calibration.json", screen)
        monkeypatch.setattr(module, "device", device)
        monkeypatch.setattr(module, "screen", screen)
        monkeypatch.setattr(module, "phone", phone)
        monkeypatch.setattr("hidagent.phone.vision.absolute_pointer", lambda *_: (.595, .5))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            pending = asyncio.create_task(client.post("/api/tap", json={"x": .5, "y": .5, "precise": True}))
            await asyncio.wait_for(positioned.wait(), 1)
            stop = asyncio.create_task(client.post("/api/agent/stop"))
            await asyncio.wait_for(cleanup.wait(), 1)
            assert not stop.done() and "home" not in commands
            assert (await client.post("/api/tap", json={"x": .2, "y": .2})).status_code == 409
            assert (await client.post("/api/agent", json={"text": "Open Clock"})).status_code == 409
            allow_cleanup.set()
            assert (await stop).status_code == 200
            at_stop = commands.copy()
            assert (await pending).status_code == 409
            await asyncio.sleep(0)
            assert commands == at_stop and commands[-2:] == ["release", "home"]
            assert "click" not in commands and not module.manual_lock.locked()
            # Stop does not permanently disable explicitly requested later actions.
            assert (await client.post("/api/tap", json={"x": .2, "y": .2})).status_code == 200
            assert commands[-1] == "click"

    asyncio.run(scenario())


def test_cancelled_stop_waiter_does_not_abandon_release(api, monkeypatch):
    module, _ = api

    async def scenario():
        resetting, finish = asyncio.Event(), asyncio.Event()

        async def reset():
            resetting.set()
            await finish.wait()

        monkeypatch.setattr(module, "device", SimpleNamespace(connected=True))
        monkeypatch.setattr(module.phone, "reset", reset, raising=False)
        waiter = asyncio.create_task(module.agent_stop())
        await resetting.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert module.stopping and not module.stop_task.done()
        finish.set()
        await module.stop_task
        assert not module.stopping

    asyncio.run(scenario())


def test_changed_backend_source_blocks_new_actions_but_preserves_emergency_stop(api, monkeypatch):
    module, calls = api
    monkeypatch.setattr(module.runtime_version, "runtime_is_current", lambda: False)
    jobs = []

    def start(kind, plan, **options):
        jobs.append((kind, plan, options))
        return {"id": "e" * 32, "kind": kind, "status": "queued"}

    monkeypatch.setattr(module.studio_jobs, "start", start)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://test") as client:
            blocked = await client.post("/api/tap", json={"x": .5, "y": .4})
            assert blocked.status_code == 409 and "重新启动" in blocked.json()["detail"]
            assert (await client.post("/api/studio/build", json={})).status_code == 409
            assert (await client.get("/api/status")).status_code == 200
            assert (await client.post("/api/agent/stop")).status_code == 200
            assert (await client.post("/api/macro/stop")).status_code == 200
            control = {"port": "/dev/MOCK-ONLY", "expected_mac": "02:00:00:00:00:01",
                       "controller": "ble-esp32", "command": "stop"}
            assert (await client.post("/api/studio/player/control", json=control)).status_code == 200
            assert jobs[0][2]["command"] == "stop"
            control["command"] = "arm"
            assert (await client.post("/api/studio/player/control", json=control)).status_code == 409
            assert len(jobs) == 1
        assert not calls

    asyncio.run(scenario())


def test_precise_tap_finds_the_pointer_first(api):
    module, calls = api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post("/api/tap", json={"x": .5, "y": .4, "precise": True})).status_code == 200
            assert (await client.post("/api/tap", json={"x": .2, "y": .3})).status_code == 200

    asyncio.run(scenario())
    assert calls == [("tap", .5, .4, "aim", True), ("tap", .2, .3)]


def test_recording_routes_replay_in_the_running_event_loop(api, tmp_path, monkeypatch):
    import io
    import time

    from PIL import Image

    from hidagent.macro import Macro

    module, calls = api
    blob = io.BytesIO()
    Image.new("RGB", (300, 600), "white").save(blob, "JPEG")
    module.screen.frame = blob.getvalue()
    module.screen.updated = time.time()
    module.device.connected = True
    module.phone.device, module.phone.screen = module.device, module.screen

    async def settled(**kwargs):
        return True

    async def send(line):
        calls.append((line,))

    async def tap(x, y, aim=False, strict=False):
        calls.append(("tap", x, y, aim, strict))
        return x, y, 0, 0

    module.screen.settled, module.device.send, module.phone.tap = settled, send, tap
    monkeypatch.setattr(module, "macro", Macro(tmp_path / "test-macro.json"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post("/api/macro/record/start")).status_code == 200
            assert (await client.post("/api/tap", json={"x": .5, "y": .4})).status_code == 200
            assert (await client.post("/api/macro/record/stop")).status_code == 200
            assert module.macro.state["guarded"]
            assert (await client.post("/api/macro/play", json={"loops": 2})).status_code == 200
            assert (await client.post("/api/tap", json={"x": .5, "y": .4})).status_code == 409
            await asyncio.wait_for(module.macro.wait_stopped(), 5)
            assert not module.macro.state["playing"] and not module.macro.last_error
    asyncio.run(scenario())
    taps = [c for c in calls if c[0] == "tap"]
    assert len(taps) == 3 and all(c[-2:] == (True, True) for c in taps)


@pytest.mark.parametrize("path,body", [
    ("/api/tap", {"x": 2, "y": .5}),
    ("/api/swipe", {"x1": 0, "y1": 0, "x2": -1, "y2": .5}),
    ("/api/scroll", {"x": .5, "y": .5, "lines": 101}),
    ("/api/type", {"text": "Calendar\nhome"}),
    ("/api/key", {"text": "enter\rhome"}),
    ("/api/type", {"text": "你好\u2028世界"}),
    ("/api/key", {"text": "cmd+你"}),
    ("/api/type", {"text": "\u007f"}),
    ("/api/calibrate/finish", {"x": 0, "y": .5}),
])
def test_invalid_remote_input_is_rejected_before_hid(api, path, body):
    module, calls = api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post(path, json=body)).status_code == 422
        assert not calls

    asyncio.run(scenario())


def test_non_ascii_text_reaches_the_phone(api):
    module, calls = api

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post("/api/type", json={"text": "打开设置 ⚙️"})).status_code == 200

    asyncio.run(scenario())
    assert calls == [("type", "打开设置 ⚙️")]


def test_raw_board_commands_make_the_pointer_position_unknown(api, monkeypatch):
    module, calls = api
    sent = []

    async def send(line):
        sent.append(line)
    monkeypatch.setattr(module, "DEBUG", True)
    monkeypatch.setattr(module, "device", type("D", (), {"send": staticmethod(send), "connected": True})())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post("/api/raw", json={"text": "move 40 0"})).status_code == 200

    asyncio.run(scenario())
    assert sent == ["move 40 0"] and calls == [("forget",)]


def test_raw_board_commands_forget_the_pointer_even_when_the_board_errs(api, monkeypatch):
    # the board may have moved the pointer before the reply timed out
    from hidagent.device import DeviceError
    module, calls = api

    async def send(line):
        raise DeviceError("board did not answer")
    monkeypatch.setattr(module, "DEBUG", True)
    monkeypatch.setattr(module, "device", type("D", (), {"send": staticmethod(send), "connected": True})())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://legacy") as client:
            assert (await client.post("/api/raw", json={"text": "move 40 0"})).status_code == 409

    asyncio.run(scenario())
    assert calls == [("forget",)]


def test_frame_long_poll_waits_for_change_and_keeps_last_image_on_timeout(api):
    module, _ = api
    module.screen.frame_seq = 4

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='http://legacy') as client:
            first = await client.get('/api/frame.jpg')
            assert first.content == b'jpeg' and first.headers['X-Frame-Seq'] == '4'
            unchanged = await client.get('/api/frame.jpg?after=4&wait=0.02')
            assert unchanged.status_code == 204 and not unchanged.content
            pending = asyncio.create_task(client.get('/api/frame.jpg?after=4&wait=0.3'))
            await asyncio.sleep(0.03)
            module.screen.frame, module.screen.frame_seq = b'new jpeg', 5
            changed = await pending
            assert changed.status_code == 200 and changed.content == b'new jpeg'
            assert changed.headers['X-Frame-Seq'] == '5'
            assert (await client.get('/api/frame.jpg?after=100&wait=0.1')).status_code == 200
            module.screen.live = False
            assert (await client.get('/api/frame.jpg?after=5&wait=0.1')).status_code == 404
            assert (await client.get('/api/frame.jpg?wait=10')).status_code == 422

    asyncio.run(scenario())
