"""
Finding a Synapse 1.x on this computer, and what it would take to move it.

v1.x had three installers and they put things in different places:

* **script** (`setup.sh` / `setup.ps1`): code in `~/.local/share/SynapseAI`
  (or `…/synapse-ai`, `~/Library/Application Support/SynapseAI`,
  `%LOCALAPPDATA%\\Programs\\SynapseAI`), data in `<dir>/backend/data` as its
  `.env` says, logs in `<dir>/backend/logs`, PID files in the data folder.
* **pip**: data in `~/.synapse/data`, but chats, schedules and logs beside the
  code in `site-packages/backend/`; `.env` in site-packages.
* **npm**: data in `~/.synapse/data`, a venv in `~/.synapse/venv`, chats and
  logs inside the npm package folder.

`find()` returns one `Migration` covering all of them — the folders to import
in priority order, the files to back up, the processes to stop and the
leftovers to remove — plus a count of what is there, for the one screen that
says what was found.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_WIN = sys.platform == "win32"

#: What a v1.x data folder holds that is worth moving (the importer's list).
_JSON_SOURCES = ("orchestrations", "user_agents", "custom_tools", "mcp_servers",
                 "schedules", "usage_logs", "model_pricing", "settings")
_CONTENT_DIRS = ("chat_sessions", "vault", "datasets")
_LOG_DIRS = ("orchestration_logs", "agent_logs", "orchestration_runs")

#: .env keys v2 no longer reads, or that would point it at the old install.
_DROP_ENV = {"SYNAPSE_DATA_DIR", "NEXT_PUBLIC_BACKEND_PORT", "DEFAULT_TENANT_ID",
             "ENABLE_TENANT_ISOLATION", "NUM_QUEUE_SHARDS", "WORKER_QUEUE_SHARD",
             "WORKER_FILESYSTEM_PATHS"}


@dataclass
class OldInstall:
    kind: str                 # "script" | "pip" | "npm"
    root: Path                # the code: install dir, site-packages, npm package
    version: str = ""
    bin_dir: Path | None = None      # what v1.x put on PATH (script installs)
    python: str = ""                 # the interpreter a pip install runs under
    script: str = ""                 # its `synapse` command


@dataclass
class Migration:
    installs: list[OldInstall] = field(default_factory=list)
    data_dirs: list[Path] = field(default_factory=list)     # highest priority first
    logs_dirs: list[Path] = field(default_factory=list)
    env_files: list[Path] = field(default_factory=list)
    pid_files: list[Path] = field(default_factory=list)
    leftovers: list[Path] = field(default_factory=list)     # removed at the end
    counts: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)            # the old settings.json

    @property
    def found(self) -> bool:
        return bool(self.data_dirs or self.logs_dirs or self.installs)

    @property
    def version(self) -> str:
        versions = [i.version for i in self.installs if i.version]
        return versions[0] if versions else "1.x"

    @property
    def where(self) -> Path | None:
        return self.installs[0].root if self.installs else (self.data_dirs[0] if self.data_dirs else None)

    def has_model(self) -> bool:
        """Whether the old install had an AI chosen, so the question can be skipped."""
        model = self.settings.get("model")
        return bool(model) and model != "ollama.mistral"


# ── discovery ────────────────────────────────────────────────────────────────

def script_dirs() -> list[Path]:
    home = Path.home()
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return [base / "Programs" / "SynapseAI"]
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "SynapseAI"]
    return [home / ".local" / "share" / "SynapseAI", home / ".local" / "share" / "synapse-ai"]


def _version_of(root: Path) -> str:
    for name, pattern in (("pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
                          ("package.json", r'"version"\s*:\s*"([^"]+)"')):
        try:
            m = re.search(pattern, (root / name).read_text(encoding="utf-8"), re.M)
            if m:
                return m.group(1)
        except OSError:
            continue
    return ""


def _read_env(path: Path) -> dict:
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def has_data(root: Path) -> bool:
    if not root.is_dir():
        return False
    if any((root / f"{n}.json").is_file() for n in _JSON_SOURCES):
        return True
    return any((root / d).is_dir() and any((root / d).iterdir()) for d in _CONTENT_DIRS)


def has_logs(root: Path) -> bool:
    return root.is_dir() and any((root / d).is_dir() for d in _LOG_DIRS)


def _script_install(root: Path) -> OldInstall | None:
    if (root / ".git").exists():
        return None              # a checkout is somebody's working tree, not an install
    if not (root / "backend").is_dir():
        return None
    version = _version_of(root)
    # The marker a finished v1.x install wrote, or a v1.x tree without one (an
    # install that stopped half way). A 2.x source tree with neither is only
    # the carrier an old setup.sh unpacked to run setup.py, not an install.
    if not ((root / ".installed").exists() or version.startswith("1.")):
        return None
    return OldInstall("script", root, version, root / "bin")


def _python_site_root(python: str) -> Path | None:
    """Where `python` imports `synapse` from (the parent of the package)."""
    try:
        out = subprocess.run(
            [python, "-c", "import synapse, pathlib; print(pathlib.Path(synapse.__file__).resolve().parent.parent)"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip().splitlines()[-1:] if out.returncode == 0 else []
    return Path(line[0]) if line else None


def _pip_installs(exclude: set[Path]) -> list[OldInstall]:
    """v1.x pip installs, found through the `synapse` commands on PATH."""
    found: list[OldInstall] = []
    seen: set[Path] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        exe = Path(entry) / ("synapse.exe" if IS_WIN else "synapse")
        if not exe.is_file():
            continue
        python = _shebang_python(exe)
        if not python:
            continue
        root = _python_site_root(python)
        if root is None or root in seen or any(root == e or e in root.parents for e in exclude):
            continue
        seen.add(root)
        if "site-packages" not in root.parts and "dist-packages" not in root.parts:
            continue
        version = _dist_version(python)
        if version.startswith("1."):
            found.append(OldInstall("pip", root, version, python=python, script=str(exe)))
    return found


def _shebang_python(exe: Path) -> str | None:
    if IS_WIN:
        # A pip console script on Windows is a launcher .exe in Scripts\, beside
        # a venv's python.exe or one level below a system Python's.
        for candidate in (exe.parent / "python.exe", exe.parent.parent / "python.exe"):
            if candidate.is_file():
                return str(candidate)
        return None
    try:
        first = exe.read_bytes()[:256].split(b"\n", 1)[0].decode("utf-8", "replace")
    except OSError:
        return None
    if first.startswith("#!") and "python" in first:
        return first[2:].strip().split()[0]
    return None


def _dist_version(python: str) -> str:
    try:
        out = subprocess.run(
            [python, "-c", "import importlib.metadata as m; print(m.version('synapse-orch-ai'))"],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _npm_install(home: Path) -> OldInstall | None:
    """A v1.x npm install: its venv in ~/.synapse, its package under npm's root."""
    if not (home / "venv").is_dir():
        return None
    root = None
    npm = shutil.which("npm.cmd" if IS_WIN else "npm") or shutil.which("npm")
    if npm:
        try:
            out = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30)
            candidate = Path(out.stdout.strip()) / "synapse-orch-ai"
            if out.returncode == 0 and candidate.is_dir():
                root = candidate
        except (OSError, subprocess.SubprocessError):
            pass
    install = OldInstall("npm", root or home / "venv", _version_of(root) if root else "")
    if install.version and not install.version.startswith("1."):
        # Already the 2.x wrapper: npm replaced the old package, and whatever
        # v1.x kept inside it went with it. The venv is still worth removing.
        install.root = home / "venv"
    return install


def find(home: Path, tool_root: Path | None = None) -> Migration:
    """Everything v1.x left on this computer. `tool_root`: our own new install, skipped."""
    m = Migration()
    exclude = {p for p in (tool_root,) if p}

    for root in script_dirs():
        install = _script_install(root)
        if install is None:
            continue
        m.installs.append(install)
        env = _read_env(root / ".env")
        m.env_files.append(root / ".env")
        configured = env.get("SYNAPSE_DATA_DIR", "")
        if configured:
            data = Path(configured)
            m.data_dirs.append(data if data.is_absolute() else root / data)
        m.data_dirs.append(root / "backend" / "data")
        m.logs_dirs.append(root / "backend" / "logs")
        m.leftovers.append(root)

    for install in _pip_installs(exclude | {i.root for i in m.installs}):
        m.installs.append(install)
        m.data_dirs.append(install.root / "backend" / "data")
        m.logs_dirs.append(install.root / "backend" / "logs")
        m.env_files.append(install.root / ".env")

    npm = _npm_install(home)
    if npm is not None:
        m.installs.append(npm)
        if npm.root != home / "venv":
            m.data_dirs.append(npm.root / "backend" / "data")
            m.logs_dirs.append(npm.root / "backend" / "logs")
        m.leftovers += [home / "venv", home / "requirements.hash"]

    # The pip and npm data folder — and a script install's, when its .env did
    # not name one. After the installs' own folders: those are what they ran with.
    m.data_dirs.append(home / "data")

    m.data_dirs = _unique([d for d in m.data_dirs if has_data(d)])
    m.logs_dirs = _unique([d for d in m.logs_dirs if has_logs(d)])
    m.env_files = _unique([f for f in m.env_files if f.is_file()])
    for d in m.data_dirs:
        m.pid_files += [d / "backend.pid", d / "frontend.pid"]
    m.counts = count(m.data_dirs, m.logs_dirs)
    for d in m.data_dirs:
        settings = _json(d / "settings.json")
        if isinstance(settings, dict) and settings:
            m.settings = settings
            break
    return m


def _unique(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def count(data_dirs: list[Path], logs_dirs: list[Path]) -> dict:
    """What the confirm screen names: agents, orchestrations, chats, …"""
    ids: dict[str, set] = {k: set() for k in ("agents", "orchestrations", "schedules",
                                               "tools", "mcp_servers", "api_keys")}
    chats: set[str] = set()
    vault_bytes = 0
    for d in data_dirs:
        for key, name, field_ in (("agents", "user_agents", "id"), ("orchestrations", "orchestrations", "id"),
                                  ("schedules", "schedules", "id"), ("tools", "custom_tools", "id"),
                                  ("mcp_servers", "mcp_servers", "name"), ("api_keys", "api_keys", "id")):
            items = _json(d / f"{name}.json")
            if isinstance(items, list):
                ids[key] |= {str(i.get(field_) or i.get("name") or n)
                             for n, i in enumerate(items) if isinstance(i, dict)}
        sessions = d / "chat_sessions"
        if sessions.is_dir():
            chats |= {p.name for p in sessions.glob("*.json")}
        vault = d / "vault"
        if vault.is_dir():
            vault_bytes += sum(p.stat().st_size for p in vault.rglob("*") if p.is_file())
    runs = sum(len(list((d / "orchestration_logs").glob("*.log"))) for d in logs_dirs
               if (d / "orchestration_logs").is_dir())
    out = {k: len(v) for k, v in ids.items()}
    out.update(chats=len(chats), runs=runs, vault_bytes=vault_bytes)
    return out


def summary(counts: dict) -> list[str]:
    """["7 agents", "4 orchestrations", "312 chats"] — only what is there."""
    parts = []
    for key, one, many in (("agents", "agent", "agents"), ("orchestrations", "orchestration", "orchestrations"),
                           ("chats", "chat", "chats"), ("schedules", "schedule", "schedules"),
                           ("tools", "tool", "tools"), ("mcp_servers", "MCP server", "MCP servers"),
                           ("api_keys", "API key", "API keys"), ("runs", "run log", "run logs")):
        n = counts.get(key, 0)
        if n:
            parts.append(f"{n} {one if n == 1 else many}")
    if counts.get("vault_bytes"):
        parts.append(f"vault {_size(counts['vault_bytes'])}")
    return parts


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── the .env ─────────────────────────────────────────────────────────────────

def merged_env(env_files: list[Path]) -> dict:
    """The old .env values v2 should keep: its secrets and ports, not its paths.

    Keeping `SYNAPSE_JWT_SECRET` keeps everyone signed in; keeping the internal
    and access tokens keeps existing links working. The first file wins.
    """
    merged: dict = {}
    for path in env_files:
        for key, value in _read_env(path).items():
            if key not in _DROP_ENV and value and key not in merged:
                merged[key] = value
    return merged


# ── the old processes ────────────────────────────────────────────────────────

def _cmdline(pid: int) -> str:
    try:
        if sys.platform.startswith("linux"):
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        if IS_WIN:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=20)
            return out.stdout
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def is_synapse_process(pid: int) -> bool:
    """A PID file can outlive its process and the PID be reused: check the command."""
    cmd = _cmdline(pid)
    return any(marker in cmd for marker in ("main.py", "server.js", "next start", "next-server", "synapse"))


def stop_services(m: Migration, log=None) -> list[int]:
    """Stop what the old install is running. Returns the PIDs stopped."""
    _stop_service_managers(log)
    stopped: list[int] = []
    for pid_file in m.pid_files:
        try:
            pid = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            continue
        if pid <= 1 or not is_synapse_process(pid):
            continue
        if _terminate(pid):
            stopped.append(pid)
            if log:
                log.write(f"stopped old process {pid} ({pid_file})")
        try:
            pid_file.unlink()
        except OSError:
            pass
    return stopped


def _stop_service_managers(log=None) -> None:
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / "synapse-ai.service"
        if unit.exists() and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "stop", "synapse-ai.service"],
                           capture_output=True, timeout=60)
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.synapse-ai.server.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, timeout=60)


def _terminate(pid: int) -> bool:
    if IS_WIN:
        out = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        return out.returncode == 0
    try:
        target = os.getpgid(pid) if os.getpgid(pid) == pid else None
        (os.killpg if target else os.kill)(target or pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


# ── autostart and PATH lines v1.x left ───────────────────────────────────────

def had_autostart() -> bool:
    home = Path.home()
    if sys.platform.startswith("linux"):
        return (home / ".config" / "systemd" / "user" / "synapse-ai.service").exists()
    if sys.platform == "darwin":
        return (home / "Library" / "LaunchAgents" / "com.synapse-ai.server.plist").exists()
    if IS_WIN:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, "SynapseAI")
                return True
        except OSError:
            return False
    return False


def rc_files() -> list[Path]:
    home = Path.home()
    files = [home / ".bashrc", home / ".zshrc", home / ".bash_profile", home / ".profile"]
    if IS_WIN:
        docs = home / "Documents"
        files += [docs / "PowerShell" / "profile.ps1", docs / "WindowsPowerShell" / "profile.ps1",
                  docs / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
                  docs / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"]
    return [f for f in files if f.is_file()]


def remove_path_lines(bin_dirs: list[Path]) -> list[Path]:
    """Drop the `export PATH="<old>/bin:$PATH"` lines v1.x added. Returns files changed.

    They put the old `synapse` ahead of the new one on PATH. Only lines that
    name one of `bin_dirs` go, with the `# Synapse AI` comment just above them.
    """
    changed: list[Path] = []
    needles = [str(b) for b in bin_dirs]
    for path in rc_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        kept: list[str] = []
        for line in lines:
            if any(n in line for n in needles) and ("PATH" in line or "Path" in line):
                if kept and kept[-1].strip() == "# Synapse AI":
                    kept.pop()
                continue
            kept.append(line)
        if kept != lines:
            path.write_text("".join(kept), encoding="utf-8")
            changed.append(path)
    if IS_WIN:
        _remove_from_user_path(needles)
    return changed


def _remove_from_user_path(needles: list[str]) -> None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
            parts = [p for p in value.split(";") if p and not any(p.rstrip("\\") == n.rstrip("\\") for n in needles)]
            if ";".join(parts) != value:
                winreg.SetValueEx(key, "Path", 0, kind, ";".join(parts))
    except OSError:
        pass
