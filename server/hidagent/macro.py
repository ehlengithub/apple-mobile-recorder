"""Desktop-only teach/replay. Adapted from ESP32 iPhone Agent, Henry Yuan (MIT).

The on-disk format remains a list. New steps add a monotonic ``wait`` and an
optional inline ``guard`` (normalized crop + lossless RGB thumbnail). Recording
is a draft until record_stop commits atomically; record_cancel restores the old
macro. A guard is a conservative local appearance check, not semantic vision.

Phone contract: strict tap/swipe/scroll MUST fail before pressing if pointer
localization fails, and return (found_x, found_y, correction_dx, correction_dy).
All phone/screen/device coroutines must propagate cancellation. No board replay.
"""

import asyncio
import base64
import copy
import io
import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

log = logging.getLogger(__name__)
GUARD_SIZE = (64, 64)
QUIET = 0.35


class Macro:
    def __init__(self, file: Path, *, settle_timeout=4.0, action_timeout=20.0, release_timeout=2.0):
        self._file = Path(file)
        self._traces = self._file.parent / "macro_traces"
        self.last_error = ""
        try:
            self.steps = json.loads(self._file.read_text()) or []
            if not isinstance(self.steps, list) or any(not isinstance(s, dict) for s in self.steps):
                raise ValueError("expected a list of steps")
        except FileNotFoundError:
            self.steps = []
        except (OSError, ValueError) as exc:
            self.steps = []
            self.last_error = f"读取宏失败：{exc}"
        self.recording = self.playing = self.paused = False
        self.loop = self.loops = 0
        self.trace = ""
        self.current_step = None
        self._task = None
        self._running = self._cleaning = self._stop_requested = False
        self._record_backup = None
        self._record_at = None
        # Bound caller-supplied settings too; none can disable the safety timeout.
        self.settle_timeout = self._timeout(settle_timeout, 10)
        self.action_timeout = self._timeout(action_timeout, 60)
        self.release_timeout = self._timeout(release_timeout, 5)

    @staticmethod
    def _timeout(value, upper):
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("等待上限必须是有限正数")
        return min(value, upper)

    @property
    def state(self):
        coords = [s for s in self.steps if self._first_xy(s) is not None]
        return {"recording": self.recording, "steps": len(self.steps), "playing": self.playing,
                "loop": self.loop, "loops": self.loops, "trace": self.trace,
                "last_error": self.last_error, "paused": self.paused,
                "stopping": self.playing and self._stop_requested,
                "current_step": self.current_step,
                "list_mode": any(s.get("guard", {}).get("policy") == "list_row" for s in self.steps),
                "guarded": bool(coords) and all(bool(s.get("guard")) for s in coords)}

    def set_list_mode(self, enabled):
        """Only the opening vertical run of row taps may ignore text to the right of its icon.

        Later buttons/confirmations keep exact guards. Keep the original references
        so disabling this mode restores strict matching without re-recording.
        """
        if self.recording or self.playing:
            raise RuntimeError("请先结束录制或停止回放，再切换列表循环")
        steps = copy.deepcopy(self.steps)
        for step in steps:
            step.get("guard", {}).pop("policy", None)
        if enabled:
            rows = []
            for step in steps:
                if step.get("do") != "tap" or "guard" not in step:
                    break
                x, y = step.get("x", -1), step.get("y", -1)
                if not .08 <= x <= .85 or not .06 <= y <= .9:
                    break
                if rows and (abs(x - rows[0]["x"]) > .03 or not .02 <= y - rows[-1]["y"] <= .12):
                    break
                rows.append(step)
            if len(rows) < 2:
                raise RuntimeError("未找到连续向下的列表选择动作；请先录制至少两行，再录制后续操作按钮")
            for step in rows:
                step["guard"]["policy"] = "list_row"
        self._save(steps)
        self.steps = steps
        self.last_error, self.paused = "", False
        return self.state

    def _save(self, steps):
        """Write in the same directory so a failed commit preserves the old file."""
        data = json.dumps(steps, ensure_ascii=False, allow_nan=False)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self._file.parent,
                                             prefix=f".{self._file.name}.", delete=False) as out:
                name = out.name
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self._file)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def record_start(self):
        if self.recording or self.playing:
            raise RuntimeError("宏正忙，请先停止")
        self._record_backup = copy.deepcopy(self.steps)
        self.steps = []
        self._record_at = time.monotonic()
        self.recording = True
        self.last_error = ""

    def record(self, do, *, frame=None, **kw):
        if not self.recording:
            return
        now = time.monotonic()
        step = {**kw, "do": do, "wait": max(0.0, now - self._record_at)}
        # Build the entire step first. Bad JPEG/coordinates never corrupt the draft.
        xy = self._first_xy(step)
        if frame is not None and xy is not None:
            step["guard"] = self._make_guard(self._image(frame), xy)
        else:
            step.pop("guard", None)
        self.steps.append(step)
        self._record_at = now

    def record_stop(self):
        if not self.recording:
            return len(self.steps)
        if self.steps:
            try:
                self._save(self.steps)
            except Exception as exc:
                self.last_error = f"保存录制失败，旧宏已保留，可重试：{exc}"
                raise
        else:
            self.steps = self._record_backup
        self.recording = False
        self._record_backup = None
        return len(self.steps)

    def record_cancel(self):
        if self.recording:
            self.steps = self._record_backup
            self._record_backup = None
            self.recording = False
        return len(self.steps)

    def undo(self):
        if not self.recording:
            raise RuntimeError("只能撤销正在录制的动作")
        if self.steps:
            removed = self.steps.pop()
            # The next surviving action must retain the time spent on the undone one.
            self._record_at -= removed["wait"]
        return len(self.steps)

    def clear(self):
        if self.recording or self.playing:
            raise RuntimeError("宏正忙，请先停止")
        self._save([])
        self.steps = []

    @staticmethod
    def _image(frame):
        if not frame:
            raise RuntimeError("投屏画面缺失，已停止回放")
        try:
            with Image.open(io.BytesIO(frame)) as image:
                return image.convert("RGB")
        except (OSError, ValueError) as exc:
            raise RuntimeError("投屏画面无法解码，已停止回放") from exc

    @staticmethod
    def _first_xy(step):
        do = step.get("do")
        if do in ("tap", "scroll"):
            return step.get("x"), step.get("y")
        if do == "swipe":
            return step.get("x1"), step.get("y1")
        if do == "home":
            return 0.5, 0.995
        if do == "back":
            return 0.005, 0.45
        return None

    @staticmethod
    def _thumbnail(image, box):
        w, h = image.size
        pixels = tuple(round(v * (w if i % 2 == 0 else h)) for i, v in enumerate(box))
        return image.crop(pixels).resize(GUARD_SIZE, Image.Resampling.LANCZOS).filter(
            ImageFilter.GaussianBlur(0.7))

    @classmethod
    def _make_guard(cls, image, xy):
        x, y = xy
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in xy):
            raise ValueError("动作坐标必须在 0 到 1 之间")
        w, h = image.size
        # Context around the target, clipped at edges. Relative radius scales with
        # resolution, 24 px minimum preserves enough detail on small mirror frames.
        radius = max(24, min(w, h) * 0.14)
        box = [max(0, x - radius / w), max(0, y - radius / h),
               min(1, x + radius / w), min(1, y + radius / h)]
        out = io.BytesIO()
        cls._thumbnail(image, box).save(out, "PNG")
        return {"v": 1, "box": box, "aspect": w / h,
                "png": base64.b64encode(out.getvalue()).decode("ascii")}

    @staticmethod
    def _pointer_mask(*images):
        """Ignore only small, round, neutral cursor blobs, never the whole target.

        Conservative shape/color limits and a 20% total mask budget prevent a
        changed button/label from being discarded as a pointer. Content hidden
        under an actual pointer cannot be verified by an appearance guard.
        """
        mask = Image.new("L", GUARD_SIZE, 255)
        draw = ImageDraw.Draw(mask)
        w, h = GUARD_SIZE
        for image in images:
            raw = image.tobytes()
            pixels = zip(raw[::3], raw[1::3], raw[2::3], strict=True)
            remaining = {i for i, rgb in enumerate(pixels) if max(rgb) - min(rgb) <= 20 and max(rgb) <= 220}
            while remaining:
                seed = remaining.pop()
                component, pending = [seed], [seed]
                while pending:
                    i = pending.pop()
                    x, y = i % w, i // w
                    for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                        j = ny * w + nx
                        if 0 <= nx < w and 0 <= ny < h and j in remaining:
                            remaining.remove(j)
                            component.append(j)
                            pending.append(j)
                xs, ys = [i % w for i in component], [i // w for i in component]
                left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
                bw, bh = right - left + 1, bottom - top + 1
                if not (7 <= bw <= 22 and 7 <= bh <= 22 and .8 <= bw / bh <= 1.25):
                    continue
                if not .25 <= len(component) / (bw * bh) <= .9:
                    continue
                cx, cy = (left + right) / 2, (top + bottom) / 2
                # A rectangular grey UI control is not a circular pointer.
                outside = sum(((x - cx) / (bw / 2)) ** 2 + ((y - cy) / (bh / 2)) ** 2 > 1.1
                              for x, y in zip(xs, ys, strict=True))
                if outside > len(component) * .05:
                    continue
                candidate = mask.copy()
                ImageDraw.Draw(candidate).ellipse((left - 1, top - 1, right + 1, bottom + 1), fill=0)
                if candidate.histogram()[0] <= w * h * .2:
                    draw.ellipse((left - 1, top - 1, right + 1, bottom + 1), fill=0)
        return mask

    @classmethod
    def _check_guard(cls, step, image):
        if "guard" not in step:
            return False  # Legacy/frameless recordings: pointer-only, never claim page verification.
        try:
            guard = step["guard"]
            box = guard["box"]
            if (guard["v"] != 1 or len(box) != 4
                    or any(not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("invalid guard")
            if abs(image.width / image.height / guard["aspect"] - 1) > 0.025:
                raise RuntimeError("投屏方向或比例已改变，请检查后重新录制")
            reference = cls._image(base64.b64decode(guard["png"], validate=True))
            if reference.size != GUARD_SIZE:
                raise ValueError("invalid thumbnail")
            current = cls._thumbnail(image, box)
            diff = ImageChops.difference(reference, current)
            mask = cls._pointer_mask(reference, current)
            policy = guard.get("policy", "exact")
            if policy == "list_row":
                if step.get("do") != "tap":
                    raise ValueError("list guard requires a row tap")
                # The recorded tap is on the row icon. The checkbox and icon
                # stay checked; only the text column to their right may vary.
                cutoff = (step["x"] + .035 - box[0]) / (box[2] - box[0])
                if not .55 <= cutoff <= .8:
                    raise ValueError("invalid list text column")
                ImageDraw.Draw(mask).rectangle((round(cutoff * 64), 0, 64, 64), fill=0)
            elif policy != "exact":
                raise ValueError("unknown guard policy")
            diff = ImageChops.multiply(diff, mask.convert("RGB"))
            mean = sum(ImageStat.Stat(diff).mean) / 3
            # Check local blocks as well: a changed label/icon can occupy only a
            # small fraction of the surrounding crop. Blur tolerates JPEG noise.
            local = max(sum(ImageStat.Stat(diff.crop((x, y, x + 8, y + 8))).mean) / 3
                        for y in range(0, 64, 8) for x in range(0, 64, 8))
            if mean > 12 or local > 28:
                message = ("列表的行位置、选择状态或图标已变化，已暂停回放" if policy == "list_row"
                           else "目标周围界面已变化，已暂停回放，请检查或重新录制")
                raise RuntimeError(message)
        except RuntimeError:
            raise
        except (KeyError, TypeError, ValueError, OSError, ZeroDivisionError) as exc:
            raise RuntimeError("页面验证依据损坏，已暂停回放，请重新录制") from exc
        return True

    @staticmethod
    def _ready(phone):
        if not phone.device.connected:
            raise RuntimeError("设备未连接，已停止回放")
        if not phone.calibrated:
            raise RuntimeError("指针尚未校准，已停止回放")
        if not getattr(phone, "precision_calibrated", False):
            raise RuntimeError("尚未完成覆盖三行三列的九点校准，请校准后再回放")
        if phone.screen is None or not phone.screen.live:
            raise RuntimeError("需要实时投屏，已停止回放")
        if not phone.screen.frame:
            raise RuntimeError("投屏画面缺失，已停止回放")

    def play(self, phone, loops=0):
        try:
            if self.recording or self.playing:
                raise RuntimeError("宏正忙，请先停止")
            if not self.steps:
                raise RuntimeError("没有可回放的动作")
            if not isinstance(loops, int) or isinstance(loops, bool):
                raise ValueError("循环次数必须是整数，0 表示持续循环")
            self._ready(phone)
            self._image(phone.screen.frame)
            loop = asyncio.get_running_loop()
            self._traces.mkdir(parents=True, exist_ok=True)
            self.trace = tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=self._traces)
            steps = copy.deepcopy(self.steps)
        except Exception as exc:
            self.last_error = f"无法开始回放：{exc}"
            raise
        self.loops, self.loop, self.playing = max(0, loops), 0, True
        self.last_error, self.paused, self.current_step = "", False, None
        self._stop_requested = self._running = self._cleaning = False
        self._task = loop.create_task(self._run(phone, steps))

    async def _settle(self, phone):
        self._ready(phone)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(phone.screen.settled(
                quiet=QUIET, least=min(0.25, self.settle_timeout), most=self.settle_timeout),
                timeout=self.settle_timeout + 0.5)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("等待投屏稳定超时，已停止回放") from exc
        self._ready(phone)
        if result is False or (result is None and time.monotonic() - started >= self.settle_timeout
                               and time.time() - getattr(phone.screen, "updated", 0) < QUIET):
            raise RuntimeError("投屏持续变化，等待稳定超时，已停止回放")
        return self._image(phone.screen.frame)

    async def _run(self, phone, steps):
        self._running = True
        logf = None
        try:
            logf = (Path(self.trace) / "steps.jsonl").open("a", encoding="utf-8")
            previous = time.monotonic()
            while not self._stop_requested and (self.loops == 0 or self.loop < self.loops):
                self.loop += 1
                for i, step in enumerate(steps):
                    self.current_step = i
                    entry = {"round": self.loop, "i": i, "do": step.get("do"), "guarded": False}
                    started = time.monotonic()
                    try:
                        delay = float(step.get("wait", 0))
                        if not math.isfinite(delay) or delay < 0:
                            raise RuntimeError("录制动作间隔无效")
                        await asyncio.sleep(max(0, previous + delay - time.monotonic()))
                        if self._stop_requested:
                            raise asyncio.CancelledError
                        image = await self._settle(phone)
                        entry["before"] = self._shot(phone, "before", i)
                        entry["guard_policy"] = step.get("guard", {}).get("policy", "exact")
                        entry["guarded"] = self._check_guard(step, image)
                        previous = time.monotonic()
                        try:
                            aim = await asyncio.wait_for(self._do(phone, step), self.action_timeout)
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError("指针定位或动作执行超时，已停止回放") from exc
                        entry["aim_found"] = list(aim[:2]) if aim is not None else None
                        entry["aim_correct_units"] = list(aim[2:]) if aim is not None else None
                        entry["after"] = self._shot(phone, "after", i)
                        entry["status"] = "ok"
                    except asyncio.CancelledError:
                        entry["status"] = "cancelled"
                        raise
                    except Exception as exc:
                        entry.update(status="error", error=str(exc))
                        raise
                    finally:
                        entry["latency_s"] = round(time.monotonic() - started, 3)
                        logf.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        logf.flush()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.last_error = f"第 {self.loop} 轮第 {(self.current_step or 0) + 1} 步回放失败：{exc}"
            self.paused = True
            log.warning("%s", self.last_error)
        finally:
            self._cleaning = True
            cleanup = asyncio.create_task(asyncio.wait_for(phone.device.send("release"), self.release_timeout))
            try:
                # Release only: a re-home here could move to an unexpected UI target.
                # An action can finish concurrently with stop(): its pending
                # cancellation may first arrive here. Shield AND join cleanup.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            except (Exception, asyncio.CancelledError) as exc:
                reason = "释放按键失败：等待设备响应超时，请检查连接" if isinstance(
                    exc, asyncio.TimeoutError) else f"释放按键失败：{exc}"
                self.last_error = (self.last_error + "；" if self.last_error else "") + reason
                self.paused = True
            finally:
                try:
                    if logf is not None:
                        logf.close()
                finally:
                    self.playing, self.loop, self._running, self._cleaning = False, 0, False, False

    def _shot(self, phone, when, i):
        name = f"r{self.loop:02d}s{i:02d}_{when}.jpg"
        frame = phone.screen.frame
        if not frame:
            return ""
        (Path(self.trace) / name).write_bytes(frame)
        return name

    @staticmethod
    async def _do(phone, step):
        do = step.get("do")
        keys = ("x1", "y1", "x2", "y2") if do == "swipe" else ("x", "y") if do in ("tap", "scroll") else ()
        for key in keys:
            value = step.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise RuntimeError("宏动作坐标无效，已停止回放")
        if do == "tap":
            aim = await phone.tap(step["x"], step["y"], aim=True, strict=True)
        elif do == "swipe":
            aim = await phone.swipe(step["x1"], step["y1"], step["x2"], step["y2"], aim=True, strict=True)
        elif do == "scroll":
            aim = await phone.scroll(step["x"], step["y"], step["lines"], aim=True, strict=True)
        elif do == "home":
            aim = await phone.swipe(0.5, 0.995, 0.5, 0.6, hold=False, aim=True, strict=True)
        elif do == "back":
            aim = await phone.swipe(0.005, 0.45, 0.6, 0.45, hold=False, aim=True, strict=True)
        elif do in ("type", "key"):
            await getattr(phone, do)(step["text"])
            return None
        else:
            raise RuntimeError(f"不支持的宏动作：{do!r}")
        if (not isinstance(aim, (tuple, list)) or len(aim) < 2
                or any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
                       for v in aim[:2])):
            raise RuntimeError("指针定位失败，未收到有效的确认坐标，已停止回放")
        return aim

    def stop(self):
        """Request cancellation synchronously. playing stays true until release finishes."""
        if self._task and not self._task.done() and not self._stop_requested:
            self._stop_requested = True
            # A task cancelled before its first instruction never enters finally.
            # Let a not-yet-started runner enter its cleanup path instead.
            if self._running and not self._cleaning:
                self._task.cancel()

    async def wait_stopped(self):
        """Wait for runner AND release. Cancelling an API waiter must not cancel cleanup."""
        task = self._task
        if task is not None:
            await asyncio.shield(task)
