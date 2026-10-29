"""Touch-like gestures on the phone, built from relative mouse moves.

The pointer is relative, so the server tracks where it is (in mouse units) and moves
from there; every few gestures it slams it into the top-left corner (`home`) again to
cancel drift. Coordinates are fractions of the screen (0..1, origin top-left), so they
work for any frame size.
"""

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from . import clipboard, vision
from .device import Device

log = logging.getLogger(__name__)

CALIBRATION_MOVE = 200  # mouse units moved diagonally for a manual (one point) calibration; stays on screen
CALIBRATION_STOPS = (150, 350)  # automatic calibration finds the pointer at these distances from the corner
# then where it lands for plain moves across the whole screen: the two stops above sit in the top-left
# third, so a line through them alone stretches small reading errors into misses of up to ~65 px near the
# bottom; with this grid the worst miss stayed within ~18 px (three calibrations on a real iPhone)
CALIBRATION_GRID = tuple((x, y) for y in (0.15, 0.5, 0.85) for x in (0.15, 0.5, 0.85))
REHOME_EVERY = 10       # gestures between re-homing
HOLD = 0.25             # seconds to rest before lifting, so iOS adds no momentum (the swipe stops where it ends)
TAP_GAP = 0.15          # seconds to stay still after a click, so iOS sees a finished tap, not the start of a drag
NUDGE = 12              # mouse units the pointer is wiggled by to find it on screen
AIM_TOLERANCE = 0.008   # screen fraction; a bigger miss is corrected before pressing
MAX_MISS = 0.06         # bigger misses (up to AIM_REJECT) are still corrected, but not learned from
HANDOFF_WAIT = 4.0      # seconds for Universal Clipboard to offer the Mac's new clipboard (1 s was too short)
AIM_REJECT = 2 * MAX_MISS  # a pointer "found" farther off is something else that moved (a clock hand): ignored
STALE_AFTER = 5.0       # seconds without a new frame before an action first checks that the phone still responds
PROBE_WAIT = 1.5        # seconds a pointer wiggle gets to show up on the mirrored screen
FROZEN_AFTER = 70.0     # no frame for this long: not even the status-bar clock changed, so the mirror is stuck
LONG_MOVE = 0.3         # screen fraction; after a longer move the pointer gets a moment to settle before a click
SETTLE_AFTER_LONG_MOVE = 0.15  # seconds


class PhoneUnavailable(RuntimeError):
    """The phone cannot be acted on right now (mirror frozen, phone locked, mouse ignored): say why, do nothing."""


class Phone:
    def __init__(self, device: Device, calibration_file: Path, screen=None):
        self.device, self._file, self.screen = device, calibration_file, screen
        # mouse units needed to cross the full screen width / height
        saved = json.loads(calibration_file.read_text()) if calibration_file.exists() else None
        self.span = {"w": saved["w"], "h": saved["h"]} if saved else None
        self._pos = None  # pointer position in mouse units; None = unknown
        self._last_move = 0.0  # length of the latest move, as a fraction of the screen
        self._moves = 0
        self._link = 0  # device.links when the pointer was last homed
        self.grid_points = (saved or {}).get("grid_points", 0)
        self.frame_aspect = (saved or {}).get("frame_aspect", 0)
        # where the pointer really landed (seen on screen) for the screen fraction we aimed at, per axis;
        # a straight-line fit through them corrects every later move, so taps get better as the agent works
        self._seen = {axis: deque(map(tuple, (saved or {}).get("seen", {}).get(axis, [])), maxlen=24)
                      for axis in ("x", "y")}

    @property
    def absolute(self) -> bool:
        return getattr(self.device, "absolute", False)

    @property
    def calibrated(self) -> bool:
        return self.absolute or self.span is not None

    @property
    def precision_calibrated(self) -> bool:
        return self.absolute or (self.calibrated and self.grid_points >= 6)

    def _check_geometry(self) -> None:
        aspect = getattr(self.screen, "aspect", 0)
        if self.frame_aspect and aspect and abs(aspect / self.frame_aspect - 1) > 0.03:
            raise PhoneUnavailable("手机画面方向或比例已变化，请重新执行全屏九点校准")

    async def start_calibration(self) -> bool:
        """Find the pointer on screen at two known distances from the corner and fit the mapping from both.

        Two points give the scale and the offset, which one point cannot (iOS pointer motion is not exactly
        proportional). Returns True when it calibrated itself; otherwise the user clicks the pointer
        (finish_calibration).
        """
        if self.absolute:
            self._check_geometry()
            return True  # Native normalized coordinates do not use a relative calibration fit.
        self._pos = None
        self.grid_points = 0
        await self.device.send("home")
        seen = []
        # iPad's full width can be fewer than 350 mouse units. Use shorter
        # seed moves there so the second sample cannot be clipped at an edge.
        aspect = getattr(self.screen, "aspect", 0)
        stops = (80, 180) if aspect >= 0.6 else CALIBRATION_STOPS
        for units in stops:
            step = units - (seen[-1][0] if seen else 0)
            await self.device.send(f"move {step} {step}")
            found = await self._find_pointer() if self.screen is not None and self.screen.live else None
            if not found:
                break
            seen.append((units, *found))
        if len(seen) < 2:
            log.info("calibration: pointer not found on screen, waiting for a click")
            if not seen:
                await self.device.send("home")
                await self.device.send(f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")
            return False
        (u1, x1, y1), (u2, x2, y2) = seen
        if x2 - x1 < 0.01 or y2 - y1 < 0.01 or max(x2, y2) >= 0.97:
            self.span = None
            await self.device.send("home")
            await self.device.send(f"move {CALIBRATION_MOVE} {CALIBRATION_MOVE}")
            return False
        self.span = {"w": round((u2 - u1) / (x2 - x1)), "h": round((u2 - u1) / (y2 - y1))}
        for samples in self._seen.values():
            samples.clear()
        for units, x, y in seen:  # where each stop landed, so moves are corrected for the offset too
            self._seen["x"].append((units / self.span["w"], x))
            self._seen["y"].append((units / self.span["h"], y))
        return await self.calibrate_grid()

    async def calibrate_grid(self) -> bool:
        """Measure the full screen; old single-point calibrations never count as precise replay calibration."""
        self.grid_points = 0
        rows, columns = set(), set()
        for gx, gy in CALIBRATION_GRID:
            await self.device.send("home")
            await self.device.send(f"move {round(gx * self.span['w'])} {round(gy * self.span['h'])}")
            found = await self._find_pointer()
            if found and max(abs(found[0] - gx), abs(found[1] - gy)) <= 2 * MAX_MISS:
                self._seen["x"].append((gx, found[0]))
                self._seen["y"].append((gy, found[1]))
                self.grid_points += 1
                rows.add(gy)
                columns.add(gx)
        self._pos = None  # the grid moved the pointer behind _move_to's back
        if len(rows) < 3 or len(columns) < 3:
            self.grid_points = 0
        self.frame_aspect = getattr(self.screen, "aspect", 0)
        self._save()
        log.info("full-screen calibration: %d/9 points, span %s", self.grid_points, self.span)
        if not self.precision_calibrated:
            raise PhoneUnavailable("九点校准未完整覆盖屏幕，请停在静止页面后重新校准")
        return True

    async def _find_pointer(self) -> tuple[float, float] | None:
        """Wiggle the pointer and find it anywhere on screen (the span may still be unknown)."""
        await self.screen.settled(quiet=0.2, least=0.3, most=1.5)
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {NUDGE} 0")
        after = await self.screen.fresh(sent, 1.0)
        if after:
            await self.screen.settled(quiet=0.15, least=0.1, most=0.6)
            after = self.screen.frame
        await self.device.send(f"move -{NUDGE} 0")
        found = before and after and vision.pointer(before, after, (0.5, 0.5), 0.025, reach=(0.5, 0.5))
        return found if found and 0.03 < found[0] < 0.97 and 0.03 < found[1] < 0.97 else None

    def finish_calibration(self, x: float, y: float) -> None:
        """x, y: where the pointer ended up after start_calibration() (one point, picked by the user)."""
        self.span = {"w": round(CALIBRATION_MOVE / x), "h": round(CALIBRATION_MOVE / y)}
        self.grid_points = 0
        self._pos = None
        for samples in self._seen.values():
            samples.clear()
        self._save()

    def _save(self) -> None:
        """Calibration plus the observed landings, so the correction survives a restart."""
        self._file.write_text(json.dumps({**self.span, "grid_points": self.grid_points,
                                         "frame_aspect": self.frame_aspect,
                                         "seen": {k: list(v) for k, v in self._seen.items()}}))

    def forget_position(self) -> None:
        """The pointer was moved behind our back (raw board commands): start the next gesture from the corner."""
        self._pos = None

    async def ensure_live(self) -> None:
        """Before acting on a screen that has been still for a while, wiggle the pointer and wait for the mirror to
        show it, so a frozen mirror or a mouse the phone ignores is reported instead of tapped into blindly.
        (A locked phone usually shows up earlier, as the board's "phone usb asleep" error.)"""
        if self.screen is None or getattr(self.screen, "updated", None) is None:
            return
        if time.time() - self.screen.updated < STALE_AFTER:
            return
        if self.absolute:
            raise PhoneUnavailable("投屏尚未更新，已暂停绝对坐标操作，请检查连接")
        near_right = self._pos is not None and self._pos[0] > 0.9 * self.span["w"]
        nudge = -NUDGE if near_right else NUDGE  # iOS stops the pointer at the edge, and the move back would not
        sent = time.time()
        await self.device.send(f"move {nudge} 0")
        moved = await self.screen.fresh(sent, PROBE_WAIT)  # before moving back: a move undone at once may never show
        await self.device.send(f"move {-nudge} 0")
        if moved:
            return
        name = getattr(self.screen, "name", "iPhone Agent")
        if not self.screen.updated:  # no frame ever: nothing is stuck, mirroring was just never started
            raise PhoneUnavailable(f"the phone is not mirrored: choose Control Center › Screen Mirroring › {name}")
        if time.time() - self.screen.updated > FROZEN_AFTER:
            self.screen.restart()
            raise PhoneUnavailable("the mirrored screen stopped updating, so the AirPlay receiver was restarted: "
                                   "unlock the phone if it is locked, then choose Control Center › Screen Mirroring › "
                                   f"{name} again")
        raise PhoneUnavailable("the pointer did not move on the mirrored screen: unlock the phone if it is locked; "
                               "if it is unlocked, it is ignoring the board's mouse, so unplug the board's adapter "
                               "from the phone and plug it back in")

    async def point(self, x: float, y: float, aim: bool = False, strict: bool = False):
        if self.absolute:
            return await self._absolute_point(x, y, strict or aim)
        if not self.calibrated:
            raise RuntimeError("pointer not calibrated")
        if strict:
            if not self.precision_calibrated:
                raise PhoneUnavailable("请先完成全屏九点校准，再录制或回放")
            if self.screen is None or not getattr(self.screen, "live", False):
                raise PhoneUnavailable("手机投屏未连接，已暂停操作")
            self._check_geometry()
        await self.ensure_live()
        await self._move_to(self._dx(x), self._dy(y))
        if aim and self.screen is not None:
            if strict:
                return await self._aim_verified(x, y)
            return await self._aim(x, y)

    async def _absolute_point(self, x, y, verify):
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError("坐标必须在屏幕范围内")
        if self.screen is None or not self.screen.live:
            raise PhoneUnavailable("投屏未连接，已暂停绝对坐标操作")
        self._check_geometry()
        await self.ensure_live()
        if not verify:
            await self._move_to(round(x * 32767), round(y * 32767))
            await self.screen.settled(quiet=.12, least=.2, most=.6)
            return None
        # iPad can merge the pointer into a button. Compare two positions
        # beside the target, where the pointer is visible, then return to it.
        offset = .06 if x < .86 else -.06
        shift = .035 if offset > 0 else -.035
        sent = time.time()
        await self._move_to(round((x + offset) * 32767), round(y * 32767))
        await self.screen.fresh(sent, PROBE_WAIT)
        await self.screen.settled(quiet=.12, least=.4, most=.7)
        before, sent = self.screen.frame, time.time()
        restored = None
        try:
            await self.device.send(f"abs {round((x + offset + shift) * 32767)} {round(y * 32767)}")
            after = await self.screen.fresh(sent, PROBE_WAIT)
            if after:
                await self.screen.settled(quiet=.1, least=.3, most=.6)
                after = self.screen.frame
        finally:
            sent = time.time()
            await self.device.send(f"abs {round(x * 32767)} {round(y * 32767)}")
            self._pos = (round(x * 32767), round(y * 32767))
        restored = await self.screen.fresh(sent, PROBE_WAIT)
        if restored:
            await self.screen.settled(quiet=.1, least=.25, most=.6)
        found = before and after and restored and vision.absolute_pointer(before, after, (x + offset + shift, y))
        if found:
            found = (found[0] - offset - shift, found[1])
        log.debug("absolute verify target=%s found=%s", (x, y), found)
        if not found or max(abs(found[0] - x), abs(found[1] - y)) > AIM_TOLERANCE:
            self.forget_position()
            raise PhoneUnavailable("绝对坐标指针位置未能确认，已暂停，未点击")
        return found[0], found[1], 0, 0

    async def _probe_pointer(self, x: float, y: float):
        """Locate a real, settled pointer from a fresh frame pair before every strict gesture."""
        nudge = NUDGE if x < 0.9 else -NUDGE
        await self.screen.settled(quiet=0.2, least=0.3, most=1.2)
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {nudge} 0")
        try:
            after = await self.screen.fresh(sent, PROBE_WAIT)
            if after:
                await self.screen.settled(quiet=0.15, least=0.2, most=0.6)
                after = self.screen.frame
        finally:
            restored_at = time.time()
            await self.device.send(f"move {-nudge} 0")
        # iPad applies a HID report after the board acknowledges it. Wait
        # for the return move to appear before allowing a press.
        restored = await self.screen.fresh(restored_at, PROBE_WAIT)
        if restored:
            await self.screen.settled(quiet=0.15, least=0.2, most=0.6)
        if not before or not after or not restored:
            return None
        shift = nudge / self.span["w"]
        return (vision.pointer(before, after, (x, y), shift)
                or vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))

    async def _aim_verified(self, x: float, y: float):
        """Re-read after every correction; never emit a click on an unverified estimate."""
        total_dx = total_dy = 0
        for _ in range(4):
            self._check_geometry()
            found = await self._probe_pointer(x, y)
            log.debug("precision probe: target %.4f %.4f, found %s", x, y, found)
            if not found:
                continue
            fx, fy = found
            miss = max(abs(fx - x), abs(fy - y))
            if miss > AIM_REJECT:
                continue
            if miss <= AIM_TOLERANCE:
                self._seen["x"].append((self._pos[0] / self.span["w"], fx))
                self._seen["y"].append((self._pos[1] / self.span["h"], fy))
                self._save()
                return fx, fy, total_dx, total_dy
            dx = round((x - fx) * self.span["w"])
            dy = round((y - fy) * self.span["h"])
            await self.device.send(f"move {dx} {dy}")
            self._pos = (self._pos[0] + dx, self._pos[1] + dy)
            total_dx += dx
            total_dy += dy
        self.forget_position()
        raise PhoneUnavailable("未能确认指针已准确到达目标，已暂停，未点击；请检查投屏延迟或重新校准")

    async def _aim(self, x: float, y: float) -> None:
        """Closed loop: find the real pointer on screen, fix a miss, and learn the error for later moves."""
        nudge = NUDGE if x < 0.9 else -NUDGE  # stay clear of the screen edge
        await self.screen.settled(quiet=0.15, least=0.1, most=0.6)  # pointer at rest
        before, sent = self.screen.frame, time.time()
        await self.device.send(f"move {nudge} 0")
        after = await self.screen.fresh(sent, 0.6)
        if after:
            await self.screen.settled(quiet=0.1, least=0.05, most=0.4)
            after = self.screen.frame
        await self.device.send(f"move {-nudge} 0")
        shift = nudge / self.span["w"]
        found = before and after and (vision.pointer(before, after, (x, y), shift)  # near the target first,
                                      or vision.pointer(before, after, (0.5, 0.5), shift, reach=(0.5, 0.5)))  # anywhere
        if not found:
            log.debug("aim (%.3f, %.3f): pointer not found (%s)", x, y,
                      "no new frame after the nudge" if not after else "no pointer-sized change near the target")
            return
        fx, fy = found
        log.debug("aim (%.3f, %.3f): pointer at (%.3f, %.3f)", x, y, fx, fy)
        if max(abs(fx - x), abs(fy - y)) > AIM_REJECT:  # something else moved; correcting toward it would mis-tap
            log.info("aim (%.3f, %.3f): ignoring a pointer reading at (%.3f, %.3f), too far off", x, y, fx, fy)
            return
        if max(abs(fx - x), abs(fy - y)) <= MAX_MISS:  # learn from normal drift only, never from a stray reading
            self._seen["x"].append((self._fix(x, "x"), fx))
            self._seen["y"].append((self._fix(y, "y"), fy))
            self._save()
        if abs(fx - x) <= AIM_TOLERANCE and abs(fy - y) <= AIM_TOLERANCE:
            return
        dx, dy = round((x - fx) * self.span["w"]), round((y - fy) * self.span["h"])  # fix this miss right away
        await self.device.send(f"move {dx} {dy}")
        self._pos = (self._pos[0] + dx, self._pos[1] + dy)

    def _fix(self, t: float, axis: str) -> float:
        """The fraction to aim at so the pointer lands on t, from the line through the observed landings."""
        pts = self._seen[axis]
        if len(pts) < 2:
            return t
        n = len(pts)
        mc, mf = sum(c for c, _ in pts) / n, sum(f for _, f in pts) / n
        var = sum((c - mc) ** 2 for c, _ in pts)
        a = sum((c - mc) * (f - mf) for c, f in pts) / var if var > 1e-4 else 1.0  # landed = a * aimed + b
        a = a if 0.85 < a < 1.15 else 1.0
        b = mf - a * mc
        return (t - b) / a if abs(b) < 0.1 else t

    async def _move_to(self, tx: int, ty: int, rehome: bool = True) -> None:
        if self.absolute:
            if self._link != self.device.links:
                await self.device.send("release")
                self._link = self.device.links
                self._pos = None
            if not rehome and self._pos is not None:
                x, y = self._pos
                steps = max(1, round(max(abs(tx-x), abs(ty-y)) / 1000))
                for i in range(1, steps):
                    await self.device.send(f"abs {round(x+(tx-x)*i/steps)} {round(y+(ty-y)*i/steps)}")
                    await asyncio.sleep(.01)
            await self.device.send(f"abs {tx} {ty}")
            self._pos, self._last_move = (tx, ty), 0
            return
        if self._link != self.device.links:  # new link: a dropped gesture may have left the button down
            await self.device.send("release")
            self._pos, self._link = None, self.device.links
        if self._pos is None or (rehome and self._moves >= REHOME_EVERY):
            await self.device.send("home")
            self._pos, self._moves = (0, 0), 0
        x, y = self._pos
        self._pos = None  # unknown until the move succeeds
        self._last_move = max(abs(tx - x) / self.span["w"], abs(ty - y) / self.span["h"])
        await self.device.send(f"move {tx - x} {ty - y}")
        self._pos, self._moves = (tx, ty), self._moves + 1

    async def reset(self) -> None:
        """Let go of the button and re-home the pointer, whatever state a gesture left."""
        self._pos = None
        await self.device.send("release")
        await self.device.send("home")
        self._pos, self._moves = (0, 0), 0

    async def tap(self, x: float, y: float, aim: bool = False, strict: bool = False):
        """aim=True finds the pointer on screen first (slower); worth it when a tap just missed."""
        correction = await self.point(x, y, aim, strict)
        if self._last_move > LONG_MOVE:  # let iOS finish moving the pointer, or the click can land on the way
            await asyncio.sleep(SETTLE_AFTER_LONG_MOVE)
        await self.device.send("click")
        await asyncio.sleep(TAP_GAP)
        return correction

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, hold: bool = True,
                    aim: bool = False, strict: bool = False):
        correction = await self.point(x1, y1, aim, strict)
        await self._drag_from_current([(x2, y2)], hold)
        return correction

    async def drag(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        """Press at the first point, move through the rest, release: one continuous stroke.

        hold=True rests at the end first, so lists and pickers stop exactly there instead of flinging on.
        System gestures (home, back) need the flick, so they pass hold=False.
        """
        await self.point(*points[0])
        await self._drag_from_current(points[1:], hold)

    async def _drag_from_current(self, points: list[tuple[float, float]], hold: bool = True) -> None:
        await self.device.send("press")
        try:
            for x, y in points:
                await self._move_to(self._dx(x), self._dy(y), rehome=False)  # never home mid-drag
            if hold:
                await asyncio.sleep(HOLD)
        finally:
            await self.device.send("release")

    async def scroll(self, x: float, y: float, lines: int, aim: bool = False, strict: bool = False):
        correction = await self.point(x, y, aim, strict)
        await self.device.send(f"scroll {lines} 0")
        return correction

    async def type(self, text: str) -> None:
        """ASCII is typed key by key; anything else (Chinese, emoji) is pasted through Universal Clipboard."""
        await self.ensure_live()
        if text.isascii():
            await self.device.send(f"type {text}")
            return
        if not clipboard.supported():
            raise RuntimeError("non-ASCII text is pasted through Universal Clipboard, so the server must run on a Mac")
        # The text stays on the Mac's clipboard: iOS may first ask "Allow Paste?", and putting the user's own
        # clipboard back before that is answered would paste it (it could be a password) instead.
        clipboard.write(text)
        await asyncio.sleep(HANDOFF_WAIT)
        await self.device.send("key cmd+v")

    async def key(self, combo: str) -> None:
        await self.ensure_live()
        await self.device.send(f"key {combo}")

    async def go_home(self) -> None:
        await self.swipe(0.5, 0.995, 0.5, 0.6, hold=False)  # resting here would open the app switcher

    async def go_back(self) -> None:
        await self.swipe(0.005, 0.45, 0.6, 0.45, hold=False)

    def _dx(self, x: float) -> int:
        if self.absolute:
            return round(min(max(x, 0), 1) * 32767)
        return round(min(max(self._fix(x, "x"), 0), 1) * self.span["w"])

    def _dy(self, y: float) -> int:
        if self.absolute:
            return round(min(max(y, 0), 1) * 32767)
        return round(min(max(self._fix(y, "y"), 0), 1) * self.span["h"])
