"""
uv and Node.js: the two things Synapse needs that are not Python packages.

Both come from the system when it has a good one, and otherwise into
`<home>/runtime/` — downloaded from their official releases, checked against
the published SHA-256, and never with sudo. The CLI puts `runtime/` first on
PATH (`synapse/cli.py:_ensure_runtime_on_path`), so the backend and every MCP
server it starts use the same Node.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from . import net

IS_WIN = sys.platform == "win32"
MIN_NODE = (20, 9, 0)
NODE_MAJOR = 22          # the LTS line a private Node comes from
UV_VERSION = "0.11.8"    # a private uv is this version; a system uv is used as it is


class RuntimeError_(Exception):
    """A runtime could not be found or fetched. The message is for the user."""


# ── platform ─────────────────────────────────────────────────────────────────

def _arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    raise RuntimeError_(f"this processor ({machine}) has no prebuilt Node.js or uv")


def glibc_version() -> tuple[int, int] | None:
    """(2, 35) on glibc Linux; None on musl or anything else."""
    try:
        name, _, version = os.confstr("CS_GNU_LIBC_VERSION").partition(" ")
        if name == "glibc":
            major, minor = version.split(".")[:2]
            return int(major), int(minor)
    except (ValueError, OSError, AttributeError):
        pass
    return None


# ── uv ───────────────────────────────────────────────────────────────────────

def find_uv(home: Path) -> Path | None:
    candidates = [os.environ.get("SYNAPSE_UV"), shutil.which("uv"),
                  str(home / "runtime" / "uv" / ("uv.exe" if IS_WIN else "uv")),
                  str(Path.home() / ".local" / "bin" / ("uv.exe" if IS_WIN else "uv")),
                  str(Path.home() / ".cargo" / "bin" / ("uv.exe" if IS_WIN else "uv"))]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _uv_target() -> str:
    arch = {"x64": "x86_64", "arm64": "aarch64"}[_arch()]
    if IS_WIN:
        return f"{arch}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    return f"{arch}-unknown-linux-{'gnu' if glibc_version() else 'musl'}"


def install_uv(home: Path, progress: Callable[[float], None] | None = None) -> Path:
    target = _uv_target()
    name = f"uv-{target}.{'zip' if IS_WIN else 'tar.gz'}"
    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}"
    dest_dir = home / "runtime" / "uv"
    with tempfile.TemporaryDirectory(dir=_mkdir(home / "runtime")) as tmp:
        archive = net.download(f"{base}/{name}", Path(tmp) / name, progress)
        expected = net.get_text(f"{base}/{name}.sha256").split()[0]
        _check_sha(archive, expected)
        unpacked = Path(tmp) / "unpacked"
        _extract(archive, unpacked)
        exe = next(unpacked.rglob("uv.exe" if IS_WIN else "uv"), None)
        if exe is None:
            raise RuntimeError_("the uv download did not contain uv")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True)
        for f in exe.parent.iterdir():
            shutil.copy2(f, dest_dir / f.name)
    uv = dest_dir / exe.name
    if not IS_WIN:
        uv.chmod(0o755)
    return uv


# ── node ─────────────────────────────────────────────────────────────────────

def node_version(node: str | Path) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=10)
        return tuple(int(x) for x in out.stdout.strip().lstrip("v").split(".")[:3])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def private_node(home: Path) -> Path:
    node = home / "runtime" / "node"
    return node / "node.exe" if IS_WIN else node / "bin" / "node"


def find_node(home: Path) -> tuple[Path, tuple[int, ...], str] | None:
    """(node, version, "system"|"private") for a Node ≥ 20.9, else None."""
    candidates: list[tuple[str, str]] = []
    if private_node(home).is_file():
        candidates.append((str(private_node(home)), "private"))
    system = shutil.which("node")
    if system:
        candidates.append((system, "system"))
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("LocalAppData", "")):
            exe = Path(base) / "nodejs" / "node.exe"
            if exe.is_file():
                candidates.append((str(exe), "system"))
    for path, source in candidates:
        version = node_version(path)
        if version and version >= MIN_NODE:
            return Path(path), version, source
    return None


def _node_platform() -> tuple[str, str]:
    arch = _arch()
    if IS_WIN:
        return f"win-{arch}", "zip"
    if sys.platform == "darwin":
        return f"darwin-{arch}", "tar.gz"
    glibc = glibc_version()
    if glibc is None or glibc < (2, 28):
        raise RuntimeError_(
            "this Linux has no official Node.js build (it needs glibc 2.28 or newer). "
            "Install Node.js 20.9 or newer yourself, then run the installer again."
        )
    return f"linux-{arch}", "tar.xz"


def install_node(home: Path, progress: Callable[[float], None] | None = None) -> tuple[Path, tuple[int, ...]]:
    plat, ext = _node_platform()
    releases = net.get_json("https://nodejs.org/dist/index.json")
    release = next(
        (r for r in releases
         if r.get("lts") and r["version"].startswith(f"v{NODE_MAJOR}.")),
        None,
    )
    if release is None:
        raise RuntimeError_(f"no Node.js {NODE_MAJOR} LTS release is listed")
    version = release["version"]
    folder = f"node-{version}-{plat}"
    base = f"https://nodejs.org/dist/{version}"
    runtime = _mkdir(home / "runtime")
    with tempfile.TemporaryDirectory(dir=runtime) as tmp:
        archive = net.download(f"{base}/{folder}.{ext}", Path(tmp) / f"{folder}.{ext}", progress)
        sums = net.get_text(f"{base}/SHASUMS256.txt")
        expected = next((line.split()[0] for line in sums.splitlines()
                         if line.endswith(f"  {folder}.{ext}")), None)
        if expected is None:
            raise RuntimeError_(f"{folder}.{ext} is not in Node's published checksums")
        _check_sha(archive, expected)
        unpacked = Path(tmp) / "unpacked"
        _extract(archive, unpacked)
        target = runtime / "node"
        if target.exists():
            shutil.rmtree(target)
        (unpacked / folder).rename(target)
    node = private_node(home)
    found = node_version(node)
    if not found:
        raise RuntimeError_("the downloaded Node.js does not run on this computer")
    return node, found


# ── helpers ──────────────────────────────────────────────────────────────────

def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_sha(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected.strip().lower():
        raise RuntimeError_(f"{path.name} did not match its published checksum")


def _extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                _safe(dest, member)
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for member in t.getmembers():
            _safe(dest, member.name)
        if hasattr(tarfile, "data_filter"):
            t.extractall(dest, filter="tar")
        else:
            t.extractall(dest)


def _safe(dest: Path, name: str) -> None:
    target = (dest / name).resolve()
    if dest.resolve() not in target.parents and target != dest.resolve():
        raise RuntimeError_(f"refusing to unpack {name!r} outside {dest}")


def version_text(version: tuple[int, ...]) -> str:
    return "v" + ".".join(str(v) for v in version)


_VERSION_IN_WHEEL = re.compile(r"^synapse_orch_ai-([^-]+)-")


def wheel_version(path: str) -> str | None:
    m = _VERSION_IN_WHEEL.match(Path(path).name)
    return m.group(1) if m else None
