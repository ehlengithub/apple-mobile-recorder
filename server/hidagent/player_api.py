"""Explicit recording transfer endpoints, separate from firmware flashing."""

from typing import Literal

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import Field

from .player import encode_recording
from .studio import StrictModel


class Transfer(StrictModel):
    project_id: str
    port: str
    expected_mac: str = Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
    hid_disconnected: Literal[True]


class Control(StrictModel):
    command: Literal["arm", "desktop", "stop"]
    project_id: str | None = None
    port: str
    expected_mac: str = Field(pattern=r"^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$")
    hid_disconnected: Literal[True]


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
            return jobs.start("upload", plan, port=request.port, expected_mac=request.expected_mac)
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
                "player-control", plan, port=request.port, expected_mac=request.expected_mac, command=request.command
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
