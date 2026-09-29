"""
"Start Synapse when you log in": a systemd user unit, a LaunchAgent, or a Run key.

Each one runs `synapse start --detach --no-browser` — detached, so the service
manager is not left holding a foreground process, and without a browser, which
nobody asked to have opened at login. The names are the ones v1.x used, so
turning it on replaces a v1.x entry instead of adding a second, and `synapse
uninstall` finds either.

Linux needs a running systemd user instance; WSL usually has none, and then the
question is not asked (`supported()`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
UNIT = "synapse-ai.service"
LABEL = "com.synapse-ai.server"
RUN_VALUE = "SynapseAI"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user" / UNIT


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def supported() -> bool:
    if IS_WIN or sys.platform == "darwin":
        return True
    if not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        return False
    out = subprocess.run(["systemctl", "--user", "is-system-running"],
                         capture_output=True, text=True, timeout=15)
    return out.stdout.strip() not in ("", "offline")


def is_on() -> bool:
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _plist_path().exists()
    return _unit_path().exists()


def command(python: str | Path) -> list[str]:
    """How the service manager runs Synapse: this install's own interpreter.

    The interpreter rather than a `synapse` on PATH: login shells and service
    managers each build PATH their own way, and an old install's `synapse` can
    be the one they find.
    """
    python = Path(python)
    if IS_WIN:
        windowless = python.with_name("pythonw.exe")
        python = windowless if windowless.exists() else python
    return [str(python), "-m", "synapse", "start", "--detach", "--no-browser"]


def enable(python: str | Path, home: Path, path_dirs: list[Path]) -> None:
    cmd = command(python)
    env_path = os.pathsep.join([str(d) for d in path_dirs] + [os.environ.get("PATH", "")])
    if IS_WIN:
        import subprocess as _sp
        import winreg

        value = _sp.list2cmdline(cmd)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)
        return
    if sys.platform == "darwin":
        plist = _plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"\n        <string>{_xml(a)}</string>" for a in cmd)
        (home / "logs").mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SYNAPSE_HOME</key>
        <string>{_xml(str(home))}</string>
        <key>PATH</key>
        <string>{_xml(env_path)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{_xml(str(home / "logs" / "launchd.log"))}</string>
    <key>StandardErrorPath</key>
    <string>{_xml(str(home / "logs" / "launchd.log"))}</string>
</dict>
</plist>
""", encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=30)
        subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, timeout=30)
        return
    unit = _unit_path()
    unit.parent.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(_systemd_quote(a) for a in cmd)
    stop = " ".join(_systemd_quote(a) for a in cmd[:3] + ["stop"])
    unit.write_text(f"""[Unit]
Description=Synapse AI
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=SYNAPSE_HOME={_systemd_quote(str(home))}
Environment=PATH={_systemd_quote(env_path)}
ExecStart={quoted}
ExecStop={stop}
TimeoutStartSec=600

[Install]
WantedBy=default.target
""", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "enable", UNIT], capture_output=True, timeout=30)


def disable() -> None:
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except OSError:
            pass
        return
    if sys.platform == "darwin":
        plist = _plist_path()
        if plist.exists():
            subprocess.run(["launchctl", "unload", "-w", str(plist)], capture_output=True, timeout=30)
            plist.unlink()
        return
    unit = _unit_path()
    if unit.exists():
        subprocess.run(["systemctl", "--user", "disable", UNIT], capture_output=True, timeout=30)
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _systemd_quote(text: str) -> str:
    if text and all(c.isalnum() or c in "/._-:+=," for c in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
