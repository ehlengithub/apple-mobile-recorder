"""Web API + control page. Run with `python -m hidagent`."""

import asyncio
import os
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent
from .debug import Trace
from .device import Device, DeviceError
from .llm import LLM
from .macro import Macro
from .phone import Phone
from .screen import Screen
from .studio_api import register as register_studio

DATA_DIR = Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".apple-mobile-recorder"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG = os.environ.get("HIDAGENT_DEBUG") == "1"

device = Device()
# MIRROR_STREAM=127.0.0.1:8891 follows a standalone receiver (python -m hidagent.receiver) instead of running
# UxPlay here, so restarting the server keeps the phone's Screen Mirroring session
screen = Screen(os.environ.get("AIRPLAY_NAME", "Apple Recorder"), fps=30, stream=os.environ.get("MIRROR_STREAM"),
                pidfile=DATA_DIR / "uxplay.pid")
phone = Phone(device, DATA_DIR / "calibration.json", screen)
agent = Agent(phone, screen, LLM.from_env("ACTOR"), LLM.from_env("PLANNER"),
              Trace(DATA_DIR / "traces" if DEBUG else None))
macro = Macro(DATA_DIR / "macro.json")


@asynccontextmanager
async def lifespan(_):
    server = await device.serve("0.0.0.0", int(os.environ.get("DEVICE_PORT", 7400)))
    screen.start()
    awake = asyncio.create_task(device.keep_awake(screen=screen))
    yield
    macro.stop()
    await macro.wait_stopped()
    awake.cancel()
    screen.stop()
    server.close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def local_mutations(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if (origin and urlparse(origin).hostname not in ("localhost", "127.0.0.1", "::1")):
            return Response("仅允许本机页面执行操作", status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return Response("拒绝跨站操作", status_code=403)
    return await call_next(request)
class Static(StaticFiles):
    """Static files that browsers re-check on every load, so a new version shows up on refresh."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", Static(directory=Path(__file__).parent / "static"), name="static")


class Point(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class Tap(Point):
    precise: bool = False  # find the pointer on screen and correct a miss before tapping (about 1 s slower)


class Swipe(BaseModel):
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)
    x2: float = Field(ge=0, le=1, allow_inf_nan=False)
    y2: float = Field(ge=0, le=1, allow_inf_nan=False)
    precise: bool = False


class Scroll(Point):
    lines: int = Field(ge=-100, le=100)
    precise: bool = False


class Text(BaseModel):
    text: str


class KeyboardText(Text):
    text: str = Field(min_length=1, max_length=4000)


def check_keyboard(text, ascii_only=False):
    """One line, no control characters. Non-ASCII text is fine for typing (it is pasted), never for keys."""
    if (ascii_only and not text.isascii()) or any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in text):
        raise HTTPException(422, "keys must be printable ASCII" if ascii_only else "text must be one printable line")


manual_lock = asyncio.Lock()


def check_manual():
    if agent.running or macro.playing or manual_lock.locked():
        raise HTTPException(409, "phone is busy; stop the agent or wait for the current action")


async def act(action):
    check_manual()
    try:
        async with manual_lock:
            await action()
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "studio.html")


@app.get("/legacy")
def legacy_index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/api/status")
def status():
    return {"device": device.connected, "screen": screen.live, "calibrated": phone.calibrated,
            "pointer_mode": "absolute" if getattr(phone, "absolute", False) else "relative",
            "debug": DEBUG, "trace": str(agent.trace.dir or ""), "aspect": round(screen.aspect, 4),
            "models": {"planner": agent.planner.model, "actor": agent.actor.model},
            "agent": {"running": agent.running, "task": agent.task, "steps": agent.steps},
            "precision_calibrated": phone.precision_calibrated,
            "calibration_points": phone.grid_points, "macro": macro.state,
            "frame_seq": getattr(screen, "frame_seq", 0)}


@app.get("/api/frame.jpg")
async def frame(after: int | None = Query(default=None, ge=0), wait: float = Query(default=0, ge=0, le=1)):
    deadline = asyncio.get_running_loop().time() + wait
    while (screen.live and after is not None and after == getattr(screen, "frame_seq", 0)
           and asyncio.get_running_loop().time() < deadline):
        await asyncio.sleep(0.01)
    if not screen.live:
        raise HTTPException(404, "no live screen: start screen mirroring on the phone")
    seq = getattr(screen, "frame_seq", 0)
    headers = {"Cache-Control": "no-store", "X-Frame-Seq": str(seq)}
    if after == seq:
        return Response(status_code=204, headers=headers)
    return Response(screen.frame, media_type="image/jpeg", headers=headers)


@app.get("/api/steps/{index}.jpg")
def step_shot(index: int):
    if index not in agent.shots:
        raise HTTPException(404)
    return Response(agent.shots[index], media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


@app.post("/api/tap")
async def tap(p: Tap):
    before = screen.frame
    r = await act(lambda: phone.tap(p.x, p.y, aim=True, strict=True)
                  if p.precise or macro.recording else phone.tap(p.x, p.y))
    macro.record("tap", x=p.x, y=p.y, frame=before)
    return r


@app.post("/api/swipe")
async def swipe(s: Swipe):
    before = screen.frame
    r = await act(lambda: phone.swipe(s.x1, s.y1, s.x2, s.y2, aim=True, strict=True)
                  if s.precise or macro.recording else phone.swipe(s.x1, s.y1, s.x2, s.y2))
    macro.record("swipe", x1=s.x1, y1=s.y1, x2=s.x2, y2=s.y2, frame=before)
    return r


@app.post("/api/scroll")
async def scroll(s: Scroll):
    before = screen.frame
    r = await act(lambda: phone.scroll(s.x, s.y, s.lines, aim=True, strict=True)
                  if s.precise or macro.recording else phone.scroll(s.x, s.y, s.lines))
    macro.record("scroll", x=s.x, y=s.y, lines=s.lines, frame=before)
    return r


@app.post("/api/type")
async def type_text(t: KeyboardText):
    check_keyboard(t.text)
    r = await act(lambda: phone.type(t.text))
    macro.record("type", text=t.text)
    return r


@app.post("/api/key")
async def key(t: KeyboardText):
    check_keyboard(t.text, ascii_only=True)
    r = await act(lambda: phone.key(t.text))
    macro.record("key", text=t.text)
    return r


@app.post("/api/home")
async def home():
    r = await act(phone.go_home)
    macro.record("home")
    return r


@app.post("/api/back")
async def back():
    r = await act(phone.go_back)
    macro.record("back")
    return r


@app.post("/api/raw")
async def raw(t: Text):
    """Debug only: send one protocol line to the board as-is."""
    if not DEBUG:
        raise HTTPException(404)

    async def send():
        phone.forget_position()  # the server will not know where the pointer is, even if the board errs
        await device.send(t.text)
    return await act(send)


@app.post("/api/calibrate/start")
async def calibrate_start():
    """Calibrate the pointer. {"auto": true}: done by itself; otherwise the user clicks the pointer next."""
    check_manual()
    try:
        async with manual_lock:
            return {"ok": True, "auto": await phone.start_calibration(), "span": phone.span}
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/calibrate/finish")
async def calibrate_finish(p: Point):
    check_manual()
    if not 0 < p.x < 1 or not 0 < p.y < 1:
        raise HTTPException(422, "calibration pointer must be inside the screen")
    try:
        async with manual_lock:
            phone.finish_calibration(p.x, p.y)
            await phone.calibrate_grid()
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True, "span": phone.span}


@app.post("/api/agent")
async def agent_start(t: Text):
    if manual_lock.locked() or macro.playing or macro.recording:
        raise HTTPException(409, "phone is busy; wait for the current action")
    try:
        agent.start(t.text)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/agent/stop")
async def agent_stop():
    """Emergency stop: cancel the agent and release the mouse button."""
    agent.stop()
    macro.stop()
    await macro.wait_stopped()
    await asyncio.sleep(0.1)  # let a gesture in flight finish its own release
    # Emergency stop must stay available even while a direct action holds the lock.
    try:
        if device.connected:
            await phone.reset()
    except (DeviceError, RuntimeError) as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class Loops(BaseModel):
    loops: int = Field(default=1, ge=0, le=10000)


class ListMode(BaseModel):
    enabled: bool


@app.post("/api/macro/list-mode")
def macro_list_mode(options: ListMode):
    check_manual()
    try:
        return {"ok": True, "macro": macro.set_list_mode(options.enabled)}
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@app.post("/api/macro/record/start")
def macro_record_start():
    check_manual()
    if not device.connected or not screen.live or not phone.precision_calibrated:
        raise HTTPException(409, "请先连接手机、开启投屏并完成九点校准")
    try:
        macro.record_start()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/macro/record/stop")
async def macro_record_stop():
    async with manual_lock:
        return {"ok": True, "steps": macro.record_stop()}


@app.post("/api/macro/clear")
def macro_clear():
    check_manual()
    try:
        macro.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/macro/play")
async def macro_play(options: Loops):
    check_manual()
    if macro.recording or not phone.precision_calibrated:
        raise HTTPException(409, "请先结束录制并完成九点校准")
    try:
        macro.play(phone, options.loops)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@app.post("/api/macro/stop")
async def macro_stop():
    macro.stop()
    await macro.wait_stopped()
    if device.connected:
        try:
            await device.send("release")
        except DeviceError:
            pass
    return {"ok": True}


@app.post("/api/agent/clear")
def agent_clear():
    """Forget the last run (steps, screenshots, plan) so the page starts clean."""
    try:
        agent.clear()
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


studio_store, studio_jobs = register_studio(app, DATA_DIR, device, screen, phone, macro, check_manual)
