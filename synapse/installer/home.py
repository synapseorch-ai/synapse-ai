"""
The install's home (`SYNAPSE_HOME`, `~/.synapse`) and its `install.json`.

`install.json` is what the installer knows about this install that the package
cannot say about itself: how it was installed, where uv and Node came from,
what it was migrated from, and anything left to clean up. Written atomically;
read leniently, because a half-written or hand-edited one must not stop an
upgrade.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def home_dir() -> Path:
    configured = os.environ.get("SYNAPSE_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".synapse"


def read_install(home: Path) -> dict:
    try:
        data = json.loads((home / "install.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_install(home: Path, **fields) -> dict:
    """Merge `fields` into install.json. A None value removes the key."""
    data = read_install(home)
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "install.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, home / "install.json")
    return data


class Busy(Exception):
    """Another installer is running against this home."""


class InstallLock:
    """One installer at a time per home. A lock left by a dead process is taken over."""

    def __init__(self, home: Path):
        self.path = home / "install.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pid = int(self.path.read_text().strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _alive(pid):
            raise Busy(pid)
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
