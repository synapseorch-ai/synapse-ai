"""
"Start Synapse when you log in": a systemd user unit, a LaunchAgent, or a Run key.

Each one runs `synapse start --detach --no-browser` — detached, so the service
manager is not left holding a foreground process, and without a browser, which
nobody asked to have opened at login. The unit and the LaunchAgent mark the start as theirs
(`SYNAPSE_STARTED_BY=autostart`, which `start` records in its state, F1641); the Run key carries no mark, a Windows
launch this repo cannot exercise, so a Windows autostart is recorded as a person's. The names are the ones v1.x used, so
turning it on replaces a v1.x entry instead of adding a second, and `synapse
uninstall` finds either.

Linux needs a running systemd user instance; WSL usually has none, and then the
question is not asked (`supported()`).

It is on unless the person says no (F1693), except where it cannot work or would surprise: a CI run, a container, or
no service manager (`unavailable_reason()`). A systemd user unit starts at boot only while its user lingers, so on
Linux turning it on asks logind for that, without a password and never with sudo, and records in the home's
`autostart.json` that it did; turning it off ends only a linger it began. Elsewhere it starts when the person logs in.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

IS_WIN = sys.platform == "win32"
UNIT = "synapse-ai.service"
LABEL = "com.synapse-ai.server"
RUN_VALUE = "SynapseAI"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: logind's own record of who lingers: a file per user (`loginctl enable-linger` writes it).
_LINGER_DIR = Path("/var/lib/systemd/linger")
#: Beside the install, what turning it on changed that turning it off must undo.
_STATE = "autostart.json"


@dataclass(frozen=True)
class Starts:
    """When Synapse will start: at boot (a lingering systemd user), or when the person logs in; `hint` says how to
    have it start at boot where it could not be arranged here."""
    at_boot: bool
    hint: str = ""


def _linux() -> bool:
    return not IS_WIN and sys.platform != "darwin"


def question() -> str:
    """The installer's question, in the words of when it would start here."""
    return "Start Synapse when your computer starts?" if _linux() else "Start Synapse when you log in?"


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def unavailable_reason() -> str | None:
    """Why Synapse is not set to start on its own here, or None where it can be: a CI run and a container would be
    surprised by a service, and without a service manager there is nothing to register it with."""
    if (os.environ.get("CI") or "").strip().lower() not in ("", "0", "false"):
        return "this is a CI run (CI is set)"
    if _in_container():
        return "this runs in a container"
    if not supported():
        return "there is no systemd user session here"
    return None


def said(starts: Starts) -> list[str]:
    """The lines the install and `synapse autostart on` end with: when it starts, and how to turn that off."""
    when = ("Synapse starts again when this computer restarts." if starts.at_boot
            else "Synapse starts again when you log in after a restart.")
    return [f"{when} To turn that off: synapse autostart off", *([starts.hint] if starts.hint else [])]


def _read_state(home: Path | None) -> dict:
    try:
        return json.loads((Path(home) / _STATE).read_text(encoding="utf-8")) if home else {}
    except (OSError, ValueError):
        return {}


def _lingers(user: str) -> bool:
    return (_LINGER_DIR / user).exists()


def _start_at_boot(home: Path) -> Starts:
    """A systemd user unit starts at boot only while its user lingers: ask logind, without a password and never with
    sudo, and record that this did it so `disable` ends only a linger it began. A user who already lingers is left
    alone."""
    user = getpass.getuser()
    if _lingers(user):
        return Starts(at_boot=True)
    try:
        subprocess.run(["loginctl", "enable-linger", "--no-ask-password"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass
    if _lingers(user):
        home.mkdir(parents=True, exist_ok=True)
        (home / _STATE).write_text(json.dumps({"linger_turned_on": True}), encoding="utf-8")
        return Starts(at_boot=True)
    return Starts(at_boot=False, hint=f"To start it at boot without logging in: sudo loginctl enable-linger {user}")


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
    # -P: the service manager's working directory stays off sys.path, so a
    # folder named `synapse` there can never stand in for this install's.
    return [str(python), "-P", "-m", "synapse", "start", "--detach", "--no-browser"]


def enable(python: str | Path, home: Path, path_dirs: list[Path]) -> Starts:
    """Register Synapse with this machine's service manager, and say when it will start (F1693)."""
    _register(python, home, path_dirs)
    return _start_at_boot(Path(home)) if _linux() else Starts(at_boot=False)


def _register(python: str | Path, home: Path, path_dirs: list[Path]) -> None:
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
        <key>SYNAPSE_STARTED_BY</key>
        <string>autostart</string>
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
    stop = " ".join(_systemd_quote(a) for a in cmd[:4] + ["stop"])
    unit.write_text(f"""[Unit]
Description=Synapse AI
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
Environment=SYNAPSE_HOME={_systemd_quote(str(home))}
Environment=PATH={_systemd_quote(env_path)}
Environment=SYNAPSE_STARTED_BY=autostart
ExecStart={quoted}
ExecStop={stop}
TimeoutStartSec=600

[Install]
WantedBy=default.target
""", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "enable", UNIT], capture_output=True, timeout=30)


def disable(home: Path | None = None) -> None:
    """Turn it off; on Linux, also end a linger that turning it on began (recorded in `home`), never one the person
    had before."""
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
    if _read_state(home).get("linger_turned_on"):
        try:
            subprocess.run(["loginctl", "disable-linger", "--no-ask-password"], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    if home:
        (Path(home) / _STATE).unlink(missing_ok=True)


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _systemd_quote(text: str) -> str:
    if text and all(c.isalnum() or c in "/._-:+=," for c in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
