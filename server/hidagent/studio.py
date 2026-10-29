"""Device-specific recording plans and isolated, reproducible firmware builds."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
FQBN = "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=8M"
ID = re.compile(r"^[a-f0-9]{32}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeviceProfile(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    family: Literal["iphone", "ipad", "other-apple"]
    connector: Literal["usb-c", "lightning"] = "usb-c"
    orientation: Literal["portrait", "landscape"] = "portrait"
    width: int = Field(ge=100, le=8192)
    height: int = Field(ge=100, le=8192)

    @model_validator(mode="after")
    def geometry(self):
        if (self.orientation == "portrait") != (self.height > self.width):
            raise ValueError("横竖屏与画面尺寸不一致")
        return self

    @property
    def aspect(self):
        return self.width / self.height


class TapStep(StrictModel):
    do: Literal["tap"] = "tap"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    wait_ms: int = Field(ge=500, le=3_600_000, strict=True)
    source_index: int | None = Field(default=None, ge=0, le=255)


class Project(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=80)
    device: DeviceProfile
    steps: list[TapStep] = Field(min_length=1, max_length=256)
    loops: int = Field(default=1, ge=0, le=100000, strict=True)
    startup_ms: int = Field(default=10000, ge=3000, le=60000, strict=True)
    settle_ms: Literal[350] = 350
    press_ms: Literal[60] = 60
    release_ms: int = Field(default=90, ge=90, le=1000, strict=True)

    @model_validator(mode="after")
    def timing(self):
        minimum = max(500, self.settle_ms + self.press_ms + self.release_ms)
        for i, step in enumerate(self.steps, 1):
            if step.wait_ms < minimum:
                raise ValueError(f"第{i}步至少需要{minimum}毫秒，包含定位、按压和释放等待")
        return self

    @property
    def cycle_ms(self):
        return sum(step.wait_ms for step in self.steps)


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def check_id(value):
    if not ID.fullmatch(value):
        raise ValueError("无效的项目或任务编号")
    return value


def capture(name, profile: DeviceProfile, recording, loops=1):
    """Never silently drop an unsupported action or reuse a different screen."""
    if not recording:
        raise ValueError("还没有点击步骤，请先在目标设备上录制")
    steps = []
    for i, item in enumerate(recording):
        if item.get("do") != "tap":
            raise ValueError(f"第{i + 1}步是{item.get('do')}；当前离线版仅支持点击，请分开录制")
        aspect = item.get("guard", {}).get("aspect")
        if aspect is not None and abs(aspect / profile.aspect - 1) > 0.03:
            raise ValueError("录制画面比例与设备档案不同；切换设备或横竖屏后必须重新录制")
        wait = float(item.get("wait", 1))
        if not math.isfinite(wait) or wait < 0:
            raise ValueError("录制包含无效等待时间")
        steps.append(TapStep(x=item["x"], y=item["y"], wait_ms=max(1000, math.ceil(wait * 1000)), source_index=i))
    return Project(name=name, device=profile, steps=steps, loops=loops)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
        self.projects = self.root / "projects"
        self.jobs = self.root / "jobs"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.jobs.mkdir(parents=True, exist_ok=True)

    def save(self, project: Project, recording=None):
        folder = self.projects / check_id(project.id)
        # Guard images remain local; portable plans contain only coordinates.
        if recording is not None:
            atomic_json(folder / "recording.json", recording)
        if (folder / "project.json").exists():
            shutil.copy2(folder / "project.json", folder / "previous.json")
        atomic_json(folder / "project.json", project.model_dump())
        return project

    def get(self, identity):
        return Project.model_validate_json((self.projects / check_id(identity) / "project.json").read_text())

    def list(self):
        result = []
        for path in sorted(self.projects.glob("*/project.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                p = Project.model_validate_json(path.read_text())
                result.append(
                    {
                        "id": p.id,
                        "name": p.name,
                        "device": p.device.model_dump(),
                        "steps": len(p.steps),
                        "cycle_ms": p.cycle_ms,
                        "loops": p.loops,
                    }
                )
            except (ValueError, OSError):
                continue
        return result

    def guarded_steps(self, project):
        path = self.projects / project.id / "recording.json"
        if not path.exists():
            raise ValueError("此导入项目没有本机图像校验依据，请先在对应设备重新录制")
        raw = json.loads(path.read_text())
        result = []
        for step in project.steps:
            if step.source_index is None or step.source_index >= len(raw):
                raise ValueError("导入项目没有本机图像校验依据，请重新录制后做电脑试跑")
            source = raw[step.source_index]
            if (
                source.get("do") != "tap"
                or source.get("x") != step.x
                or source.get("y") != step.y
                or not source.get("guard")
            ):
                raise ValueError("坐标与原图像校验依据不同，请重新录制")
            result.append({**source, "wait": step.wait_ms / 1000})
        return result


def render_header(project: Project, deployment_id: str):
    check_id(deployment_id)
    data = json.dumps(project.model_dump(), sort_keys=True, ensure_ascii=False).encode()
    rows = [f"  {{{round(s.x * 32767)}, {round(s.y * 32767)}, {s.wait_ms}}}," for s in project.steps]
    return (
        "// Generated by Apple Mobile Recorder. No screenshots or network credentials.\n"
        '#pragma once\n#include "replay_engine.h"\n'
        f'constexpr char MACRO_SHA256[] = "{hashlib.sha256(data).hexdigest()}";\n'
        f'constexpr char DEPLOYMENT_ID[] = "{deployment_id}";\n'
        f'constexpr char REPLAY_PROFILE[] = "project-{project.id}";\n'
        "constexpr ReplayTiming REPLAY_TIMING = "
        f"{{500, {project.release_ms}, {project.startup_ms}, {project.loops}}};\n"
        f"constexpr uint32_t REPLAY_CYCLE_MS = {project.cycle_ms};\n"
        f"constexpr uint32_t RECORDED_CYCLE_MS = {project.cycle_ms};\n"
        "constexpr ReplayStep MACRO_STEPS[] = {\n" + "\n".join(rows) + "\n};\n"
        "constexpr size_t MACRO_COUNT = sizeof(MACRO_STEPS) / sizeof(MACRO_STEPS[0]);\n"
    )


def tools_status():
    def executable(name):
        found = shutil.which(name)
        if found:
            return found
        for folder in (Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
            candidate = folder / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    esptool = executable("esptool") or executable("esptool.py")
    if not esptool:
        found = sorted((Path.home() / "Library/Arduino15/packages/esp32/tools/esptool_py").glob("*/esptool"))
        esptool = str(found[-1]) if found else None
    uxplay = os.environ.get("UXPLAY") or executable("uxplay")
    if not uxplay:
        for candidate in (Path.home() / "UxPlay/build-desktop-diagnostic/uxplay", Path.home() / "UxPlay/build/uxplay"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                uxplay = str(candidate)
                break
    return {
        "arduino": executable("arduino-cli"),
        "esptool": esptool,
        "uxplay": uxplay,
    }


def ports():
    from serial.tools import list_ports

    return [
        {"port": p.device, "description": p.description, "serial": p.serial_number, "vid": p.vid, "pid": p.pid}
        for p in list_ports.comports()
        if p.vid is not None
    ]


class Jobs:
    """One hardware/build job at a time; never execute user-provided shell text."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.states = {}
        self.redactions = {}
        self.lock_file = None

    def _process_lock(self):
        handle = (self.store.root / "jobs.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("另一个工作台或命令行任务正在使用此数据目录") from exc
        self.lock_file = handle

    def _release_process_lock(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def get(self, identity):
        check_id(identity)
        if identity in self.states:
            return json.loads(json.dumps(self.states[identity]))
        return json.loads((self.store.jobs / identity / "job.json").read_text())

    def _save(self, job, **changes):
        job.update(changes, updated=time.time())
        atomic_json(self.store.jobs / job["id"] / "job.json", job)

    def _run(self, job, argv, timeout=600):
        # The command is an argv list. Wi-Fi secrets are not logged or returned.
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        output = result.stdout + result.stderr
        for secret in self.redactions.get(job["id"], []):
            if secret:
                output = output.replace(secret, "***")
        folder = self.store.jobs / job["id"]
        with (folder / "tools.log").open("a") as log:
            log.write(output + "\n")
        self._save(job, log=output[-5000:])
        if result.returncode:
            raise RuntimeError(f"工具执行失败（退出码{result.returncode}），请看任务日志")
        return output

    def start(self, kind, project=None, **options):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("另一个生成或烧录任务正在执行，请等它完成")
        try:
            self._process_lock()
        except Exception:
            self.lock.release()
            raise
        identity = uuid.uuid4().hex
        job = {"id": identity, "kind": kind, "status": "queued", "stage": "准备", "log": "", "created": time.time()}
        self.states[identity] = job
        snapshot = project.model_copy(deep=True) if project else None
        try:
            self._save(job)
        except Exception:
            self._release_process_lock()
            self.lock.release()
            raise

        def work():
            try:
                self._save(job, status="running")
                if kind == "build":
                    self._build(job, snapshot, **options)
                elif kind == "flash":
                    self._flash(job, **options)
                elif kind == "restore":
                    self._restore(job, **options)
                else:
                    raise ValueError("未知任务类型")
                self._save(job, status="succeeded", stage="完成")
            except Exception as exc:
                self._save(job, status="failed", error=str(exc), stage="失败；保留日志与备份")
            finally:
                self.redactions.pop(identity, None)
                self._release_process_lock()
                self.lock.release()

        threading.Thread(target=work, daemon=True).start()
        return job

    def _build(self, job, project, mode="offline", network=None):
        tools = tools_status()
        if not tools["arduino"]:
            raise RuntimeError("未找到 arduino-cli，请先运行 scripts/setup-macos.sh")
        folder = self.store.jobs / job["id"]
        out = folder / "firmware"
        if mode == "offline":
            sketch = folder / "OfflinePlayer"
            shutil.copytree(
                ROOT / "hardware/offline_replay", sketch, ignore=shutil.ignore_patterns("build", "README.md")
            )
            (sketch / "offline_replay.ino").rename(sketch / "OfflinePlayer.ino")
            (sketch / "macro_data.h").write_text(render_header(project, job["id"]))
            atomic_json(folder / "project.json", project.model_dump())
            arguments = []
        elif mode == "desktop":
            if not network or not all(network.get(key) for key in ("ssid", "host")):
                raise ValueError("录制模式需要 Wi-Fi 名称和电脑的局域网地址")
            sketch = folder / "DesktopBridge"
            shutil.copytree(ROOT / "hardware/hid_bridge", sketch, ignore=shutil.ignore_patterns("build", "secrets.h"))
            (sketch / "hid_bridge.ino").rename(sketch / "DesktopBridge.ino")
            secrets = {
                "WIFI_SSID": network["ssid"],
                "WIFI_PASS": network.get("password", ""),
                "SERVER_HOST": network["host"],
                "SERVER_PORT": int(network.get("port", 7400)),
            }
            self.redactions[job["id"]] = [network.get("password", "")]
            (sketch / "secrets.h").write_text(
                "\n".join(f"#define {k} {json.dumps(v, ensure_ascii=True)}" for k, v in secrets.items()) + "\n"
            )
            arguments = ["--build-property", "compiler.cpp.extra_flags=-DABSOLUTE_POINTER=1"]
        else:
            raise ValueError("未知固件模式")
        self._save(job, stage="编译固件（首次会比较慢）", mode=mode)
        self._run(
            job, [tools["arduino"], "compile", "--fqbn", FQBN, "--output-dir", str(out), *arguments, str(sketch)], 600
        )
        binary = out / f"{sketch.name}.ino.bin"
        manifest = {
            "schema_version": 1,
            "mode": mode,
            "fqbn": FQBN,
            "application_offset": 65536,
            "application_file": binary.name,
            "application_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "bytes": binary.stat().st_size,
            "project_id": project.id if project else None,
            "steps": len(project.steps) if project else 0,
            "cycle_ms": project.cycle_ms if project else 0,
            "loops": project.loops if project else None,
            "startup_ms": project.startup_ms if project else None,
            "deployment_id": job["id"],
            "contains_network_credentials": mode == "desktop",
        }
        if project:
            manifest["project_sha256"] = hashlib.sha256(
                json.dumps(project.model_dump(), sort_keys=True).encode()
            ).hexdigest()
        atomic_json(folder / "manifest.json", manifest)
        self._save(job, manifest=manifest, stage="固件已生成；尚未写入板子")

    def _flash(self, job, build_id, port, initialize=False):
        build = self.get(build_id)
        if build.get("kind") != "build" or build.get("status") != "succeeded":
            raise ValueError("请先成功生成固件")
        if port not in {p["port"] for p in ports()}:
            raise ValueError("请选择当前已连接的 COM 烧录口")
        if os.name != "nt" and shutil.which("lsof"):
            busy = subprocess.run(["lsof", "-t", port], capture_output=True, text=True)
            if busy.stdout.strip():
                raise ValueError("COM 串口被其他程序占用，请先关闭串口监视器；不会擅自结束其他程序")
        tools = tools_status()
        if not tools["esptool"]:
            raise RuntimeError("未找到 esptool，请运行环境检查")
        source = self.store.jobs / build_id
        manifest = json.loads((source / "manifest.json").read_text())
        if manifest.get("project_id"):
            latest = self.store.get(manifest["project_id"])
            latest_hash = hashlib.sha256(json.dumps(latest.model_dump(), sort_keys=True).encode()).hexdigest()
            if latest_hash != manifest.get("project_sha256"):
                raise ValueError("此项目的参数已修改，请重新生成固件，避免烧录旧参数")
        out = source / "firmware"
        binary = out / manifest["application_file"]
        if hashlib.sha256(binary.read_bytes()).hexdigest() != manifest["application_sha256"]:
            raise ValueError("固件文件已变化，禁止烧录，请重新生成")
        base = [tools["esptool"], "--chip", "esp32s3", "--port", port, "--baud", "115200", "--no-stub"]

        def command(*args, after="no-reset", timeout=600):
            return self._run(job, [*base, "--after", after, *map(str, args)], timeout)

        self._save(job, stage="识别 ESP32-S3（会停止现有程序）", build_id=build_id, port=port)
        identity = command("chip-id")
        mac = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", identity)
        if not mac:
            raise RuntimeError("未读取到板卡 MAC，停止烧录")
        backup = self.store.jobs / job["id"] / "backup"
        backup.mkdir()
        self._save(job, stage="备份启动区与分区表")
        command("read-flash", "0x0", "0x10000", backup / "boot-and-partitions.bin")
        original = (backup / "boot-and-partitions.bin").read_bytes()
        expected = next(out.glob("*.partitions.bin")).read_bytes()
        actual = original[0x8000 : 0x8000 + len(expected)]
        empty = original == b"\xff" * 65536
        if actual != expected and not (initialize and empty):
            raise ValueError("现有分区与本项目不同，未写入。空板首次使用请明确选择“初始化空板”")
        # Read the trusted build table, never arbitrary offsets from the device.
        app_size = None
        for offset in range(0, len(expected) - 31, 32):
            magic, kind, _, address, size = struct.unpack_from("<HBBII", expected, offset)
            if magic == 0x50AA and kind == 0 and address == 0x10000:
                app_size = size
                break
        if app_size is None or not 0 < binary.stat().st_size <= app_size <= 4 * 1024 * 1024:
            raise ValueError("固件大小或应用分区不兼容")
        self._save(job, stage="备份应用区（115200串口首次可能需要数分钟）")
        command("read-flash", "0x10000", hex(app_size), backup / "application.bin", timeout=900)
        restore = {
            "mac": mac.group(1).lower(),
            "application_offset": 65536,
            "application_bytes": app_size,
            "application_sha256": hashlib.sha256((backup / "application.bin").read_bytes()).hexdigest(),
            "boot_sha256": hashlib.sha256(original).hexdigest(),
            "initialized_empty_board": empty,
        }
        atomic_json(backup / "restore.json", restore)
        self._save(job, stage="写入并校验固件", board_mac=restore["mac"], backup_ready=True)
        segments = ["0x10000", str(binary)]
        if initialize and empty:
            bootloader = next(out.glob("*.bootloader.bin"))
            table = next(out.glob("*.partitions.bin"))
            candidates = list(
                (Path.home() / "Library/Arduino15/packages/esp32/hardware/esp32").glob(
                    "*/tools/partitions/boot_app0.bin"
                )
            )
            if not candidates:
                raise RuntimeError("未找到 boot_app0.bin；保留备份，未初始化空板")
            segments = ["0x0", str(bootloader), "0x8000", str(table), "0xe000", str(sorted(candidates)[-1]), *segments]
        output = command("write-flash", *segments, after="hard-reset")
        if "Hash of data verified" not in output:
            raise RuntimeError("未获得写入哈希校验确认，请保留备份检查板卡")
        self._save(
            job,
            stage="写入成功；离线固件首次保持待机，重插HID后启用",
            flash_hash_verified=True,
            manifest=manifest,
            restore=restore,
        )

    def _restore(self, job, flash_id, port):
        previous = self.get(flash_id)
        if previous.get("kind") != "flash" or not previous.get("backup_ready"):
            raise ValueError("该任务没有可用的烧录前备份")
        if port not in {p["port"] for p in ports()}:
            raise ValueError("请选择已连接的COM口")
        backup = self.store.jobs / flash_id / "backup"
        info = json.loads((backup / "restore.json").read_text())
        app = backup / "application.bin"
        boot = backup / "boot-and-partitions.bin"
        if hashlib.sha256(app.read_bytes()).hexdigest() != info["application_sha256"]:
            raise ValueError("应用备份校验失败，禁止恢复")
        if hashlib.sha256(boot.read_bytes()).hexdigest() != info["boot_sha256"]:
            raise ValueError("启动区备份校验失败，禁止恢复")
        executable = tools_status()["esptool"]
        if not executable:
            raise RuntimeError("未找到esptool")
        base = [executable, "--chip", "esp32s3", "--port", port, "--baud", "115200", "--no-stub"]
        self._save(job, stage="核对板卡身份，准备恢复", flash_id=flash_id, port=port)
        identity = self._run(job, [*base, "--after", "no-reset", "chip-id"])
        mac = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", identity)
        if not mac or mac.group(1).lower() != info["mac"]:
            raise ValueError("板卡MAC与备份不一致，禁止恢复到另一块板子")
        segments = ["0x10000", str(app)]
        if info["initialized_empty_board"]:
            segments = ["0x0", str(boot), *segments]
        self._save(job, stage="恢复烧录前应用并校验")
        output = self._run(job, [*base, "--after", "hard-reset", "write-flash", *segments], 900)
        if "Hash of data verified" not in output:
            raise RuntimeError("恢复未得到哈希校验确认")
        self._save(job, flash_hash_verified=True, stage="应用已恢复；原NVS数据未改写")
