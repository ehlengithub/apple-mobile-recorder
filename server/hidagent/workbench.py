"""Apple Mobile Recorder command line. No device commands run on startup."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

from .studio import Jobs, Project, Store, ports, tools_status


def main(argv=None):
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data", type=Path, default=Path(os.environ.get("HIDAGENT_DATA", Path.home() / ".apple-mobile-recorder"))
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8990)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--without-receiver", action="store_true")
    build = commands.add_parser("build")
    build.add_argument("project", type=Path)
    flash = commands.add_parser("flash")
    flash.add_argument("build_id")
    flash.add_argument("--port", required=True)
    flash.add_argument("--initialize", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("flash_id")
    restore.add_argument("--port", required=True)
    import_project = commands.add_parser("import")
    import_project.add_argument("project", type=Path)
    transfer = commands.add_parser("upload", help="只上传录制数据，不重新烧录固件")
    transfer.add_argument("project", type=Path)
    transfer.add_argument("--port", required=True)
    transfer.add_argument("--mac", required=True)
    transfer.add_argument("--hid-disconnected", action="store_true", required=True)
    control = commands.add_parser("player", help="启用离线执行或切回电脑录制")
    control.add_argument("action", choices=("arm", "desktop", "stop"))
    control.add_argument("--project", type=Path)
    control.add_argument("--port", required=True)
    control.add_argument("--mac", required=True)
    control.add_argument("--hid-disconnected", action="store_true", required=True)
    export = commands.add_parser("export-recording", help="导出独立AMR2录制数据")
    export.add_argument("project", type=Path)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ["HIDAGENT_DATA"] = str(args.data.resolve())
    os.environ.setdefault("DEVICE_PORT", "7400")
    os.environ.setdefault("AIRPLAY_NAME", "Apple Recorder")
    try:
        if args.command == "doctor":
            tools = tools_status()
            core = (
                subprocess.run([tools["arduino"], "core", "list"], capture_output=True, text=True)
                if tools["arduino"]
                else None
            )
            print(
                json.dumps(
                    {
                        "python": sys.version.split()[0],
                        "tools": tools,
                        "ports": ports(),
                        "esp32_core": core.stdout if core else "未安装",
                        "data": str(args.data.resolve()),
                        "hardware_touched": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        store = Store(args.data / "studio")
        if args.command == "export-recording":
            from .player import encode_recording

            project = Project.model_validate_json(args.project.read_text())
            args.output.write_bytes(encode_recording(project))
            print(f"已导出{len(project.steps)}步录制；未操作硬件")
            return 0
        if args.command == "import":
            project = Project.model_validate_json(args.project.read_text())
            project.id = uuid.uuid4().hex
            store.save(project)
            print(
                json.dumps(
                    {"project_id": project.id, "steps": len(project.steps), "cycle_ms": project.cycle_ms},
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command in ("build", "flash", "restore", "upload", "player"):
            manager = Jobs(store)
            if args.command == "build":
                project = Project.model_validate_json(args.project.read_text())
                store.save(project)
                job = manager.start("build", project, mode="offline")
            elif args.command == "upload":
                project = Project.model_validate_json(args.project.read_text())
                job = manager.start("upload", project, port=args.port, expected_mac=args.mac)
            elif args.command == "player":
                project = Project.model_validate_json(args.project.read_text()) if args.project else None
                if args.action == "arm" and project is None:
                    raise ValueError("启用前必须通过--project指定对应录制")
                job = manager.start(
                    "player-control", project, port=args.port, expected_mac=args.mac, command=args.action
                )
            elif args.command == "flash":
                job = manager.start("flash", build_id=args.build_id, port=args.port, initialize=args.initialize)
            else:
                job = manager.start("restore", flash_id=args.flash_id, port=args.port)
            last_stage = None
            while True:
                state = manager.get(job["id"])
                if state["stage"] != last_stage:
                    print(state["stage"], file=sys.stderr, flush=True)
                    last_stage = state["stage"]
                if state["status"] in ("failed", "succeeded"):
                    print(json.dumps(state, ensure_ascii=False, indent=2))
                    return 0 if state["status"] == "succeeded" else 2
                time.sleep(0.2)
        import uvicorn

        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/studio/status", timeout=1) as existing:
                current = json.load(existing)
            if current.get("product") == "AppleMobileRecorder":
                fingerprint = hashlib.sha256(str(args.data.resolve()).encode()).hexdigest()
                if current.get("data_fingerprint") != fingerprint:
                    raise RuntimeError("此端口属于另一个数据目录，请选择不同的--port")
                if not args.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{args.port}/")
                print("工作台已运行，已复用现有服务。")
                return 0
        except (OSError, ValueError):
            pass

        receiver = None
        env = os.environ.copy()
        if not args.without_receiver and not os.environ.get("MIRROR_STREAM"):
            executable = tools_status()["uxplay"]
            if not executable:
                raise RuntimeError("未找到UxPlay，请先运行准备环境.command或设置UXPLAY路径")
            env["UXPLAY"] = executable
            env.setdefault("MIRROR_PORT", "8991")
            os.environ.setdefault("MIRROR_STREAM", "127.0.0.1:" + env["MIRROR_PORT"])
            receiver = subprocess.Popen([sys.executable, "-m", "hidagent.receiver"], env=env)
        if not args.no_browser:
            threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
        try:
            uvicorn.run("hidagent.app:app", host="127.0.0.1", port=args.port, access_log=False)
        finally:
            if receiver and receiver.poll() is None:
                receiver.send_signal(signal.SIGTERM)
                try:
                    receiver.wait(5)
                except subprocess.TimeoutExpired:
                    receiver.kill()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
