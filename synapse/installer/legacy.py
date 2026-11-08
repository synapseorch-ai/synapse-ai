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

#: What a v1.x data folder holds that the product made, not the person, by id: Synapse AI
#: (the default agent), the builder's agents in any release (`agent_native_builder` and
#: `agent_native_builder_*`), the builder's workflow, and the builder's runs (`builder_*`).
#: 2.0's import replaces them with its own (core.store.importer._seeded_ids and
#: core.native_builder.seed.is_builder_agent), so the confirm screen counts only the
#: person's work (F1446: "6 agents" were all 1.9's own). The installer can't import core,
#: being standard library only; test_installer_parts checks these agree with it.
BUILT_INS = {
    "agents": {"ids": ("agent_synapse_ai",), "prefix": "agent_native_builder"},
    "orchestrations": {"ids": ("orch_native_builder",)},
    "runs": {"prefix": "builder"},
}


def is_built_in(kind: str, item_id: str) -> bool:
    """Whether a v1.x agent, orchestration or run (by its id) is the product's own (`BUILT_INS`).
    A prefix covers the name itself and the name followed by `_`, never a longer word."""
    rule = BUILT_INS.get(kind, {})
    prefix = rule.get("prefix")
    return item_id in rule.get("ids", ()) or bool(prefix) and (item_id == prefix or item_id.startswith(prefix + "_"))

#: What a v1.x process leaves in its environment that must not reach 2.0: the
#: flag its upgrade re-exec sets, its data folder, and its venv. The installer
#: clears these before it runs anything (flow.py, handoff.py).
LEGACY_ENV = ("SYNAPSE_UPGRADE_SKIP_DOWNLOAD", "SYNAPSE_DATA_DIR",
              "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")

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
    stopped_words: list[str] = field(default_factory=list)  # what stop_services found with no PID file, said

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
    if not (root / "backend").is_dir():
        return None
    version = _version_of(root)
    if (root / ".git").exists() and not (version.startswith("1.") and (root / ".installed").exists()):
        # A checkout is somebody's working tree, not an install. But v1.x's setup
        # script cloned into this folder whenever the release download failed, and
        # a finished one wrote .installed: Naveen's Windows 1.7.4 was missed, so 2.0
        # neither moved its data nor took its place on PATH (F1057).
        return None
    # The marker a finished v1.x install wrote, a v1.x tree without one (an
    # install that stopped half way), or data in its folder whatever its code
    # says — an old setup.sh unpacks the newest source over an unmarked
    # install before running setup.py. A 2.x tree with none of those is only
    # that carrier, not an install.
    has_old_data = has_data(root / "backend" / "data")
    if not ((root / ".installed").exists() or version.startswith("1.") or has_old_data):
        return None
    if not version.startswith("1."):
        version = "1.x"
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
    """What the confirm screen names: agents, orchestrations, chats, … — the person's, not the product's own."""
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
                ids[key] = {i for i in ids[key] if not is_built_in(key, i)}
        sessions = d / "chat_sessions"
        if sessions.is_dir():
            chats |= {p.name for p in sessions.glob("*.json")}
        vault = d / "vault"
        if vault.is_dir():
            vault_bytes += sum(p.stat().st_size for p in vault.rglob("*") if p.is_file())
    runs = sum(1 for d in logs_dirs if (d / "orchestration_logs").is_dir()
               for log in (d / "orchestration_logs").glob("*.log") if not is_built_in("runs", log.stem))
    out = {k: len(v) for k, v in ids.items()}
    out.update(chats=len(chats), runs=runs, vault_bytes=vault_bytes)
    return out


def mentions(data_dirs: list[Path], roots: list[str]) -> list[str]:
    """The agents, workflows, tools and MCP servers whose definition names a path in `roots`, the
    product's own left out (2.0 replaces them).

    A repo, a working folder or a stdio server's command inside the old
    install's folder keeps its path through the import, and stops working when
    that folder is removed. Returned as "kind name" for the log and a count.
    """
    if not roots:
        return []
    # As written inside JSON too: a Windows path's backslashes are escaped there.
    needles = set(roots) | {json.dumps(r)[1:-1] for r in roots}
    found: list[str] = []
    for d in data_dirs:
        for kind, name, key in (("agent", "user_agents", "agents"), ("workflow", "orchestrations", "orchestrations"),
                                ("tool", "custom_tools", ""), ("MCP server", "mcp_servers", ""),
                                ("repo", "repos", "")):
            items = _json(d / f"{name}.json")
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or is_built_in(key, str(item.get("id") or "")):
                    continue
                if any(n in json.dumps(item) for n in needles):
                    found.append(f"{kind} {item.get('name') or item.get('id') or '?'}")
    return found


def summary(counts: dict) -> list[str]:
    """["7 agents", "4 workflows", "312 chats"] — only what is there, in 2.0's words."""
    parts = []
    for key, one, many in (("agents", "agent", "agents"), ("orchestrations", "workflow", "workflows"),
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


@dataclass
class Proc:
    """One running process, as much of it as this platform says."""
    pid: int
    ppid: int
    argv: list[str]                 # one unsplit command line where the platform gives no argv
    exe: str = ""
    cwd: str = ""
    uid: int | None = None
    ports: tuple = ()               # the TCP ports it listens on (Linux)


#: Interpreters whose first script argument is the program they run.
_INTERPRETER = re.compile(r"^(python(\d+(\.\d+)*)?|node|nodejs)(\.exe)?$", re.I)
#: Options of those interpreters that take the next argument as their value.
_OPTION_WITH_VALUE = {"-X", "-W", "-r", "--require", "--import", "--loader", "--experimental-loader"}


def code_dirs(m: Migration) -> list[Path]:
    """Where each old install's code runs from: a script install's folder, a pip install's packages (never all of
    site-packages), an npm package's folder."""
    dirs: list[Path] = []
    for install in m.installs:
        dirs += [install.root / "synapse", install.root / "backend"] if install.kind == "pip" else [install.root]
    return _unique(dirs)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: str, dirs: list[str]) -> bool:
    p = _norm(path)
    return any(p == d or p.startswith(d.rstrip(os.sep) + os.sep) for d in dirs)


def _script_arg(argv: list[str]) -> str | None:
    """The program an interpreter runs: its first argument that is not an option. None for -m, -c and -e, which
    run a module or text, not a file."""
    skip = False
    for arg in argv[1:]:
        if skip:
            skip = False
        elif arg in ("-m", "-c", "-e", "--eval", "-p", "--print"):
            return None
        elif arg in _OPTION_WITH_VALUE:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def _runs_old_code(p: Proc, dirs: list[str]) -> bool:
    """Its program is the old install's: the executable or the path it was started by is inside a code folder, or
    it is an interpreter whose script is. Where it merely sits (its working folder) or a file it names is never
    enough: a person's shell or editor in the old folder is theirs (F1350)."""
    if p.exe and _inside(p.exe, dirs):
        return True
    if len(p.argv) == 1 and " " in p.argv[0] and not os.path.exists(p.argv[0]):
        # An unsplit command line (Windows, macOS, a retitled process): an interpreter naming the folder, or its
        # relative script under its working folder.
        cmd = p.argv[0]
        if not _INTERPRETER.match(os.path.basename(p.exe or cmd.split(" ")[0].strip('"'))):
            return False
        if any(d in os.path.normcase(cmd) for d in dirs):
            return True
        script = _script_arg(cmd.split())
        return bool(script and p.cwd and not os.path.isabs(script)) and _inside(_resolve(script, p.cwd), dirs)
    if not p.argv:
        return False
    first = p.argv[0]
    if (os.sep in first or (os.altsep and os.altsep in first)) and _inside(_resolve(first, p.cwd), dirs):
        return True     # started by its path: a venv's python, the old bin/synapse
    if _INTERPRETER.match(os.path.basename(first)) or (p.exe and _INTERPRETER.match(os.path.basename(p.exe))):
        script = _script_arg(p.argv)
        return bool(script) and _inside(_resolve(script, p.cwd), dirs)
    return False


def _resolve(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) or not cwd else os.path.join(cwd, path)


def _ancestors(pid: int, procs: dict[int, Proc]) -> set[int]:
    seen: set[int] = set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        pid = procs[pid].ppid
    return seen


def old_install_processes(m: Migration, procs: list[Proc] | None = None) -> list[Proc]:
    """What still runs the old install's code, with what it started: the web server its frontend's node started
    names no path of its own (next-server rewrites its title). Never this process or one it runs under (the hand-off
    runs from inside the old folder), never another user's, never PID 1. Listeners first: they are what holds the
    old addresses."""
    dirs = [_norm(str(d)) for d in code_dirs(m)] + [_norm(os.path.realpath(d)) for d in code_dirs(m)]
    if not dirs:
        return []
    procs = _processes() if procs is None else procs
    by_pid = {p.pid: p for p in procs}
    mine = _ancestors(os.getpid(), by_pid) | {os.getpid()}
    uid = os.getuid() if hasattr(os, "getuid") else None

    def eligible(p: Proc) -> bool:
        return p.pid > 1 and p.pid not in mine and (uid is None or p.uid is None or p.uid == uid)

    found = {p.pid for p in procs if eligible(p) and _runs_old_code(p, dirs)}
    grew = True
    while grew:
        children = {p.pid for p in procs if p.ppid in found and p.pid not in found and eligible(p)}
        found |= children
        grew = bool(children)
    return sorted((by_pid[pid] for pid in found), key=lambda p: (not p.ports, p.pid))


def _processes() -> list[Proc]:
    try:
        if sys.platform.startswith("linux"):
            return _linux_processes()
        if IS_WIN:
            return _windows_processes()
        return _ps_processes()
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _linux_processes() -> list[Proc]:
    listening = _linux_listening_inodes()
    procs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            argv = [a.decode("utf-8", "replace") for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            uid = entry.stat().st_uid
        except (OSError, ValueError, IndexError):
            continue
        exe, cwd = _readlink(entry / "exe"), _readlink(entry / "cwd")    # "" where it is not ours to read
        ports: set[int] = set()
        try:
            for fd in (entry / "fd").iterdir():
                link = _readlink(fd)
                if link.startswith("socket:[") and link[8:-1] in listening:
                    ports.add(listening[link[8:-1]])
        except OSError:
            pass
        procs.append(Proc(int(entry.name), ppid, argv, exe, cwd, uid, tuple(sorted(ports))))
    return procs


def _readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _linux_listening_inodes() -> dict[str, int]:
    out: dict[str, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == "0A":     # LISTEN
                out[f[9]] = int(f[1].rsplit(":", 1)[1], 16)
    return out


def _ps_processes() -> list[Proc]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,uid=,command="], capture_output=True, text=True, timeout=20)
    exes = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=20).stdout
    exe_of = {int(a): b.strip() for a, b in (ln.strip().split(None, 1) for ln in exes.splitlines() if ln.strip())
              if a.isdigit()}
    cwd_of: dict[int, str] = {}
    try:    # each process's working folder, for a relative script (the 1.x frontend's scripts/load-env.js)
        lsof = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fpn", "-u", str(os.getuid())],
                              capture_output=True, text=True, timeout=30).stdout
        pid = 0
        for ln in lsof.splitlines():
            if ln[:1] == "p" and ln[1:].isdigit():
                pid = int(ln[1:])
            elif ln[:1] == "n" and pid:
                cwd_of[pid] = ln[1:]
    except (OSError, subprocess.SubprocessError):
        pass
    procs = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            pid = int(parts[0])
            procs.append(Proc(pid, int(parts[1]), [parts[3]], exe_of.get(pid, ""), cwd_of.get(pid, ""),
                              int(parts[2])))
    return procs


def _windows_processes() -> list[Proc]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine"
         " | ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=60)
    rows = json.loads(out.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    return [Proc(int(r.get("ProcessId") or 0), int(r.get("ParentProcessId") or 0), [r.get("CommandLine") or ""],
                 r.get("ExecutablePath") or "") for r in rows if r.get("ProcessId")]


def describe(p: Proc) -> str:
    """The stop line's words for one: what it was, by its short command, and its PID."""
    argv = p.argv if len(p.argv) > 1 else (p.argv[0].split() if p.argv else [])
    name = os.path.basename(argv[0]).strip('"') if argv else os.path.basename(p.exe)
    script = _script_arg(argv) if argv and _INTERPRETER.match(name) else None
    rest = argv[argv.index(script) + 1:argv.index(script) + 2] if script in argv else []
    short = " ".join([name, "…", os.path.basename(script), *rest] if script else argv[:2] or [name])
    text = f"{name} {os.path.basename(script) if script else ' '.join(argv[1:2])}".lower()   # never a folder's name
    what = ("the old web page" if any(k in text for k in ("next", "load-env", "server.js", "frontend"))
            else "the old backend" if any(k in text for k in ("main.py", "uvicorn", "core.server"))
            else "an old process")
    return f"{what} ({short}, pid {p.pid})"


def stop_services(m: Migration, log=None) -> list[int]:
    """Stop what the old install is running. Returns the PIDs stopped.

    First what its PID files name; then whatever still runs the old install's code with no PID file. A 1.x
    `synapse upgrade` stops its own services before it hands over and removes the PID files, but its frontend's
    web server outlives that stop and keeps the old address, serving the old pages from a folder the hand-off then
    removes (F1350). Each of those is said (`m.stopped_words`)."""
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
    for p in old_install_processes(m):
        if p.pid in stopped or not _terminate(p.pid):
            continue
        stopped.append(p.pid)
        m.stopped_words.append(describe(p))
        if log:
            log.write(f"stopped old process {p.pid} ({' '.join(p.argv)[:200]}), still running the old install's code"
                      + (f", listening on {', '.join(map(str, p.ports))}" if p.ports else ""))
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
        # Its whole group when it leads one, unless that group is this process's own.
        target = os.getpgid(pid) if os.getpgid(pid) == pid and pid != os.getpgrp() else None
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
