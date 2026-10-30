"""Explicit recording transfer endpoints, separate from firmware flashing."""

from typing import Literal

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import Field, model_validator

from .controllers import Controller
from .player import encode_recording
from .studio import StrictModel


class Manage(StrictModel):
    port: str
    expected_mac: str = Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
    controller: Controller = "usb-s3"
    hid_disconnected: bool = Field(default=False, strict=True)
    maintenance_confirmed: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def safe_to_open(self):
        if self.controller == "usb-s3" and not self.hid_disconnected:
            raise ValueError("操作前必须先拔下USB/HID并确认")
        if self.controller == "ble-esp32" and not self.maintenance_confirmed:
            raise ValueError("操作前必须先长按BOOT两秒进入蓝牙维护模式并确认")
        return self


class Transfer(Manage):
    project_id: str


class Control(Manage):
    command: Literal["arm", "desktop", "stop"]
    project_id: str | None = None


def register(app, store, jobs, idle):
    def project(identity):
        try:
            return store.get(identity)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "没有找到该录制项目") from exc

    @app.get("/api/studio/projects/{identity}/recording.amr")
    def download(identity: str):
        return Response(
            encode_recording(project(identity)),
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="recording.amr"'},
        )

    @app.post("/api/studio/player/upload")
    def upload(request: Transfer):
        idle()
        plan = project(request.project_id)
        try:
            return jobs.start(
                "upload", plan, port=request.port, expected_mac=request.expected_mac, controller=request.controller
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/studio/player/control")
    def control(request: Control):
        idle()
        plan = project(request.project_id) if request.project_id else None
        if request.command == "arm" and plan is None:
            raise HTTPException(422, "启用前请选择对应录制项目")
        try:
            return jobs.start(
                "player-control",
                plan,
                port=request.port,
                expected_mac=request.expected_mac,
                command=request.command,
                controller=request.controller,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
