"""Local workbench routes. Explicit build and flash actions are separate."""

import copy
import hashlib
import io
import json
import os
import socket
import uuid

from fastapi import HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import Field

from .studio import DeviceProfile, Jobs, Project, Store, StrictModel, capture, check_id, ports, tools_status


class CaptureRequest(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    device: DeviceProfile
    loops: int = Field(default=1, ge=0, le=100000, strict=True)


class Network(StrictModel):
    ssid: str = Field(min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)
    host: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9.:-]+$")
    port: int = Field(default=7400, ge=1024, le=65535)


class BuildRequest(StrictModel):
    mode: str = Field(default="offline", pattern=r"^(offline|desktop)$")
    project_id: str | None = None
    network: Network | None = None


class FlashRequest(StrictModel):
    build_id: str
    port: str
    initialize: bool = False


class RestoreRequest(StrictModel):
    flash_id: str
    port: str


def register(app, data_dir, device, screen, phone, macro, check_manual):
    store = Store(data_dir / "studio")
    jobs = Jobs(store)

    def idle():
        check_manual()
        if macro.recording or macro.playing:
            raise HTTPException(409, "请先结束录制或停止电脑回放")

    from .player_api import register as register_player

    register_player(app, store, jobs, idle)

    def get_project(identity):
        try:
            return store.get(identity)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "没有找到该录制项目") from exc

    @app.get("/api/studio/status")
    def status():
        shape = None
        if screen.live and screen.frame:
            with Image.open(io.BytesIO(screen.frame)) as im:
                shape = {"width": im.width, "height": im.height}
        lan_host = ""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                lan_host = probe.getsockname()[0]
        except OSError:
            pass
        return {
            "product": "AppleMobileRecorder",
            "data_fingerprint": hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest(),
            "airplay_name": os.environ.get("AIRPLAY_NAME", "Apple Recorder"),
            "lan_host": lan_host,
            "device_port": int(os.environ.get("DEVICE_PORT", 7400)),
            "tools": {key: bool(value) for key, value in tools_status().items()},
            "ports": ports(),
            "frame": shape,
            "projects": store.list(),
            "busy": jobs.lock.locked(),
            "offline_actions": ["tap"],
            "compatibility": "iPad14,3实测；其他机型需分别录制并验收",
        }

    @app.get("/api/studio/projects")
    def project_list():
        return store.list()

    @app.get("/api/studio/projects/{identity}")
    def project_get(identity: str):
        return get_project(identity).model_dump()

    @app.post("/api/studio/capture")
    def project_capture(request: CaptureRequest):
        idle()
        try:
            project = capture(request.name, request.device, macro.steps, request.loops)
            return store.save(project, copy.deepcopy(macro.steps)).model_dump()
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/studio/import")
    def project_import(project: Project):
        idle()
        # Import cannot overwrite another project's local guard images.
        project.id = uuid.uuid4().hex
        return store.save(project).model_dump()

    @app.put("/api/studio/projects/{identity}")
    def project_save(identity: str, project: Project):
        idle()
        previous = get_project(identity)
        if project.id != identity:
            raise HTTPException(422, "项目编号不一致")
        if previous.device != project.device:
            raise HTTPException(422, "设备档案不能与录制分离修改；新设备请新建并重新录制")
        return store.save(project).model_dump()

    @app.post("/api/studio/projects/{identity}/play")
    async def project_play(identity: str):
        idle()
        project = get_project(identity)
        if not device.connected or not screen.live or not getattr(device, "absolute", False):
            raise HTTPException(409, "请先连接录制模式固件、开启投屏，并使用绝对定位")
        if abs(screen.aspect / project.device.aspect - 1) > 0.03:
            raise HTTPException(409, "当前画面比例与此设备档案不同，请切回原设备和方向")
        try:
            steps = store.guarded_steps(project)
            macro._save(steps)
            macro.steps = steps
            macro.play(phone, 1)
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "loops": 1}

    @app.post("/api/studio/build")
    def build(request: BuildRequest):
        idle()
        project = get_project(request.project_id) if request.project_id else None
        try:
            return jobs.start(
                "build", project, mode=request.mode, network=request.network.model_dump() if request.network else None
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/studio/flash")
    def flash(request: FlashRequest):
        idle()
        try:
            return jobs.start("flash", **request.model_dump())
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/studio/jobs")
    def job_history():
        # Read persisted metadata, so a page or server restart cannot hide backups.
        # Deliberately omit logs, source paths and network configuration.
        result = []
        fields = (
            "kind", "status", "stage", "created", "updated", "mode", "port", "board_mac", "build_id", "flash_id",
            "command", "project_id",
        )
        for path in store.jobs.glob("*/job.json"):
            try:
                identity = check_id(path.parent.name)
                job = json.loads(path.read_text())
                if not isinstance(job, dict) or job.get("id") != identity:
                    continue
                if job.get("kind") not in ("build", "flash", "restore", "upload", "player-control"):
                    continue
                if not isinstance(job.get("created", 0), (int, float)):
                    continue
                item = {key: job[key] for key in fields if key in job}
                item["id"] = identity
                item["backup_ready"] = job.get("kind") == "flash" and job.get("backup_ready") is True
                backup = path.parent / "backup"
                item["backup_available"] = item["backup_ready"] and all(
                    (backup / name).is_file()
                    for name in ("restore.json", "application.bin", "boot-and-partitions.bin")
                )
                # Persisted 'running' alone is not proof that a worker survived a restart.
                item["running_here"] = (
                    identity in jobs.states and jobs.lock.locked() and job.get("status") in ("queued", "running")
                )
                manifest = job.get("manifest")
                if isinstance(manifest, dict):
                    item["mode"] = manifest.get("mode", item.get("mode"))
                    item["recording_protocol"] = manifest.get("recording_protocol")
                recording = job.get("recording")
                if isinstance(recording, dict):
                    item["recording"] = {
                        key: recording[key]
                        for key in ("project_id", "name", "size", "steps", "cycle_ms", "loops", "crc")
                        if key in recording and isinstance(recording[key], (str, int, float))
                    }
                result.append(item)
            except (ValueError, OSError):
                continue
        return sorted(result, key=lambda item: item.get("created", 0), reverse=True)

    @app.get("/api/studio/jobs/{identity}")
    def job_get(identity: str):
        try:
            return jobs.get(identity)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(404, "没有找到该任务") from exc

    @app.post("/api/studio/restore")
    def restore(request: RestoreRequest):
        idle()
        try:
            return jobs.start("restore", **request.model_dump())
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/studio/jobs/{identity}/application.bin")
    def download(identity: str):
        job = job_get(identity)
        if job.get("kind") != "build" or job.get("status") != "succeeded":
            raise HTTPException(409, "固件尚未生成成功")
        manifest = job["manifest"]
        path = store.jobs / identity / "firmware" / manifest["application_file"]
        return FileResponse(path, filename=f"apple-recorder-{identity[:8]}.bin", media_type="application/octet-stream")

    return store, jobs
