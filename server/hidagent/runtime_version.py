"""Workbench handshake identity, captured before the API imports its implementation.

The runtime fingerprint must never be recomputed for a status response: Python
keeps imported code while StaticFiles reads the current files on disk.
"""

import hashlib
from pathlib import Path

API_VERSION = 1
CAPABILITIES = ("runtime-version-handshake", "controller-selection", "player-upload", "player-control")
SOURCE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT_FINGERPRINT = hashlib.sha256(str(SOURCE_ROOT).encode()).hexdigest()


def source_fingerprint(root: Path = SOURCE_ROOT) -> str:
    """Hash package Python and shipped static assets, including names and deletions.

    Ignore bytecode, recordings and other generated state. Do not use Git HEAD:
    working-tree edits and deployments without a .git directory must work too.
    """
    paths = sorted(set(root.rglob("*.py")) | {p for p in (root / "static").rglob("*") if p.is_file()})
    digest = hashlib.sha256()
    for path in paths:
        name = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


# Immutable process identity; status exposes this value, never today's disk hash.
RUNTIME_FINGERPRINT = source_fingerprint()


def runtime_is_current() -> bool:
    """For launcher/static-page guards; a missing or unreadable file fails closed."""
    try:
        return source_fingerprint() == RUNTIME_FINGERPRINT
    except OSError:
        return False


def runtime_status() -> dict:
    """Captured identity plus a live check; never relabel old Python as a new build."""
    return {
        "api_version": API_VERSION,
        "capabilities": list(CAPABILITIES),
        "build_fingerprint": RUNTIME_FINGERPRINT,
        "source_root_fingerprint": SOURCE_ROOT_FINGERPRINT,
        "installed_matches_running": runtime_is_current(),
    }


def incompatibility(status: dict) -> str | None:
    """Return the reason an existing local service must not be reused."""
    runtime = status.get("runtime")
    if not isinstance(runtime, dict):
        return "缺少运行时版本信息"
    if type(runtime.get("api_version")) is not int or runtime["api_version"] != API_VERSION:
        return "API版本不兼容或缺少版本信息"
    capabilities = runtime.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        return "缺少有效的API能力信息"
    if not set(CAPABILITIES).issubset(capabilities):
        return "API能力不兼容"
    if runtime.get("source_root_fingerprint") != SOURCE_ROOT_FINGERPRINT:
        return "服务属于另一个代码目录"
    if runtime.get("build_fingerprint") != RUNTIME_FINGERPRINT:
        return "后台运行源码与当前版本不一致（源码可能已更新）"
    if runtime.get("installed_matches_running") is not True:
        return "后台导入后源码已变更或无法核验"
    return None
