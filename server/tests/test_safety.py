"""The phone must not be tapped blindly: a frozen mirror, a mouse the phone ignores or a stray pointer reading
is reported or skipped instead of acted on."""

import asyncio
import time

import pytest

from hidagent import phone as phone_module
from hidagent import vision
from hidagent.phone import Phone


class Screen:
    """A mirrored screen whose frames arrive only when the board's moves reach the phone (if `mirroring`)."""

    def __init__(self, age: float):
        self.frame, self.updated, self.restarted = b"jpeg", time.time() - age, False
        self.mirroring = True

    def show_change(self):
        if self.mirroring:
            self.updated = time.time()

    async def fresh(self, since, timeout=0.8):
        deadline = time.time() + timeout
        while self.updated <= since:
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.01)
        return self.frame

    async def settled(self, **_):
        await asyncio.sleep(0)

    def restart(self):
        self.restarted = True


class Device:
    links = 0

    def __init__(self, screen: Screen, mouse_works: bool = True):
        self.screen, self.mouse_works, self.sent = screen, mouse_works, []

    async def send(self, line):
        self.sent.append((time.time(), line))
        if line.startswith("move") and self.mouse_works:
            self.screen.show_change()
        return "ok"

    @property
    def lines(self):
        return [line for _, line in self.sent]


class Pointer(Device):
    """A board whose pointer stops at the screen's edges, as iOS's does."""

    def __init__(self, screen: Screen, width: int):
        super().__init__(screen)
        self.x, self.width = 0, width

    async def send(self, line):
        if line == "home":
            self.x = 0
        elif line.startswith("move"):
            self.x = min(max(self.x + int(line.split()[1]), 0), self.width)
        return await super().send(line)


class Rendering(Pointer):
    """The phone draws the pointer a moment after a move, so a move undone within that moment shows nothing."""

    def __init__(self, screen: Screen, width: int):
        super().__init__(screen, width)
        self.mouse_works, self.drawn = False, 0  # frames come from render(), not from each move

    async def send(self, line):
        reply = await super().send(line)
        if line.startswith("move"):
            asyncio.get_running_loop().call_later(0.05, self.render)
        return reply

    def render(self):
        if self.x != self.drawn:
            self.drawn, self.screen.updated = self.x, time.time()


@pytest.fixture(autouse=True)
def quick_probe(monkeypatch):
    monkeypatch.setattr(phone_module, "PROBE_WAIT", 0.3)


def make_phone(tmp_path, age, mouse_works=True, mirroring=True):
    screen = Screen(age)
    screen.mirroring = mirroring
    device = Device(screen, mouse_works)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span = {"w": 460, "h": 1000}
    return phone, device, screen


def test_a_still_screen_is_checked_before_tapping(tmp_path):
    phone, device, _ = make_phone(tmp_path, age=30)
    asyncio.run(phone.tap(0.5, 0.5))
    nudge = phone_module.NUDGE
    assert device.lines[:2] == [f"move {nudge} 0", f"move -{nudge} 0"]
    assert device.lines[-1] == "click"


def test_a_recently_updated_screen_is_not_probed(tmp_path):
    phone, device, _ = make_phone(tmp_path, age=0)
    asyncio.run(phone.tap(0.5, 0.5))
    assert f"move {phone_module.NUDGE} 0" not in device.lines


def test_a_frozen_mirror_is_restarted_and_nothing_is_clicked(tmp_path):
    phone, device, screen = make_phone(tmp_path, age=300, mirroring=False)
    with pytest.raises(phone_module.PhoneUnavailable, match="Screen Mirroring"):
        asyncio.run(phone.tap(0.5, 0.5))
    assert screen.restarted
    assert "click" not in device.lines


def test_a_phone_that_was_never_mirrored_is_reported_without_restarting_the_receiver(tmp_path):
    phone, device, screen = make_phone(tmp_path, age=0, mirroring=False)
    screen.updated = 0.0  # no frame has ever arrived: Screen Mirroring was not started
    with pytest.raises(phone_module.PhoneUnavailable, match="Screen Mirroring"):
        asyncio.run(phone.tap(0.5, 0.5))
    assert not screen.restarted
    assert "click" not in device.lines


def test_a_mouse_the_phone_ignores_is_reported_and_nothing_is_clicked(tmp_path):
    # a phone locked a moment ago looks the same (board still answers, nothing changes on screen): say both
    phone, device, screen = make_phone(tmp_path, age=10, mouse_works=False)
    with pytest.raises(phone_module.PhoneUnavailable, match="unlock.*plug"):
        asyncio.run(phone.tap(0.5, 0.5))
    assert not screen.restarted
    assert "click" not in device.lines


def test_typing_is_checked_too(tmp_path):
    phone, device, _ = make_phone(tmp_path, age=10, mouse_works=False)
    with pytest.raises(phone_module.PhoneUnavailable):
        asyncio.run(phone.type("hello"))
    assert not any(line.startswith("type") for line in device.lines)


def test_aim_ignores_a_pointer_reading_far_from_the_target(tmp_path, monkeypatch):
    # a moving clock hand elsewhere on the screen looked like the pointer once
    phone, device, _ = make_phone(tmp_path, age=0)
    monkeypatch.setattr(vision, "pointer", lambda *a, **k: (0.86, 0.23))
    seen_before = {axis: list(v) for axis, v in phone._seen.items()}
    asyncio.run(phone.tap(0.39, 0.48, aim=True))
    after_aim = device.lines[device.lines.index(f"move -{phone_module.NUDGE} 0") + 1:]
    assert after_aim == ["click"]  # no "correction" toward the stray reading
    assert {axis: list(v) for axis, v in phone._seen.items()} == seen_before  # and nothing learned from it


def test_a_forgotten_position_makes_the_next_gesture_start_from_the_corner(tmp_path):
    phone, device, _ = make_phone(tmp_path, age=0)
    asyncio.run(phone.tap(0.5, 0.5))
    phone.forget_position()  # e.g. after raw board commands the server did not track
    device.sent.clear()
    asyncio.run(phone.tap(0.6, 0.6))
    assert device.lines[0] == "home"


def test_the_pointer_settles_after_a_long_move_before_clicking(tmp_path):
    phone, device, _ = make_phone(tmp_path, age=0)
    asyncio.run(phone.tap(0.9, 0.9))  # from the corner: a long move
    times = {line: t for t, line in device.sent}
    last_move = max(t for t, line in device.sent if line.startswith("move"))
    assert times["click"] - last_move >= phone_module.SETTLE_AFTER_LONG_MOVE * 0.9


def test_the_agent_stops_instead_of_retrying_when_the_phone_is_unavailable():
    # retrying cannot unlock the phone or restart Screen Mirroring: it only spends model calls
    from hidagent.agent import Agent
    from hidagent.debug import Trace

    class Mirror:
        live, frame, aspect = True, b"jpeg", 0.46

        async def settled(self, **_):
            pass

    class Unavailable:
        taps = 0

        async def tap(self, x, y, aim=False):
            self.taps += 1
            raise phone_module.PhoneUnavailable("unlock the phone")

    class Model:
        model = "fake"

        async def chat(self, system, text, image=None, max_tokens=600, temperature=0):
            return "THINK: tap it\nDO: tap 500 500" if "Next action?" in text else "1. Tap it"

    phone = Unavailable()
    agent = Agent(phone=phone, screen=Mirror(), actor=Model(), planner=Model(), trace=Trace(None))
    asyncio.run(agent._run())
    assert phone.taps == 1
    assert agent.steps[-1]["action"] == "error" and "unlock the phone" in agent.steps[-1]["reason"]


def test_a_restarted_receiver_is_not_restarted_again_by_the_next_action(tmp_path, monkeypatch):
    # the user may still be choosing Screen Mirroring again while a client retries
    from hidagent.screen import Screen as Mirror
    screen = Mirror()
    screen.frame, screen.updated = b"\xff\xd8old\xff\xd9", time.time() - 300  # stuck for 5 minutes
    restarts = []
    monkeypatch.setattr(screen, "restart", lambda real=screen.restart: (restarts.append(1), real()))
    phone = Phone(Device(screen, mouse_works=False), tmp_path / "calibration.json", screen)
    phone.span = {"w": 460, "h": 1000}
    for _ in range(2):
        with pytest.raises(phone_module.PhoneUnavailable):
            asyncio.run(phone.tap(0.5, 0.5))
    assert len(restarts) == 1


def test_the_probe_does_not_lose_track_of_the_pointer_at_the_right_edge(tmp_path):
    screen = Screen(age=0)
    device = Pointer(screen, width=460)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span = {"w": 460, "h": 1000}
    asyncio.run(phone.tap(1.0, 0.5))
    screen.updated = time.time() - 30  # still since then: the next action probes first
    asyncio.run(phone.tap(0.5, 0.5))
    assert phone._pos[0] == device.x


def test_a_phone_that_draws_the_pointer_a_moment_later_passes_the_probe(tmp_path):
    screen = Screen(age=30)
    device = Rendering(screen, width=460)
    phone = Phone(device, tmp_path / "calibration.json", screen)
    phone.span = {"w": 460, "h": 1000}
    asyncio.run(phone.tap(0.5, 0.5))
    assert device.lines[-1] == "click"


def precise_phone(tmp_path):
    phone, device, screen = make_phone(tmp_path, age=0)
    screen.live = True
    phone.grid_points = 9
    return phone, device, screen


def test_precise_replay_never_clicks_when_pointer_cannot_be_found(tmp_path, monkeypatch):
    phone, device, _ = precise_phone(tmp_path)
    monkeypatch.setattr(phone, "_probe_pointer", lambda *a: asyncio.sleep(0, None))
    with pytest.raises(phone_module.PhoneUnavailable, match="未点击"):
        asyncio.run(phone.tap(0.5, 0.5, aim=True, strict=True))
    assert "click" not in device.lines


def test_precise_replay_checks_the_landing_after_correcting(tmp_path, monkeypatch):
    phone, device, _ = precise_phone(tmp_path)
    found = iter([(0.55, 0.55), (0.501, 0.502)])
    monkeypatch.setattr(phone, "_probe_pointer", lambda *a: asyncio.sleep(0, next(found)))
    result = asyncio.run(phone.tap(0.5, 0.5, aim=True, strict=True))
    assert result[:2] == (0.501, 0.502)
    assert device.lines[-2:] == ["move -23 -50", "click"]


def test_repeated_mislanding_stops_before_press_or_click(tmp_path, monkeypatch):
    phone, device, _ = precise_phone(tmp_path)
    monkeypatch.setattr(phone, "_probe_pointer", lambda *a: asyncio.sleep(0, (0.55, 0.55)))
    with pytest.raises(phone_module.PhoneUnavailable):
        asyncio.run(phone.swipe(0.5, 0.5, 0.5, 0.3, aim=True, strict=True))
    assert "press" not in device.lines and "click" not in device.lines


def test_old_calibration_cannot_start_precise_replay(tmp_path):
    phone, device, _ = precise_phone(tmp_path)
    phone.grid_points = 0
    with pytest.raises(phone_module.PhoneUnavailable, match="九点"):
        asyncio.run(phone.tap(0.5, 0.5, aim=True, strict=True))
    assert not device.lines


def test_changed_orientation_stops_without_reusing_old_coordinates(tmp_path):
    phone, device, screen = precise_phone(tmp_path)
    phone.frame_aspect, screen.aspect = 0.46, 2.17
    with pytest.raises(phone_module.PhoneUnavailable, match="比例"):
        asyncio.run(phone.tap(0.5, 0.5, aim=True, strict=True))
    assert not device.lines
