"""Desktop macro tests: synthetic frames and fakes only; no service or device IO."""

import asyncio
import io
import json
import time

import pytest
from PIL import Image, ImageDraw

from hidagent import macro as module
from hidagent.macro import Macro


def frame(*, changed=False, quality=90, size=(320, 640)):
    image = Image.new("RGB", size, (235, 238, 242))
    draw = ImageDraw.Draw(image)
    w, h = size
    draw.rectangle((w * .34, h * .45, w * .66, h * .55), fill=(35, 100, 190))
    draw.text((w * .42, h * .49), "OPEN", fill="white")
    if changed:
        draw.rectangle((w * .43, h * .48, w * .57, h * .52), fill=(220, 25, 30))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def with_pointer(jpeg, x, y, radius=9):
    with Image.open(io.BytesIO(jpeg)) as source:
        image = source.convert("RGB")
    cx, cy = x * image.width, y * image.height
    ImageDraw.Draw(image).ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                                 fill=(150, 150, 150), outline=(45, 45, 45), width=2)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=70)
    return buf.getvalue()


class Screen:
    def __init__(self, events):
        self.frame = frame()
        self.live = True
        self.updated = time.time() - 1
        self.events = events

    async def settled(self, **kw):
        assert 0 < kw["least"] <= kw["most"] <= 10
        self.events.append("settled")
        await asyncio.sleep(0)


class Device:
    connected = True

    def __init__(self, events):
        self.events = events

    async def send(self, command):
        assert command == "release"  # cleanup must never home/move
        self.events.append(command)


class Phone:
    calibrated = True
    precision_calibrated = True

    def __init__(self):
        self.events = []
        self.screen = Screen(self.events)
        self.device = Device(self.events)
        self.calls = []
        self.fail = None
        self.after = None

    async def _gesture(self, name, args, aim, strict):
        assert aim is True and strict is True
        self.calls.append((name, args))
        self.events.append("locate")
        if isinstance(self.fail, Exception):
            raise self.fail
        if self.fail == "missing":
            return None
        if self.fail == "hang":
            await asyncio.Event().wait()
        self.events.append(name)
        if self.after:
            self.after()
        return args[0], args[1], 0, 0

    async def tap(self, x, y, *, aim, strict):
        return await self._gesture("click", (x, y), aim, strict)

    async def swipe(self, x1, y1, x2, y2, *, aim, strict, hold=True):
        return await self._gesture("swipe", (x1, y1, x2, y2), aim, strict)

    async def scroll(self, x, y, lines, *, aim, strict):
        return await self._gesture("scroll", (x, y, lines), aim, strict)

    async def type(self, text):
        self.events.append(("type", text))

    async def key(self, text):
        self.events.append(("key", text))


def macro(tmp_path, steps=None, **kw):
    path = tmp_path / "macro.json"
    path.write_text(json.dumps(steps or [{"do": "tap", "x": .5, "y": .5}]))
    return Macro(path, **kw)


def replay(m, phone, loops=1):
    async def run():
        m.play(phone, loops=loops)
        await m.wait_stopped()
    asyncio.run(run())


def trace(m):
    return [json.loads(line) for line in (module.Path(m.trace) / "steps.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("failure", ["missing", RuntimeError("找不到指针")])
def test_localization_failure_never_clicks_or_advances(tmp_path, failure):
    m = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5}, {"do": "key", "text": "enter"}])
    phone = Phone()
    phone.fail = failure
    replay(m, phone)
    assert phone.events == ["settled", "locate", "release"]
    assert len(phone.calls) == 1
    assert m.state["paused"] and "指针" in m.state["last_error"]
    assert not m.playing
    assert len(trace(m)) == 1 and trace(m)[0]["status"] == "error"


def test_localization_timeout_stops_before_next_action_and_releases(tmp_path):
    m = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5}] * 2, action_timeout=.01)
    phone = Phone()
    phone.fail = "hang"
    replay(m, phone)
    assert phone.events == ["settled", "locate", "release"]
    assert "超时" in m.last_error


@pytest.mark.parametrize("condition", ["screen", "live", "frame", "calibrated", "precision", "connected", "bad_jpeg"])
def test_play_requires_live_decodable_frame_calibration_and_connection(tmp_path, condition):
    m, phone = macro(tmp_path), Phone()
    if condition == "screen":
        phone.screen = None
    elif condition == "connected":
        phone.device.connected = False
    elif condition == "calibrated":
        phone.calibrated = False
    elif condition == "precision":
        phone.precision_calibrated = False
    elif condition == "bad_jpeg":
        phone.screen.frame = b"broken jpeg"
    else:
        setattr(phone.screen, condition, None if condition == "frame" else False)
    with pytest.raises(RuntimeError):
        replay(m, phone)
    assert not phone.calls and not m.playing
    assert "无法开始回放" in m.last_error


@pytest.mark.parametrize("lost", ["frame", "live", "connected", "calibrated", "precision_calibrated"])
def test_lost_readiness_mid_replay_stops_next_step(tmp_path, lost):
    m = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5}] * 2)
    phone = Phone()

    def disconnect():
        target = phone.device if lost == "connected" else phone if "calibrated" in lost else phone.screen
        setattr(target, lost, None if lost == "frame" else False)

    phone.after = disconnect
    replay(m, phone)
    assert len(phone.calls) == 1
    assert m.paused and m.last_error
    assert phone.events[-1] == "release"


def test_old_list_format_all_gestures_are_strict_and_loops_are_sequential(tmp_path):
    steps = [{"do": "tap", "x": .5, "y": .5},
             {"do": "swipe", "x1": .3, "y1": .6, "x2": .3, "y2": .2},
             {"do": "scroll", "x": .5, "y": .5, "lines": -2},
             {"do": "home"}, {"do": "back"}, {"do": "type", "text": "abc"},
             {"do": "key", "text": "enter"}]
    m, phone = macro(tmp_path, steps), Phone()
    replay(m, phone, loops=3)
    assert len(phone.calls) == 15
    assert phone.events.count("settled") == 21
    assert phone.events.count("release") == 1
    assert phone.events.count(("key", "enter")) == 3
    assert not m.state["guarded"] and not m.last_error
    assert not m.playing and m.loop == 0 and m.loops == 3
    entries = trace(m)
    assert [(e["round"], e["i"]) for e in entries] == [(r, i) for r in range(1, 4) for i in range(7)]
    assert all(e["guarded"] is False and e["status"] == "ok" for e in entries)
    assert set(("recording", "playing", "loop", "loops", "trace")) <= m.state.keys()
    assert not hasattr(m, "upload") and not hasattr(m, "board_state")


def test_guard_round_trip_tolerates_jpeg_but_rejects_changed_target(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=.5, y=.5, frame=frame(quality=95))
    m.record("key", text="enter")
    m.record_stop()
    m = Macro(tmp_path / "macro.json")
    phone.screen.frame = frame(quality=45)
    replay(m, phone)
    assert not m.last_error and trace(m)[0]["guarded"] is True
    assert m.state["guarded"] is True
    phone = Phone()
    phone.screen.frame = frame(changed=True)
    replay(m, phone)
    assert not phone.calls and ("key", "enter") not in phone.events
    assert "界面已变化" in m.last_error and m.paused


@pytest.mark.parametrize("do,kw", [("swipe", dict(x1=.5, y1=.5, x2=.5, y2=.2)),
                                  ("scroll", dict(x=.5, y=.5, lines=2))])
def test_swipe_and_scroll_also_have_page_guards(tmp_path, do, kw):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record(do, frame=frame(), **kw)
    m.record_stop()
    phone.screen.frame = frame(changed=True)
    replay(m, phone)
    assert not phone.calls and "界面已变化" in m.last_error


def test_guard_rejects_rotation_and_corruption(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=.5, y=.5, frame=frame())
    m.record_stop()
    phone.screen.frame = frame(size=(640, 320))
    replay(m, phone)
    assert "比例已改变" in m.last_error and not phone.calls
    phone.screen.frame = frame()
    m.steps[0]["guard"]["png"] = "not base64!"
    replay(m, phone)
    assert "验证依据损坏" in m.last_error and not phone.calls


def test_optional_recording_frame_does_not_claim_guarding(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=.5, y=.5)
    m.record_stop()
    replay(m, phone)
    assert len(phone.calls) == 1 and not m.state["guarded"]
    assert trace(m)[0]["guarded"] is False


@pytest.mark.parametrize("old_position,new_position", [(None, (.5, .5)), ((.5, .5), None),
                                                      ((.44, .5), (.56, .5))])
def test_guard_tolerates_small_mouse_circles_without_ignoring_entire_target(tmp_path, old_position, new_position):
    m, phone = macro(tmp_path), Phone()
    before = with_pointer(frame(), *old_position) if old_position else frame()
    after = with_pointer(frame(), *new_position) if new_position else frame()
    m.record_start()
    m.record("tap", x=.5, y=.5, frame=before)
    m.record_stop()
    phone.screen.frame = after
    replay(m, phone)
    assert not m.last_error and len(phone.calls) == 1


def test_guard_still_checks_target_changes_with_pointer_present(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=.5, y=.5, frame=with_pointer(frame(), .42, .5))
    m.record_stop()
    phone.screen.frame = with_pointer(frame(changed=True), .58, .5)
    replay(m, phone)
    assert not phone.calls and "界面已变化" in m.last_error


def test_page_guard_is_checked_again_on_every_step(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=.5, y=.5, frame=frame())
    m.record("tap", x=.5, y=.5, frame=frame())
    m.record("key", text="enter")
    m.record_stop()
    phone.after = lambda: setattr(phone.screen, "frame", frame(changed=True))
    replay(m, phone)
    assert len(phone.calls) == 1 and ("key", "enter") not in phone.events
    assert "第 2 步" in m.last_error and "界面已变化" in m.last_error
    assert [entry["status"] for entry in trace(m)] == ["ok", "error"]


@pytest.mark.parametrize("do", ["home", "back"])
def test_edge_system_gestures_never_bypass_strict_localization_failure(tmp_path, do):
    m, phone = macro(tmp_path, [{"do": do}, {"do": "key", "text": "enter"}]), Phone()
    phone.fail = RuntimeError("屏幕边缘无法确认指针")
    replay(m, phone)
    assert len(phone.calls) == 1 and phone.calls[0][0] == "swipe"
    assert phone.events == ["settled", "locate", "release"]
    assert "屏幕边缘无法确认指针" in m.last_error


@pytest.mark.parametrize("xy", [(0, 0), (1, 1), (.005, .45), (.5, .995)])
def test_recorded_guard_crops_safely_at_screen_edges(tmp_path, xy):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record("tap", x=xy[0], y=xy[1], frame=frame())
    m.record_stop()
    replay(m, phone)
    assert len(phone.calls) == 1 and not m.last_error


@pytest.mark.parametrize("aim", [(float("nan"), .5, 0, 0), (.5,), (-1, .5, 0, 0), False])
def test_invalid_pointer_confirmation_never_advances(tmp_path, aim):
    m, phone = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5}, {"do": "key", "text": "enter"}]), Phone()

    async def bad_confirmation(*_, **kw):
        assert kw == {"aim": True, "strict": True}
        return aim

    phone.tap = bad_confirmation
    replay(m, phone)
    assert "指针定位失败" in m.last_error
    assert ("key", "enter") not in phone.events and phone.events[-1] == "release"


def test_invalid_gesture_coordinates_stop_before_phone_call(tmp_path):
    m, phone = macro(tmp_path, [{"do": "swipe", "x1": .5, "y1": .5, "x2": 2, "y2": .1}]), Phone()
    replay(m, phone)
    assert not phone.calls and "坐标无效" in m.last_error


def test_empty_cancel_failed_recording_and_failed_commit_preserve_old_macro(tmp_path, monkeypatch):
    m = macro(tmp_path)
    path = tmp_path / "macro.json"
    old = path.read_bytes()
    m.record_start()
    assert path.read_bytes() == old
    m.record_stop()
    assert path.read_bytes() == old and len(m.steps) == 1
    m.record_start()
    with pytest.raises(RuntimeError, match="无法解码"):
        m.record("tap", x=.5, y=.5, frame=b"broken")
    assert not m.steps and path.read_bytes() == old
    m.record("tap", x=.25, y=.25, frame=frame())
    assert path.read_bytes() == old  # draft isn't a destructive replacement

    def fail_replace(*_):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        m.record_stop()
    assert m.recording and path.read_bytes() == old
    assert "旧宏已保留" in m.last_error
    assert not list(tmp_path.glob(".macro.json.*"))
    m.record_cancel()
    assert m.steps == json.loads(old) and not m.recording


def test_record_monotonic_intervals_and_undo(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    m = macro(tmp_path)
    m.record_start()
    now[0] += 2.5
    m.record("key", text="a")
    now[0] += 3.25
    m.record("key", text="b")
    assert [s["wait"] for s in m.steps] == [2.5, 3.25]
    m.undo()
    now[0] += 1.0
    m.record("key", text="c")
    assert [s["wait"] for s in m.steps] == [2.5, 4.25]
    m.record_stop()
    assert [s["text"] for s in Macro(tmp_path / "macro.json").steps] == ["a", "c"]


def test_play_retains_minimum_start_to_start_interval_and_settles_before_locating(tmp_path):
    m = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5, "wait": .025}] * 2)
    phone = Phone()
    times = []
    phone.after = lambda: times.append(time.monotonic())
    replay(m, phone)
    assert times[1] - times[0] >= .024
    assert phone.events == ["settled", "locate", "click", "settled", "locate", "click", "release"]


def test_settle_timeout_or_unstable_screen_prevents_any_action(tmp_path):
    m, phone = macro(tmp_path, settle_timeout=.01), Phone()

    async def unstable(**_):
        return False

    phone.screen.settled = unstable
    replay(m, phone)
    assert "稳定超时" in m.last_error and not phone.calls

    async def hangs(**_):
        await asyncio.Event().wait()

    phone.screen.settled = hangs
    replay(m, phone)
    assert "稳定超时" in m.last_error and not phone.calls
    assert phone.events.count("release") == 2


@pytest.mark.parametrize("when", ["before_start", "settle", "action", "delay"])
def test_sync_stop_always_releases_and_wait_stopped_joins_cleanup(tmp_path, when):
    m = macro(tmp_path, [{"do": "tap", "x": .5, "y": .5, "wait": 5 if when == "delay" else 0}])
    phone = Phone()

    async def run():
        entered = asyncio.Event()

        async def wait(**_):
            entered.set()
            await asyncio.Event().wait()

        if when == "settle":
            phone.screen.settled = wait
        elif when == "action":
            async def tap(*_, **kw):
                assert kw == {"aim": True, "strict": True}
                entered.set()
                await asyncio.Event().wait()
            phone.tap = tap
        m.play(phone, loops=0)
        if when in ("settle", "action"):
            await entered.wait()
        elif when == "delay":
            await asyncio.sleep(0)
        assert m.stop() is None
        m.stop()  # repeated cancellation must not interrupt release
        assert m.playing and m.state["stopping"]
        await m.wait_stopped()
        assert not m.playing and phone.events[-1] == "release"
        assert not phone.calls and not m.last_error
    asyncio.run(run())


def test_cancelled_api_waiter_and_repeated_stop_do_not_interrupt_release(tmp_path):
    m, phone = macro(tmp_path), Phone()

    async def run():
        releasing, finish = asyncio.Event(), asyncio.Event()

        async def release(command):
            assert command == "release"
            releasing.set()
            await finish.wait()
            phone.events.append("release")

        phone.device.send = release
        m.play(phone, loops=1)
        await releasing.wait()
        waiter = asyncio.create_task(m.wait_stopped())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        m.stop()
        m.stop()
        assert m.playing
        finish.set()
        await m.wait_stopped()
        assert phone.events[-1] == "release" and not m.playing
    asyncio.run(run())


def test_infinite_loops_stop_and_release_failure_are_reported(tmp_path):
    m, phone = macro(tmp_path), Phone()

    async def run():
        rounds = []

        def after():
            rounds.append(m.loop)
            if len(rounds) == 3:
                m.stop()

        phone.after = after
        m.play(phone, loops=0)
        await m.wait_stopped()
        assert rounds == [1, 2, 3]
    asyncio.run(run())
    assert phone.events[-1] == "release"

    async def broken(_):
        raise RuntimeError("连接已断开")

    phone.device.send = broken
    phone.after = None
    replay(m, phone)
    assert "释放按键失败" in m.last_error and not m.playing


def test_release_has_bounded_timeout(tmp_path):
    m, phone = macro(tmp_path, release_timeout=.01), Phone()

    async def hang(_):
        await asyncio.Event().wait()

    phone.device.send = hang
    replay(m, phone)
    assert "释放按键失败" in m.last_error and not m.playing


def test_cannot_record_or_clear_during_replay_or_play_during_recording(tmp_path):
    m, phone = macro(tmp_path), Phone()

    async def run():
        m.play(phone, loops=1)
        with pytest.raises(RuntimeError):
            m.record_start()
        with pytest.raises(RuntimeError):
            m.clear()
        with pytest.raises(RuntimeError):
            m.play(phone)
        m.stop()
        await m.wait_stopped()
        m.record_start()
        with pytest.raises(RuntimeError):
            m.play(phone)
        m.record_cancel()
    asyncio.run(run())


def list_frame(*, names='OLD', selected=False, wrong_page=False, changed_button=False):
    image = Image.new('RGB', (600, 1000), '#111111')
    draw = ImageDraw.Draw(image)
    if not wrong_page:
        for y in (200, 280):
            draw.ellipse((261, y-9, 279, y+9), outline='#555555', fill='#2299ee' if selected else '#111111')
            draw.rectangle((290, y-15, 310, y+15), fill='#ddddff')
            draw.rectangle((296, y-7, 304, y+7), fill='#77aaff')
            draw.text((340, y-12), names, fill='white', font_size=24)
    draw.rectangle((510, 875, 570, 925), fill='#334488' if changed_button else '#bb3333')
    return image


def list_recording(tmp_path):
    m = macro(tmp_path)
    encoded = io.BytesIO()
    list_frame().save(encoded, 'JPEG')
    m.record_start()
    for y in (.2, .28):
        m.record('tap', x=.5, y=y, frame=encoded.getvalue())
    m.record('tap', x=.9, y=.9, frame=encoded.getvalue())
    m.record_stop()
    return m


def test_list_loop_allows_new_names_but_preserves_icons_selection_and_button_guards(tmp_path):
    m = list_recording(tmp_path)
    original = json.loads((tmp_path / 'macro.json').read_text())
    with pytest.raises(RuntimeError, match='界面已变化'):
        Macro._check_guard(m.steps[0], list_frame(names='MMMMMM'))
    m.set_list_mode(True)
    assert m.state['list_mode']
    assert [s['guard'].get('policy', 'exact') for s in m.steps] == ['list_row', 'list_row', 'exact']
    m = Macro(tmp_path / 'macro.json')
    assert m.state['list_mode']
    assert Macro._check_guard(m.steps[0], list_frame(names='NEXT-BATCH'))
    for image in (list_frame(selected=True), list_frame(wrong_page=True)):
        with pytest.raises(RuntimeError, match='行位置、选择状态或图标'):
            Macro._check_guard(m.steps[0], image)
    with pytest.raises(RuntimeError, match='界面已变化'):
        Macro._check_guard(m.steps[2], list_frame(changed_button=True))
    m.set_list_mode(False)
    assert not m.state['list_mode'] and m.steps == original


def test_list_mode_requires_an_opening_run_of_rows_and_does_not_modify_on_failure(tmp_path):
    m = macro(tmp_path, [{'do':'tap','x':.5,'y':.2}])
    before = m._file.read_bytes()
    with pytest.raises(RuntimeError, match='至少两行'):
        m.set_list_mode(True)
    assert m._file.read_bytes() == before
    m.record_start()
    with pytest.raises(RuntimeError, match='结束录制'):
        m.set_list_mode(True)


def test_guard_failure_keeps_its_before_screenshot_for_diagnosis(tmp_path):
    m, phone = macro(tmp_path), Phone()
    m.record_start()
    m.record('tap', x=.5, y=.5, frame=frame())
    m.record_stop()
    phone.screen.frame = frame(changed=True)
    replay(m, phone)
    entry = trace(m)[0]
    assert entry['status'] == 'error' and not phone.calls
    assert (module.Path(m.trace) / entry['before']).read_bytes() == phone.screen.frame
