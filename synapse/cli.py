"""
Synapse CLI - starts the backend and frontend, then opens the browser.
"""
import os
import sys
import shutil
import signal
import threading
import time
import urllib.request
import urllib.error
import subprocess
import webbrowser
import argparse
from pathlib import Path

IS_WIN = sys.platform == "win32"


def _rmtree(path):
    """Remove a directory tree, handling read-only/locked files on all platforms."""
    def _onerror(func, p, exc_info):
        try:
            # Make writable: dir needs 0o755, file needs at least u+w
            os.chmod(p, 0o755 if os.path.isdir(p) else 0o644)
            func(p)
        except Exception:
            try:
                # If the file itself is fine but the parent dir is not writable, fix that
                os.chmod(os.path.dirname(p), 0o755)
                func(p)
            except Exception:
                pass
    shutil.rmtree(path, onerror=_onerror)


PACKAGE_DIR = Path(__file__).resolve().parent
# When installed as a package, backend is one level up from synapse/
BACKEND_DIR = PACKAGE_DIR.parent / "backend"
FRONTEND_DIR = PACKAGE_DIR.parent / "frontend"
ROOT_DIR = PACKAGE_DIR.parent

# When installed via pip, the Next.js standalone build is bundled here.
_BUNDLED_FRONTEND = PACKAGE_DIR / "_frontend"


def _is_packaged() -> bool:
    """Running from a directory a package manager owns (pip, uv, npm's venv).

    Such a directory is replaced on every upgrade, so nothing this install
    writes may live under it — see `synapse_home()`. A checkout is never
    packaged, even one installed editable into a venv.
    """
    if (ROOT_DIR / ".git").exists():
        return False
    return any(part in ("site-packages", "dist-packages") for part in ROOT_DIR.parts)


# A packaged install keeps everything it writes under SYNAPSE_HOME (~/.synapse):
# the database, the .env holding its generated secrets, logs and PID files. Set
# here, before anything reads a path, so the backend and every process it starts
# inherit the same answer (backend/core/paths.py reads it too). A checkout sets
# nothing and keeps backend/var and the root .env.
if _is_packaged() and not os.environ.get("SYNAPSE_HOME", "").strip():
    os.environ["SYNAPSE_HOME"] = str(Path.home() / ".synapse")


def synapse_home() -> "Path | None":
    """This install's home, or None for a checkout. Read at call time."""
    home = os.environ.get("SYNAPSE_HOME", "").strip()
    return Path(home).expanduser() if home else None


def _env_file() -> Path:
    """The .env this install's generated secrets are kept in."""
    home = synapse_home()
    return home / ".env" if home else ROOT_DIR / ".env"


def _var_dir() -> Path:
    """Where the database, blobs, run and log files live when nothing overrides it."""
    home = synapse_home()
    return home / "var" if home else BACKEND_DIR / "var"


def _run_dir() -> Path:
    """The two PID files `synapse stop` and `synapse status` read.

    Per install: a checkout's are under its own backend/var, so `synapse stop`
    in a checkout can never stop a packaged install on the same machine.
    """
    home = synapse_home()
    return home / "run" if home else BACKEND_DIR / "var" / "run"


def _log_dir() -> Path:
    """Where the backend and frontend write their output (`synapse logs`)."""
    home = synapse_home()
    return home / "logs" if home else BACKEND_DIR / "var" / "logs"


def _runtime_dir() -> "Path | None":
    """A private Node.js / uv the installer fetched, when the system had none."""
    home = synapse_home()
    return home / "runtime" if home else None


def _ensure_runtime_on_path() -> None:
    """Put the installer's private Node.js and uv first on PATH.

    Done once, here, so the CLI, the backend, the frontend server and every
    MCP server they start (`npx`, `uvx`) all find the same ones.
    """
    runtime = _runtime_dir()
    if runtime is None:
        return
    dirs = []
    node = runtime / "node"
    if node.is_dir():
        dirs.append(node if IS_WIN else node / "bin")
    uv = runtime / "uv"
    if uv.is_dir():
        dirs.append(uv)
    current = os.environ.get("PATH", "")
    for d in reversed(dirs):
        if str(d) not in current.split(os.pathsep):
            current = str(d) + os.pathsep + current
    os.environ["PATH"] = current


_ensure_runtime_on_path()

# ---------------------------------------------------------------------------
# Load this install's .env BEFORE reading port defaults so that values set by
# `synapse setup` (or a hand-edited .env) are honoured without the user having
# to export them manually in every shell session.
# ---------------------------------------------------------------------------
_ENV_FILE = _env_file()

def _load_dotenv(path: Path):
    """Minimal .env loader -- only sets vars that are NOT already in the environment."""
    if not path.exists():
        return
    try:
        with open(path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip()
                # Don't override variables already set in the real environment
                if _key and _key not in os.environ:
                    os.environ[_key] = _val
    except Exception:
        pass  # non-fatal -- env vars can still be set manually

_load_dotenv(_ENV_FILE)

DEFAULT_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
DEFAULT_FRONTEND_PORT = int(os.getenv("SYNAPSE_FRONTEND_PORT", "3000"))

# Runtime ports (may be overridden by CLI args -- module-level aliases kept for
# backwards compatibility; actual values are resolved in _start_command)
BACKEND_PORT = DEFAULT_BACKEND_PORT
FRONTEND_PORT = DEFAULT_FRONTEND_PORT


def _pid_file(name: str) -> Path:
    return _run_dir() / f"{name}.pid"


def _find_node_exe_win():
    """Windows: find node.exe by probing known install locations (bypasses stale PATH cache).
    Returns (node_exe_path, bin_dir) or (None, None)."""
    import os as _os
    pf   = _os.environ.get("ProgramFiles",       r"C:\Program Files")
    pf86 = _os.environ.get("ProgramFiles(x86)",  r"C:\Program Files (x86)")
    lad  = _os.environ.get("LocalAppData",        "")
    appd = _os.environ.get("APPDATA",             "")

    candidates = []
    # Standard install dirs
    for d in [
        _os.path.join(pf,   "nodejs"),
        _os.path.join(pf86, "nodejs"),
        _os.path.join(lad,  "Programs", "nodejs"),
        _os.path.join(lad,  "nodejs"),
    ]:
        exe = _os.path.join(d, "node.exe")
        if _os.path.isfile(exe):
            candidates.append((exe, d))
    # nvm-windows
    nvm_root = _os.path.join(appd, "nvm")
    if _os.path.isdir(nvm_root):
        for entry in sorted(_os.listdir(nvm_root), reverse=True):
            exe = _os.path.join(nvm_root, entry, "node.exe")
            if _os.path.isfile(exe):
                candidates.append((exe, _os.path.join(nvm_root, entry)))
    # PATH entries
    for entry in _os.environ.get("PATH", "").split(_os.pathsep):
        exe = _os.path.join(entry.strip(), "node.exe")
        if _os.path.isfile(exe):
            candidates.append((exe, entry.strip()))

    MIN = (20, 9, 0)
    for node_exe, bin_dir in candidates:
        try:
            r = subprocess.run([node_exe, "--version"], capture_output=True, text=True, timeout=5)
            ver_str = r.stdout.strip().lstrip("v")
            ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
            if ver_tuple >= MIN:
                return node_exe, bin_dir
        except Exception:
            pass
    return None, None


def _ensure_node_in_path_win():
    """Windows: make sure the Node.js bin dir is in PATH for this process.
    Returns True if a suitable node was found and PATH was updated."""
    node_exe, bin_dir = _find_node_exe_win()
    if node_exe:
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


def _npm_command():
    """Return the correct npm executable for the current OS.
    On Windows, 'npm' is a .cmd file and must be invoked explicitly or via shell."""
    if IS_WIN:
        # npm.cmd is the real entry-point on Windows; avoids needing shell=True
        npm_cmd = shutil.which("npm.cmd") or shutil.which("npm")
        if npm_cmd:
            return npm_cmd
        # Fallback: look next to node.exe
        node_exe, bin_dir = _find_node_exe_win()
        if bin_dir:
            npm_candidate = os.path.join(bin_dir, "npm.cmd")
            if os.path.isfile(npm_candidate):
                return npm_candidate
        return "npm"
    return "npm"


def check_prerequisites():
    errors = []
    if IS_WIN:
        # On Windows, PATH may be stale after a fresh install -- probe directly
        if not _ensure_node_in_path_win():
            errors.append("Node.js 20.9.0+ not found -- install from https://nodejs.org/ and re-run.")
    else:
        node = shutil.which("node")
        if node is None:
            errors.append("node not found -- install Node.js 20.9.0+ from https://nodejs.org/")
        else:
            try:
                r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
                ver_str = r.stdout.strip().lstrip("v")
                ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
                min_str = ".".join(str(x) for x in MIN_NODE)
                if ver_tuple < MIN_NODE:
                    errors.append(
                        f"Node.js {ver_str} is too old (need {min_str}+) -- "
                        "upgrade from https://nodejs.org/"
                    )
            except Exception:
                pass  # version check failed, proceed and let Node report its own errors
        # Only a checkout runs `npm start`; a packaged install runs the bundled
        # server with node alone.
        if not _BUNDLED_FRONTEND.exists() and shutil.which("npm") is None:
            errors.append(f"npm not found -- install Node.js {'.'.join(str(x) for x in MIN_NODE)}+ from https://nodejs.org/")
    if errors:
        for e in errors:
            print(f"Error: {e}")
        sys.exit(1)


def ensure_run_dir():
    """Make sure there is somewhere to put the PID files.

    This used to be `ensure_data_dir()`: it created a vault, a datasets folder,
    two log folders and five empty JSON arrays, seeding by hand the shape the
    engine expected to find. The engine creates its own schema on first use
    now, so what is left is a directory for two PID files.
    """
    _run_dir().mkdir(parents=True, exist_ok=True)


def _ensure_playwright_browsers():
    import json
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        browsers_path = Path(base) / "ms-playwright"
    elif sys.platform == "darwin":
        browsers_path = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        browsers_path = Path.home() / ".cache" / "ms-playwright"

    # Check independently: chromium-* is for Python playwright; mcp-chrome*/mcp-chromium
    # is what `@playwright/mcp install-browser` creates and what the MCP server uses.
    # A fresh install via setup.py only creates chromium-*, so the MCP browser can be
    # missing even when the Python playwright browser is present.
    has_chromium = False
    has_mcp_browser = False
    if browsers_path.exists():
        try:
            for d in browsers_path.iterdir():
                if not d.is_dir():
                    continue
                if d.name.startswith("chromium-"):
                    has_chromium = True
                if d.name.startswith("mcp-chrome") or d.name.startswith("mcp-chromium"):
                    has_mcp_browser = True
        except Exception:
            pass

    if has_chromium and has_mcp_browser:
        return

    npx_cmd = shutil.which("npx.cmd") if IS_WIN else "npx"
    if not npx_cmd:
        npx_cmd = "npx"
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_path)

    if not has_chromium:
        print("Installing Playwright browsers...", end="", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True)
            print(" done.")
        except Exception as e:
            print(f"\n  Warning: Failed to install Playwright browsers: {e}")

    if not has_mcp_browser:
        print("Installing browser for Browser Automation MCP server...", end="", flush=True)
        try:
            subprocess.run([npx_cmd, "-y", "@playwright/mcp", "install-browser", "chrome-for-testing"], env=env, check=True, capture_output=True)
            print(" done.")
        except Exception as e:
            print(f"\n  Warning: Failed to install MCP browser: {e}")
            return

    try:
        from synapse import _store
        _store.save_settings({"playwright_browsers_path": str(browsers_path)})
    except Exception as e:
        print(f"\n  Warning: Failed to save playwright_browsers_path to settings: {e}")


#: A service log is rotated at start once it passes this size; three old ones kept.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_KEEP = 3


def _log_path(name: str) -> Path:
    return _log_dir() / f"{name}.log"


def _rotate_log(path: Path) -> None:
    """`backend.log` → `backend.log.1` → … once it is over the size limit.

    Best effort: on Windows a log still held open by a running process cannot
    be renamed, and a start is not the place to fail over that.
    """
    try:
        if not path.exists() or path.stat().st_size <= _LOG_MAX_BYTES:
            return
        for i in range(_LOG_KEEP - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{i + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def _spawn_kwargs(detach: bool, verbose: bool, log_name: str) -> dict:
    """How a service is started: its own session when detached, its output in a log.

    With `verbose` the output goes to this terminal, as it always used to.
    Otherwise it goes to `<logs>/<name>.log` — the terminal is the user's, and a
    running server's request log is not something they asked to watch.
    """
    kwargs: dict = {}
    if detach:
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            if not verbose:
                flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            kwargs["creationflags"] = flags
    if not verbose:
        path = _log_path(log_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log(path)
        log = open(path, "ab", buffering=0)
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} synapse start =====\n".encode())
        kwargs.update(stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    return kwargs


def _popen(cmd: list, cwd: Path, env: dict, kwargs: dict) -> subprocess.Popen:
    """Popen, then drop this process's copy of the log handle the child now owns."""
    try:
        return subprocess.Popen(cmd, cwd=str(cwd), env=env, **kwargs)
    finally:
        log = kwargs.get("stdout")
        if log is not None and hasattr(log, "close"):
            log.close()


def start_backend(detach: bool = False, port: int | None = None, profile: bool = False,
                  verbose: bool = False):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    # A log file is block-buffered otherwise, and a crash would lose the lines
    # that explain it.
    env["PYTHONUNBUFFERED"] = "1"
    if port is not None:
        env["SYNAPSE_BACKEND_PORT"] = str(port)
    if profile:
        env["SYNAPSE_PROFILING"] = "true"
    return _popen(
        [sys.executable, str(BACKEND_DIR / "main.py")],
        BACKEND_DIR, env, _spawn_kwargs(detach, verbose, "backend"),
    )


def _sync_bundled_frontend(verbose: bool = True) -> bool:
    """Copy the latest standalone build from frontend/.next/standalone into synapse/_frontend/.

    Returns True if a sync was performed, False if skipped (no source or no mismatch).
    """
    standalone_src = FRONTEND_DIR / ".next" / "standalone"
    if not standalone_src.exists():
        if verbose:
            print(f"  Warning: standalone build not found at {standalone_src}")
            print("  synapse/_frontend/ was NOT updated. Try running scripts/build_frontend.sh manually.")
        return False
    _rmtree(_BUNDLED_FRONTEND)
    _BUNDLED_FRONTEND.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(standalone_src), str(_BUNDLED_FRONTEND), dirs_exist_ok=True)
    static_src = FRONTEND_DIR / ".next" / "static"
    static_dst = _BUNDLED_FRONTEND / ".next" / "static"
    if static_src.exists():
        static_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(static_src), str(static_dst), dirs_exist_ok=True)
    public_src = FRONTEND_DIR / "public"
    if public_src.exists():
        shutil.copytree(str(public_src), str(_BUNDLED_FRONTEND / "public"), dirs_exist_ok=True)
    return True


def start_frontend(detach: bool = False, port: int | None = None, backend_port: int | None = None,
                   verbose: bool = False):
    _backend_port = backend_port if backend_port is not None else DEFAULT_BACKEND_PORT
    _frontend_port = port if port is not None else DEFAULT_FRONTEND_PORT
    env = os.environ.copy()
    env["BACKEND_URL"] = f"http://127.0.0.1:{_backend_port}"
    env["NEXT_PUBLIC_BACKEND_PORT"] = str(_backend_port)
    env["SYNAPSE_FRONTEND_PORT"] = str(_frontend_port)
    env["SYNAPSE_BACKEND_PORT"] = str(_backend_port)

    # Pip-installed package: frontend is pre-built standalone at synapse/_frontend/
    if _BUNDLED_FRONTEND.exists():
        # Auto-sync if the source has a newer build (e.g. after synapse upgrade pulled new code).
        standalone_src = FRONTEND_DIR / ".next" / "standalone"
        if standalone_src.exists():
            src_id_file = standalone_src / ".next" / "BUILD_ID"
            bundled_id_file = _BUNDLED_FRONTEND / ".next" / "BUILD_ID"
            src_id = src_id_file.read_text(encoding="utf-8").strip() if src_id_file.exists() else None
            bundled_id = bundled_id_file.read_text(encoding="utf-8").strip() if bundled_id_file.exists() else None
            if src_id and src_id != bundled_id:
                print("  Syncing updated frontend build into synapse/_frontend/...")
                _sync_bundled_frontend(verbose=False)
                print("  Frontend sync complete.")

        server_js = _BUNDLED_FRONTEND / "server.js"
        if not server_js.exists():
            print(f"Error: bundled frontend server not found at {server_js}")
            print("Try reinstalling: pip install --upgrade synapse-orch-ai")
            sys.exit(1)
        node = shutil.which("node")
        if not node:
            print("Error: node not found -- install Node.js 20.9.0+ from https://nodejs.org/")
            sys.exit(1)
        env["PORT"] = str(_frontend_port)
        env["HOSTNAME"] = "0.0.0.0"
        env["NODE_ENV"] = "production"
        return _popen([node, str(server_js)], _BUNDLED_FRONTEND, env,
                      _spawn_kwargs(detach, verbose, "frontend"))

    # Dev/source mode: use npm start on the frontend source directory
    next_dir = FRONTEND_DIR / ".next"
    if not next_dir.exists():
        print("Error: frontend is not built. Run the following first:")
        print(f"  cd {FRONTEND_DIR} && npm install && npm run build")
        sys.exit(1)
    npm = _npm_command()
    return _popen([npm, "start"], FRONTEND_DIR, env, _spawn_kwargs(detach, verbose, "frontend"))


def wait_for_url(url: str, name: str, timeout: int = 300) -> bool:
    start = time.time()
    port = url.split(":")[-1].split("/")[0]
    while True:
        elapsed = int(time.time() - start)
        if elapsed >= timeout:
            print()
            print(f"  Timeout waiting for {name} at {url}")
            print(f"  Check that nothing else is using port {port},")
            print(f"  or try 'synapse stop' then 'synapse start'.")
            return False
        try:
            urllib.request.urlopen(url, timeout=3)
            print(f"\r  {name} ready.                    ")
            return True
        except Exception:
            print(f"\r  Waiting for {name}... {elapsed}s", end="", flush=True)
            time.sleep(2)


class _StartFailed(Exception):
    """A service did not come up; the message says which and why."""

    def __init__(self, message: str, log_name: str):
        super().__init__(message)
        self.log_name = log_name


def _wait_ready(url: str, proc: subprocess.Popen, name: str, log_name: str,
                timeout: int = 300) -> None:
    """Poll `url` until it answers. Raises `_StartFailed` on exit or timeout.

    Watches the process too: a backend that crashed on import used to be
    waited on for the full five minutes, printing a counter the whole time.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            raise _StartFailed(f"the {name} stopped (exit code {code})", log_name)
        try:
            urllib.request.urlopen(url, timeout=3)
            return
        except Exception:
            time.sleep(0.5)
    port = url.split(":")[-1].split("/")[0]
    raise _StartFailed(
        f"the {name} did not answer on port {port} within {timeout}s", log_name
    )


def _tail(path: Path, lines: int = 20) -> list[str]:
    """The last `lines` lines of a log, decoded forgivingly; [] if unreadable."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


class _Spinner:
    """One line that says what is happening, erased when it is done.

    Only on a terminal: piped or captured output gets nothing, so a script
    reading `synapse start --detach` sees just the result.
    """

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str):
        self.text = text
        self._stop = threading.Event()
        self._thread = None
        try:
            self._live = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
            "⠋".encode(sys.stdout.encoding or "ascii")
        except Exception:
            self._live = False

    def __enter__(self):
        if self._live:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        i = 0
        while not self._stop.wait(0.08):
            sys.stdout.write(f"\r  {self._FRAMES[i % len(self._FRAMES)]} {self.text}")
            sys.stdout.flush()
            i += 1

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        return False


def open_browser(url: str):
    time.sleep(1)
    webbrowser.open(url)


def _write_pidfile(path: Path, pid: int):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid), encoding="utf-8")
    except Exception as e:
        print(f"Warning: could not write pidfile {path}: {e}")


def _read_pidfile(path: Path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


def _kill_proc_tree(proc: subprocess.Popen, timeout: int = 5) -> None:
    """Kill a process AND all its descendants.

    On Windows, terminate() only kills the outermost batch wrapper (.cmd);
    child node.exe processes become orphans.  taskkill /F /T kills the whole
    process tree including every grandchild.

    On Unix, send SIGTERM to the process group so npm -> node children all die.
    """
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        # Try to kill the entire process group (handles npm -> node chains)
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        # Wait then force-kill if still alive
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _terminate_pid(pid: int, name: str, timeout: int = 5) -> bool:
    """Terminate a process by PID, with fallback to SIGKILL."""
    if IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
            return True
        except Exception as e:
            print(f"  Could not kill {name} ({pid}): {e}")
            return False
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception as e:
            print(f"  Could not signal {name} ({pid}): {e}")
            return False
        start = time.time()
        while time.time() - start < timeout:
            if not _is_running(pid):
                return True
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
        return not _is_running(pid)


def _ensure_coding_deps() -> None:
    """
    Ensure cocoindex and psycopg are installed and up-to-date in the backend venv.

    Called at every `synapse start` so the deps are self-healing:
    - Fresh installs that skipped the coding-agent step get them auto-installed.
    - Old installs with an outdated cocoindex (missing .typing) get upgraded.
    - Installs where the user toggled Code Indexing ON after initial setup work
      without needing a manual 'synapse upgrade'.
    """
    venv_python = BACKEND_DIR / "venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    if not venv_python.exists():
        return  # No venv at all — not our problem here; backend will report it.

    coding_req = BACKEND_DIR / "requirements-coding.txt"
    if not coding_req.exists():
        return  # File not shipped (shouldn't happen after package.json fix).

    # Quick check: can the venv import cocoindex.typing?
    # This catches both "not installed" and "old version" cases.
    check = subprocess.run(
        [str(venv_python), "-c", "from cocoindex.typing import VectorInfo"],
        capture_output=True,
    )
    if check.returncode == 0:
        return  # All good — nothing to do.

    print("  Coding-agent dependencies missing or outdated - installing now...")
    # Constrain to the lockfile when present. This runs on every `synapse start`
    # with --upgrade, so without a constraint it is a standing drift vector:
    # it would happily pull a new major of a shared transitive dependency into
    # an otherwise locked environment.
    pip_args = [str(venv_python), "-m", "pip", "install", "-q", "--upgrade", "-r", str(coding_req)]
    lockfile = BACKEND_DIR / "requirements.lock"
    if lockfile.exists():
        pip_args += ["-c", str(lockfile)]

    result = subprocess.run(pip_args, capture_output=True, text=True)
    if result.returncode == 0:
        print("  Coding-agent dependencies installed.")
    else:
        print(f"  Warning: could not install coding-agent dependencies:")
        print(f"  {result.stderr.strip()[:300]}")
        print(f"  Run manually: {venv_python} -m pip install -r {coding_req}")


def _ensure_internal_token():
    """Ensure SYNAPSE_INTERNAL_TOKEN exists in .env. Generate if missing.

    This token secures the backend's internal /api/* routes so only the
    frontend can access them. External API access uses separate API keys.
    """
    env_file = _env_file()
    token_var = "SYNAPSE_INTERNAL_TOKEN"

    # Check if already in environment (e.g. from .env loaded earlier)
    if os.environ.get(token_var):
        return

    # Check if present in .env file
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith(f"{token_var}=") and len(line) > len(f"{token_var}="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        os.environ[token_var] = val
                        return
        except Exception:
            pass

    # Generate a new token
    import secrets as _secrets
    token = _secrets.token_hex(32)
    os.environ[token_var] = token

    # Append to .env file
    try:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"\n# Internal token for frontend<->backend security (auto-generated)\n")
            f.write(f"{token_var}={token}\n")
    except Exception as e:
        print(f"  Warning: could not write {token_var} to .env: {e}")
        print(f"  The token is set in memory for this session.")


ACCESS_TOKEN_VAR = "SYNAPSE_ACCESS_TOKEN"


def _read_env_var(var: str) -> str:
    """A variable's value from the process, else from .env, else ""."""
    if os.environ.get(var):
        return os.environ[var]
    env_file = _env_file()
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{var}=") and line.split("=", 1)[1].strip():
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return ""


def _ensure_access_token(rotate: bool = False) -> str:
    """Ensure SYNAPSE_ACCESS_TOKEN exists in .env; generate (or replace) it.

    With login off, Synapse is opened with a link carrying this token rather
    than being open to anyone who can reach its port (D124). `rotate` replaces
    it: sessions made from the old link stop working once Synapse restarts.
    """
    import secrets as _secrets

    env_file = _env_file()
    current = "" if rotate else _read_env_var(ACCESS_TOKEN_VAR)
    if current:
        os.environ[ACCESS_TOKEN_VAR] = current
        return current

    token = _secrets.token_urlsafe(32)
    os.environ[ACCESS_TOKEN_VAR] = token
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        lines = [l for l in lines if not l.strip().startswith(f"{ACCESS_TOKEN_VAR}=")]
        if not any("access link" in l for l in lines):
            lines.append("")
            lines.append("# Access link token, for login-off installs (auto-generated)")
        lines.append(f"{ACCESS_TOKEN_VAR}={token}")
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"  Warning: could not write {ACCESS_TOKEN_VAR} to .env: {e}")
        print(f"  The token is set in memory for this session.")
    return token


def _login_is_on() -> bool:
    try:
        from synapse import _store
        _store.prepare()
        s = _store.load_settings()
        return bool(s.get("login_enabled") and s.get("login_username") and s.get("login_password_hash"))
    except Exception:
        return False


def _access_link(frontend_port: int) -> str:
    return f"http://localhost:{frontend_port}/?token={_read_env_var(ACCESS_TOKEN_VAR)}"


def _access_link_command(rotate: bool = False):
    """Print this install's access link; `--rotate` replaces it."""
    port = int(os.getenv("SYNAPSE_FRONTEND_PORT", str(DEFAULT_FRONTEND_PORT)))
    _ensure_access_token(rotate=rotate)
    if _login_is_on():
        print("  Login is on, so the access link does not open Synapse — sign in with your password.")
        print("  (It works again if you turn login off.)")
        return
    print(f"  {_access_link(port)}")
    print("  Anyone with this link can use this Synapse. Share it only with people who may.")
    if rotate:
        print("  New link made. Restart Synapse (synapse restart) to apply it —")
        print("  everyone using the old link is signed out then.")


def _ensure_jwt_secret():
    """Ensure SYNAPSE_JWT_SECRET exists in .env. Generate if missing."""
    env_file = _env_file()
    var = "SYNAPSE_JWT_SECRET"

    if os.environ.get(var):
        return

    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith(f"{var}=") and len(line) > len(f"{var}="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        os.environ[var] = val
                        return
        except Exception:
            pass

    import secrets as _secrets
    secret = _secrets.token_hex(32)
    os.environ[var] = secret
    try:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write(f"\n# JWT secret for session tokens (auto-generated)\n")
            f.write(f"{var}={secret}\n")
    except Exception as e:
        print(f"  Warning: could not write {var} to .env: {e}")


def _reset_password_command():
    """Reset the Synapse UI login password via the CLI."""
    import getpass
    import json as _json

    backend_dir = str(BACKEND_DIR)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    from synapse import _store
    _store.prepare()
    settings = _store.load_settings()

    if not settings.get("login_enabled"):
        print("  Login is not currently enabled.")
        print("  Enable it first in Settings -> General -> Require Login.")
        sys.exit(1)

    current_username = settings.get("login_username", "")
    try:
        prompt = f"  Username [{current_username}]: " if current_username else "  Username: "
        username = input(prompt).strip()
        if not username:
            username = current_username
        if not username:
            print("  Error: Username cannot be empty.")
            sys.exit(1)

        password = getpass.getpass("  New password: ")
        confirm = getpass.getpass("  Confirm password: ")
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborted.")
        sys.exit(0)

    if password != confirm:
        print("  Error: Passwords do not match.")
        sys.exit(1)
    if len(password) < 8:
        print("  Error: Password must be at least 8 characters.")
        sys.exit(1)

    from core.user_auth import hash_password
    settings["login_username"] = username
    settings["login_password_hash"] = hash_password(password)

    try:
        _store.save_settings({
            "login_username": username,
            "login_password_hash": settings["login_password_hash"],
        })
        print("\n  Password reset successfully.")
        print("  Re-login will be required if a session was active.")
    except Exception as e:
        print(f"  Error writing settings: {e}")
        sys.exit(1)


def _migrate_command(sources=None, logs=None, as_json: bool = False):
    """Bring the database up to date, and import a pre-database install.

    The backend does both of these itself on every start, so this exists for
    the cases where running the server is the wrong way to ask: a deploy that
    wants the schema in place before the first request, a container entrypoint,
    or anyone who would rather see what a migration did than infer it from the
    logs.

    `sources` and `logs` name legacy folders explicitly — the installer does,
    for a v1.x install whose files are not where this process would look. A
    named folder is imported even into a store that already has content: the
    caller has said which folder is the user's, and the import retires it, so
    naming it twice finds nothing the second time.
    """
    if sources or logs or as_json:
        _migrate_named(sources or [], logs or [], as_json)
        return

    from synapse import _store

    print("\n  Synapse - Migrate\n")
    try:
        # Touching the store creates missing tables and applies any additive
        # column changes; both print what they did.
        _store.run(_noop)
        print("  Database is up to date.")
    except Exception as e:
        print(f"  Error: could not open the database: {e}")
        sys.exit(1)

    try:
        counts = _store.run(_import_legacy)
    except Exception as e:
        print(f"  Error during import: {e}")
        sys.exit(1)

    if counts is None:
        print("  No pre-database install found to import.")
    else:
        moved = ", ".join(f"{k}={v}" for k, v in counts.items() if v) or "nothing"
        print(f"  Imported: {moved}")
    print()


async def _noop():
    return None


async def _import_legacy():
    from core.store.importer import import_legacy_data_if_present
    return await import_legacy_data_if_present()


def _migrate_named(sources: list, logs: list, as_json: bool) -> None:
    """`synapse migrate --from DIR --logs DIR [--json]`.

    With `--json`, stdout carries one JSON document and nothing else — the store
    prints what it migrated when it first opens, and that goes to stderr — and
    the exit code is 1 when the database could not be opened or any folder
    failed to import.
    """
    import contextlib
    import json as _json

    from synapse import _store

    async def _run():
        from core.store.importer import import_sources, merged_counts
        from core.store.log_importer import import_log_sources

        data = await import_sources([Path(s).expanduser() for s in sources])
        log_reports = await import_log_sources([Path(s).expanduser() for s in logs])
        return {
            "database": "ok",
            "sources": data,
            "logs": log_reports,
            "totals": merged_counts(
                [r for r in data if "error" not in r]
                + [{"counts": r["counts"]} for r in log_reports if "error" not in r]
            ),
        }

    try:
        with contextlib.redirect_stdout(sys.stderr):
            report = _store.run(_run)
    except Exception as e:
        report = {"database": "error", "error": f"{type(e).__name__}: {e}",
                  "sources": [], "logs": [], "totals": {}}

    failed = report["database"] != "ok" or any(
        "error" in r for r in report["sources"] + report["logs"]
    )
    if as_json:
        print(_json.dumps(report))
    else:
        print("\n  Synapse - Migrate\n")
        if report["database"] != "ok":
            print(f"  Error: could not open the database: {report['error']}")
        for r in report["sources"] + report["logs"]:
            if "error" in r:
                print(f"  {r['path']}: failed, left in place: {r['error']}")
            else:
                moved = _counts_line(r)
                print(f"  {r['path']}: {moved}; old files now under {r['retired_to']}")
        print()
    if failed:
        sys.exit(1)


def _counts_line(report: dict) -> str:
    """`orchestrations=2, chat_sessions=31` for one folder's report."""
    parts = []
    for source in ("counts", "files"):
        parts += [f"{k}={v}" for k, v in (report.get(source) or {}).items() if v]
    return ", ".join(parts) or "nothing to import"


def _api_keys_command(action: str, name: str = "", key_id: str = ""):
    """Manage API keys for external /api/v1/* access."""
    from synapse import _store
    _store.prepare()

    if action == "generate":
        from core.api_keys import generate_api_key
        key_name = name or "CLI-generated key"
        raw_key, record = _store.run(lambda: generate_api_key(key_name))
        masked_key = f"{raw_key[:6]}...{raw_key[-4:]}" if len(raw_key) >= 10 else "****"
        print(f"\n  API Key generated successfully!")
        print(f"  Name:    {record['name']}")
        print(f"  Key:     {masked_key}  (copy full key below)")
        print(f"  ID:      {record['id']}")
        print(f"\n  !  Save this key now - it cannot be retrieved again.")
        print(f"\n  {raw_key}\n")
        # Where this install answers, not a hard-coded port (D124, D126).
        from synapse.client.target import resolve_origin
        _origin, api_base = resolve_origin()
        print(f"  Usage:")
        print(f"    curl -X POST {api_base}/chat \\")
        print(f"      -H 'Authorization: Bearer <YOUR_API_KEY>' \\")
        print(f"      -H 'Content-Type: application/json' \\")
        print(f"      -d '{{\"message\": \"hello\"}}'")
        print()

    elif action == "list":
        from core.api_keys import list_api_keys
        keys = _store.run(list_api_keys)
        if not keys:
            print("  No API keys found. Generate one with: synapse api-keys generate \"My App\"")
            return
        print(f"\n  {'PREFIX':<18} {'NAME':<25} {'CREATED':<22} {'LAST USED':<22} {'ACTIVE'}")
        print(f"  {'-' * 18} {'-' * 25} {'-' * 22} {'-' * 22} {'-' * 6}")
        for k in keys:
            active = "[x]" if k.get("is_active", True) else "[ ]"
            last_used = k.get("last_used_at") or "never"
            print(f"  {k['key_prefix']:<18} {k['name']:<25} {k['created_at']:<22} {last_used:<22} {active}")
        print(f"\n  Total: {len(keys)} key(s)")
        print()

    elif action == "revoke":
        if not key_id:
            print("  Error: key ID required. Get IDs with: synapse api-keys list")
            sys.exit(1)
        from core.api_keys import delete_api_key
        if _store.run(lambda: delete_api_key(key_id)):
            print(f"  API key {key_id} deleted.")
        else:
            print(f"  API key {key_id} not found.")
            sys.exit(1)

    else:
        print(f"  Unknown action: {action}")
        print(f"  Usage: synapse api-keys [generate|list|revoke]")
        sys.exit(1)


def _effective_ports(backend_port: int | None = None,
                     frontend_port: int | None = None) -> tuple[int, int]:
    """The ports to use: CLI arg > stored setting > env var > default."""
    saved_backend = saved_frontend = None
    try:
        from synapse import _store
        with _quiet_stdout():
            s = _store.load_settings()
        if s.get("backend_port"):
            saved_backend = int(s["backend_port"])
        if s.get("frontend_port"):
            saved_frontend = int(s["frontend_port"])
    except Exception:
        pass
    return (
        backend_port if backend_port is not None else (saved_backend or DEFAULT_BACKEND_PORT),
        frontend_port if frontend_port is not None else (saved_frontend or DEFAULT_FRONTEND_PORT),
    )


def _quiet_stdout():
    """Send a store helper's notes (`[store] added missing columns`) to stderr."""
    import contextlib
    return contextlib.redirect_stdout(sys.stderr)


def _state_file() -> Path:
    return _run_dir() / "state.json"


def _write_state(backend_port: int, frontend_port: int) -> None:
    """What `synapse status` reports: the ports this run actually used."""
    import json as _json
    try:
        _state_file().write_text(_json.dumps({
            "backend_port": backend_port,
            "frontend_port": frontend_port,
            "version": _get_current_version(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }), encoding="utf-8")
    except OSError:
        pass


def _read_state() -> dict:
    import json as _json
    try:
        return _json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _start_command(
    detach: bool = False,
    no_browser: bool = False,
    backend_port: int | None = None,
    frontend_port: int | None = None,
    profile: bool = False,
    verbose: bool = False,
):
    check_prerequisites()
    _ensure_internal_token()
    _ensure_jwt_secret()
    _ensure_access_token()

    # First run — nothing configured yet, so run the wizard before starting.
    # Only with someone at a terminal to answer it: under a service manager
    # (start on login) the wizard's first read hits end-of-input and exits 0,
    # which used to end the start silently. The app shows what is missing.
    from synapse import _store
    if sys.stdin.isatty() and not _store.is_configured():
        try:
            from synapse import setup_wizard
            setup_wizard.run()
        except Exception as e:
            print(f"Note: setup wizard error ({e}). Run 'synapse setup' to configure.")

    effective_backend_port, effective_frontend_port = _effective_ports(backend_port, frontend_port)

    ensure_run_dir()
    _ensure_playwright_browsers()
    _ensure_coding_deps()

    # Prevent accidental foreground start if processes already running
    if not detach:
        bp = _read_pidfile(_pid_file("backend"))
        fp = _read_pidfile(_pid_file("frontend"))
        if bp and _is_running(bp):
            print(f"Synapse is already running (backend pid {bp}).")
            print("Run 'synapse stop' first, or add --detach to run alongside.")
            sys.exit(1)
        if fp and _is_running(fp):
            print(f"Synapse is already running (frontend pid {fp}).")
            print("Run 'synapse stop' first, or add --detach to run alongside.")
            sys.exit(1)

    backend_proc = frontend_proc = None
    try:
        with _Spinner("Starting Synapse…"):
            try:
                backend_proc = start_backend(detach=detach, port=effective_backend_port,
                                             profile=profile, verbose=verbose)
            except Exception as e:
                raise _StartFailed(f"the backend could not be launched: {e}", "backend")
            _write_pidfile(_pid_file("backend"), backend_proc.pid)
            _wait_ready(f"http://127.0.0.1:{effective_backend_port}/docs",
                        backend_proc, "backend", "backend")

            try:
                frontend_proc = start_frontend(detach=detach, port=effective_frontend_port,
                                               backend_port=effective_backend_port,
                                               verbose=verbose)
            except SystemExit:
                raise
            except Exception as e:
                raise _StartFailed(f"the frontend could not be launched: {e}", "frontend")
            _write_pidfile(_pid_file("frontend"), frontend_proc.pid)
            _wait_ready(f"http://127.0.0.1:{effective_frontend_port}",
                        frontend_proc, "frontend", "frontend")
    except _StartFailed as failure:
        for proc in (frontend_proc, backend_proc):
            if proc is not None:
                _kill_proc_tree(proc)
        for name in ("backend", "frontend"):
            try:
                _pid_file(name).unlink()
            except OSError:
                pass
        print(f"Synapse did not start: {failure}.")
        if not verbose:
            log = _log_path(failure.log_name)
            lines = _tail(log)
            if lines:
                print(f"  Last lines of {log}:")
                for line in lines:
                    print(f"    {line}")
            print(f"  Full log: synapse logs {failure.log_name}")
        sys.exit(1)

    _write_state(effective_backend_port, effective_frontend_port)

    url = f"http://localhost:{effective_frontend_port}"
    # With login off, the browser is opened with the access link, so the person
    # at this machine never sees the gate (D124). With login on, the plain URL.
    link = url if _login_is_on() else _access_link(effective_frontend_port)
    if not no_browser and not detach:
        threading.Thread(target=open_browser, args=(link,), daemon=True).start()

    print(f"Synapse is running → {link}")
    if link != url:
        print("  Anyone with this link can use this Synapse. Share it only with people who may,")
        print("  or turn on login in Settings. `synapse access-link` prints it again.")
    if detach:
        print("  Stop it with: synapse stop")
        return

    print("Press Ctrl+C to stop.")

    def _shutdown(sig, frame):
        print("\nStopping Synapse...")
        # Kill full process trees -- on Windows terminate() leaves node children alive
        _kill_proc_tree(frontend_proc)
        _kill_proc_tree(backend_proc)
        for name in ("backend", "frontend"):
            try:
                _pid_file(name).unlink()
            except OSError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    code = backend_proc.wait()
    # The backend ended without Ctrl+C: say so, and take the frontend with it
    # rather than leaving a UI that answers every request with an error.
    _kill_proc_tree(frontend_proc)
    for name in ("backend", "frontend"):
        try:
            _pid_file(name).unlink()
        except OSError:
            pass
    print(f"Synapse stopped (the backend exited with code {code}).")
    if not verbose:
        print(f"  See why: synapse logs backend")
    sys.exit(code or 1)


def _stop_command():
    for name, pidfile in (("frontend", _pid_file("frontend")), ("backend", _pid_file("backend"))):
        pid = _read_pidfile(pidfile)
        if not pid:
            print(f"{name.capitalize()}: not running (no pidfile)")
            continue
        if not _is_running(pid):
            print(f"{name.capitalize()}: process {pid} not running; removing pidfile.")
            try:
                pidfile.unlink()
            except Exception:
                pass
            continue
        print(f"Stopping {name} (pid {pid})...")
        ok = _terminate_pid(pid, name)
        if ok:
            print(f"  {name} stopped.")
            try:
                pidfile.unlink()
            except Exception:
                pass
        else:
            print(f"  Failed to stop {name}.")


def _status_command(as_json: bool = False):
    if not as_json:
        for name, pidfile in (("backend", _pid_file("backend")), ("frontend", _pid_file("frontend"))):
            pid = _read_pidfile(pidfile)
            if not pid:
                print(f"{name}: not running")
                continue
            running = _is_running(pid)
            print(f"{name}: {'running' if running else 'stale pid ' + str(pid)}")
        return

    # What the installer reads to print the one line it ends with. `ready` is
    # the frontend answering, not a PID existing: a PID survives a hung server.
    import json as _json
    report: dict = {"version": _get_current_version(), "home": str(synapse_home() or ROOT_DIR)}
    for name in ("backend", "frontend"):
        pid = _read_pidfile(_pid_file(name))
        report[name] = {"pid": pid, "running": bool(pid and _is_running(pid))}
    state = _read_state()
    if state.get("frontend_port"):
        frontend_port = int(state["frontend_port"])
    else:
        frontend_port = _effective_ports()[1]
    url = f"http://localhost:{frontend_port}"
    ready = False
    if report["frontend"]["running"]:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{frontend_port}", timeout=3)
            ready = True
        except Exception:
            pass
    login = _login_is_on()
    report.update(url=url, ready=ready, login=login,
                  link=url if login else _access_link(frontend_port))
    print(_json.dumps(report))


def _logs_command(name: str = "backend", lines: int = 50, follow: bool = False):
    """`synapse logs [backend|frontend|install] [-n N] [-f]`."""
    path = _log_path(name)
    if not path.exists():
        print(f"No {name} log yet ({path}).")
        return
    for line in _tail(path, lines):
        print(line)
    if not follow:
        return
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            while True:
                chunk = f.readline()
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass


def _autostart_command(action: str) -> None:
    """`synapse autostart on|off|status` — the installer's question, asked again."""
    from synapse.installer import autostart

    if action == "status":
        print("on" if autostart.is_on() else "off")
        return
    if action == "on":
        if not autostart.supported():
            print("Start on login is not available here: there is no systemd user session.")
            sys.exit(1)
        runtime = _runtime_dir()
        node_bin = runtime / "node" / ("" if IS_WIN else "bin") if runtime else None
        autostart.enable(sys.executable, synapse_home() or Path.home() / ".synapse",
                         [node_bin] if node_bin and node_bin.is_dir() else [])
        print("Synapse will start when you log in.")
    else:
        autostart.disable()
        print("Synapse will no longer start when you log in.")
    try:
        from synapse import _store
        with _quiet_stdout():
            _store.save_settings({"start_on_boot": action == "on"})
    except Exception:
        pass


#: Keys the installer may write through `synapse setup --apply`, beyond the
#: shipped defaults: what `synapse start` and the app read about the install.
_INSTALL_KEYS = {"installed_at", "backend_port", "frontend_port",
                 "playwright_browsers_path", "start_on_boot"}


def _setup_apply_command() -> None:
    """`synapse setup --apply -`: the installer's answers, as JSON on stdin.

    ``{"settings": {...}, "default_agent": true}``. On stdin rather than in
    arguments so an API key never appears in the process list. Only known
    keys are written — a typo in the installer must not become a settings row
    nothing reads — and only the keys given, never the whole default set, so
    an upgrade's answers cannot reset what the user changed in the app.
    Prints ``{"saved": [...], "ignored": [...], "default_agent": "created"|"exists"|null}``.
    """
    import json as _json

    from synapse import _store

    payload = _json.loads(sys.stdin.read() or "{}")
    settings = payload.get("settings") or {}
    _store.prepare()
    from core.config import default_settings

    allowed = set(default_settings()) | _INSTALL_KEYS
    values = {k: v for k, v in settings.items() if k in allowed}
    ignored = sorted(k for k in settings if k not in allowed)

    async def _apply():
        from core.store.settings import save_many
        if values:
            await save_many(values)
        if payload.get("default_agent"):
            from core.default_agent import ensure_default_agent
            return "created" if await ensure_default_agent() else "exists"
        return None

    with _quiet_stdout():
        agent = _store.run(_apply)
    print(_json.dumps({"saved": sorted(values), "ignored": ignored, "default_agent": agent}))


def _get_current_version() -> str:
    """This install's version.

    A source checkout reads `pyproject.toml`, which is the truth there even when
    an older copy is also pip-installed. A pip install has no `pyproject.toml`
    beside the package — the wheel does not ship it — so it reads the installed
    distribution's metadata; it used to report 0.0.0 (F80).
    """
    try:
        content = (ROOT_DIR / "pyproject.toml").read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("version") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("synapse-orch-ai") or "0.0.0"
    except Exception:
        return "0.0.0"


def _parse_version(v: str) -> tuple:
    v = v.lstrip("v")
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _install_method() -> str:
    """How this copy was installed: "uv", "pip", "npm", or "checkout".

    Read from the running process rather than trusted from install.json, which
    a hand-installed copy does not have: uv writes a receipt into the tool's
    environment, and the npm wrapper says so in the environment it starts us in.
    """
    if not _is_packaged():
        return "checkout"
    if os.environ.get("SYNAPSE_LAUNCHER") == "npm":
        return "npm"
    if (Path(sys.prefix) / "uv-receipt.toml").exists():
        return "uv"
    return "pip"


RELEASES = "https://github.com/synapseorch-ai/synapse-ai/releases"


def _latest_version() -> "str | None":
    """The newest Synapse on PyPI, or None when PyPI cannot be reached."""
    from synapse.installer import net

    try:
        return net.get_json("https://pypi.org/pypi/synapse-orch-ai/json", timeout=15)["info"]["version"]
    except (net.HttpError, KeyError, TypeError):
        return None


def _upgrade_command(version: str = ""):
    """Install the newest Synapse over this one; data and settings stay.

    uv installs (the installer's) are upgraded by that release's own installer,
    with the same screen: stop, back up the database, install, update the
    database, and start again if it was running. pip installs do the same
    with pip. A checkout is updated with git.
    """
    method = _install_method()
    if method == "checkout":
        print("This is a source checkout: update it with `git pull`.")
        sys.exit(1)
    current = _get_current_version()
    target = version or _latest_version()
    if target is None:
        print("Could not reach PyPI to look for a newer Synapse. Try again in a while.")
        sys.exit(1)
    if not version and _parse_version(target) <= _parse_version(current):
        print(f"Synapse {current} is up to date.")
        return
    if method == "npm":
        # The npm package decides which Synapse its `synapse` runs; upgrading
        # only the Python side would be undone at its next start.
        print(f"Synapse {target} is out. Upgrade it the way it was installed:")
        print(f"  npm install -g synapse-orch-ai@{target}")
        return
    if method == "pip":
        sys.exit(_upgrade_with_pip(current, target))
    _upgrade_with_installer(target)


def _upgrade_with_installer(target: str) -> None:
    """Hand over to release `target`'s own installer (`synapse-install.pyz upgrade`)."""
    from synapse.installer import net, runtime
    from synapse.installer.home import home_dir, read_install

    home = synapse_home() or home_dir()
    record = read_install(home)
    uv = record.get("uv") or ""
    if not uv or not Path(uv).is_file():
        found = runtime.find_uv(home)
        uv = str(found) if found else ""
    if not uv:
        print("uv, which this install was made with, is missing. Reinstall with:")
        print(f"  curl -LsSf {RELEASES}/latest/download/setup.sh | bash")
        sys.exit(1)
    base = f"{RELEASES}/download/v{target}"
    pyz = home / "runtime" / f"synapse-install-{target}.pyz"
    with _Spinner(f"Getting Synapse {target}…"):
        try:
            net.download(f"{base}/synapse-install.pyz", pyz)
            expected = net.get_text(f"{base}/synapse-install.pyz.sha256").split()[0]
        except net.HttpError as e:
            print(f"Could not download the {target} installer: {e}")
            sys.exit(1)
    import hashlib
    if hashlib.sha256(pyz.read_bytes()).hexdigest() != expected:
        pyz.unlink()
        print("The downloaded installer did not match its published checksum. Nothing was changed.")
        sys.exit(1)
    cmd = [uv, "run", "--no-project", "--quiet", "--python", "3.12", "python", str(pyz),
           "upgrade", "--uv", uv, "--version", target]
    if IS_WIN:
        # This process runs from the files the upgrade replaces, and Windows
        # will not replace a running .exe. The new installer waits for this
        # one to exit, in its own window.
        subprocess.Popen(cmd + ["--wait-pid", str(os.getpid())],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
        print("The upgrade continues in a new window.")
        return
    os.execv(uv, cmd)


def _upgrade_with_pip(current: str, target: str) -> int:
    """pip's upgrade, on the installer's screen: stop, back up, install, migrate, start."""
    from synapse.installer import backup
    from synapse.installer.runner import CommandFailed, Log, run
    from synapse.installer.screen import make_screen
    from synapse.installer.tui import Term

    home = synapse_home() or Path.home() / ".synapse"
    term = Term()
    screen = make_screen(term, f"Synapse {current} {term.g('arrow')} {target}")
    for key, title in (("stop", "Stopping Synapse"), ("backup", "Backing up the database"),
                       ("install", f"Installing {target}"), ("migrate", "Updating the database"),
                       ("start", "Starting")):
        screen.add(key, title)
    log = Log(home / "logs" / "install.log")
    screen.open()
    link = ""
    try:
        was_running = any(_is_running(p) for p in
                          (_read_pidfile(_pid_file(n)) for n in ("backend", "frontend")) if p)
        screen.start("stop")
        if was_running:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                _stop_command()
        screen.done("stop", "stopped" if was_running else "not running")

        screen.start("backup")
        db = _var_dir() / "synapse.db"
        if db.is_file() and not os.environ.get("SYNAPSE_DB_URL"):
            copy = backup.backup_sqlite(db, home / "backups", current)
            screen.done("backup", str(copy).replace(str(Path.home()), "~", 1))
        else:
            screen.skip("backup")

        screen.start("install")
        run([sys.executable, "-m", "pip", "install", "--upgrade", f"synapse-orch-ai=={target}"], log)
        screen.done("install", target)

        # The new code from here on, in a new interpreter.
        screen.start("migrate")
        run([sys.executable, "-m", "synapse", "migrate", "--json"], log)
        screen.done("migrate", "up to date")

        if was_running:
            screen.start("start")
            run([sys.executable, "-m", "synapse", "start", "--detach", "--no-browser"], log)
            status = subprocess.run([sys.executable, "-m", "synapse", "status", "--json"],
                                    capture_output=True, text=True)
            import json as _json
            link = (_json.loads(status.stdout.strip().splitlines()[-1]) or {}).get("link", "")
            screen.done("start")
        else:
            screen.skip("start")
    except (CommandFailed, backup.BackupError, OSError, ValueError) as e:
        failed = next((s for s in screen.steps if s.state == "running"), None)
        if failed:
            screen.fail(failed.key)
        tail = getattr(e, "tail", [])
        screen.close(["", f"  Upgrade failed: {e}", *[f"    {line}" for line in tail[-6:]],
                      f"  Full log: {log.path}", ""])
        log.close()
        return 1
    log.close()
    if link:
        screen.close(["", f"  Synapse {target} is running {term.g('arrow')} {link}", ""])
    else:
        screen.close(["", f"  Synapse {target} is installed. Start it with: synapse start", ""])
    term.close()
    return 0


def _uninstall_command(keep_data: bool = False):
    """Stop Synapse and remove it; with --keep-data, the data stays for a reinstall."""
    from synapse.installer import autostart

    method = _install_method()
    if method == "checkout":
        print("This is a source checkout; delete the folder to remove it.")
        sys.exit(1)
    home = synapse_home() or Path.home() / ".synapse"
    if keep_data:
        print(f"This removes Synapse. Your data stays in {home}.")
    else:
        print(f"This removes Synapse and everything in {home}:")
        print("your agents, workflows, chats, settings and backups.")
    try:
        answer = input("Type 'yes' to confirm: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "yes":
        print("Nothing was removed.")
        return

    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            _stop_command()
        except Exception:
            pass
    autostart.disable()

    if method == "uv":
        from synapse.installer import runtime
        from synapse.installer.home import read_install
        uv = read_install(home).get("uv") or str(runtime.find_uv(home) or "")
        if uv:
            subprocess.run([uv, "tool", "uninstall", "synapse-orch-ai"], capture_output=True)
    elif method == "pip":
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "synapse-orch-ai"],
                       capture_output=True)

    if keep_data:
        for leftover in ("runtime", "run", "logs", "install.json", "install.lock"):
            target = home / leftover
            if target.is_dir():
                _rmtree(target)
            elif target.exists():
                target.unlink()
    elif home.exists():
        _rmtree(home)

    print("Synapse has been removed." + (f" Your data is still in {home}." if keep_data else ""))
    if method == "npm":
        print("Finish with: npm uninstall -g synapse-orch-ai")


def _profile_session(origin: str) -> tuple[str | None, bool]:
    """A session for this machine's internal `/api/*`, as the browser holds one (S9, F171).

    With login on, the one `synapse login` saved. With login off and an access
    token configured (every `synapse start` writes one since D124), the token
    swapped for a session at `/api/auth/access`, as the browser's link is.
    Returns (session or None, whether login is on).
    """
    import json

    from synapse.client import target as _target

    try:
        login_on = _target._login_settings() is not None
    except Exception:
        login_on = False
    if login_on:
        entry = _target._read_config().get("targets", {}).get(origin, {})
        return entry.get("session"), True

    link = _read_env_var(ACCESS_TOKEN_VAR)
    if not link:
        return None, False
    req = urllib.request.Request(
        f"{origin}/api/auth/access", method="POST",
        data=json.dumps({"token": link}).encode(), headers={"Content-Type": "application/json"},
    )
    token = os.getenv("SYNAPSE_INTERNAL_TOKEN")
    if token:
        req.add_header("X-Synapse-Internal", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}").get("token"), False
    except Exception:
        # Refused or unreachable: the profiling call says which, as it would anyway.
        return None, False


def _profile_command(action: str, output: str | None = None, limit: int = 20, duration: int = 30):
    from synapse.client import target as _target

    # Resolved as `synapse start` and `synapse login` resolve it (stored > env >
    # default), so the session `login` saved is found under the same address.
    backend_port = _target.local_port()
    origin = f"http://127.0.0.1:{backend_port}"
    base_url = f"{origin}/api/profiling"
    session, login_on = _profile_session(origin)

    def _api(method: str, path: str, params: str = "") -> dict | str | None:
        url = f"{base_url}{path}"
        if params:
            url += f"?{params}"
        req = urllib.request.Request(url, method=method)
        # `/api/*` wants the install's internal token whenever one is set (F160; its commit says F158).
        token = os.getenv("SYNAPSE_INTERNAL_TOKEN")
        if token:
            req.add_header("X-Synapse-Internal", token)
        # With login on, the backend also wants a session (S9): the one
        # `synapse login` saved for this machine's backend.
        if session:
            req.add_header("X-Synapse-Session", session)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get("Content-Type", "")
                body = resp.read()
                if "application/json" in ct:
                    import json
                    return json.loads(body)
                return body.decode()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                if login_on:
                    print("This Synapse has login turned on. Run `synapse login` first.")
                else:
                    print("This Synapse refused the session its access link opened. "
                          "Check `synapse access-link`, then try again.")
                return None
            print(f"Error {e.code}: {e.read().decode()}")
            return None
        except Exception as e:
            print(f"Could not reach backend at {url}: {e}")
            print("Make sure Synapse is running (synapse start).")
            return None

    if action == "stats":
        data = _api("GET", "/stats")
        if not data:
            return
        if not data:
            print("No timing data yet. Send some requests first.")
            return
        col_w = max((len(k) for k in data), default=20)
        header = f"{'Endpoint':<{col_w}}  {'Count':>6}  {'Avg ms':>8}  {'p50 ms':>8}  {'p95 ms':>8}  {'p99 ms':>8}  {'Max ms':>8}"
        print(header)
        print("-" * len(header))
        for endpoint, s in sorted(data.items()):
            print(f"{endpoint:<{col_w}}  {s['count']:>6}  {s['avg_ms']:>8.1f}  {s['p50_ms']:>8.1f}  {s['p95_ms']:>8.1f}  {s['p99_ms']:>8.1f}  {s['max_ms']:>8.1f}")

    elif action == "reset":
        result = _api("DELETE", "/stats")
        if result:
            print("Timing stats reset.")

    elif action == "cpu-start":
        result = _api("POST", "/cpu/start")
        if result:
            print(result.get("status") or result.get("error"))

    elif action == "cpu-report":
        fmt = "html" if (output and output.endswith(".html")) else "text"
        result = _api("GET", "/cpu/report", f"format={fmt}")
        if result is None:
            return
        if output:
            Path(output).write_text(result, encoding="utf-8")
            print(f"CPU profile saved to {output}")
        else:
            print(result)

    elif action == "memory-start":
        result = _api("POST", "/memory/start")
        if result:
            print(result.get("status") or result.get("error"))

    elif action == "memory-snapshot":
        data = _api("GET", "/memory/snapshot", f"limit={limit}")
        if not data:
            return
        if "error" in data:
            print(data["error"])
            return
        print(f"Current: {data['current_mb']} MB  |  Peak: {data['peak_mb']} MB\n")
        print(f"{'Size KB':>10}  {'Count':>6}  Location")
        print("-" * 60)
        for alloc in data["top_allocations"]:
            print(f"{alloc['size_kb']:>10.2f}  {alloc['count']:>6}  {alloc['file']}:{alloc['line']}")

    elif action == "spy":
        pid = _read_pidfile(_pid_file("backend"))
        if not pid:
            print("Backend PID not found. Start with: synapse start --detach")
            return
        if not _is_running(pid):
            print(f"Backend process {pid} is not running.")
            return
        out_file = output or "profile.svg"
        cmd = ["py-spy", "record", "-o", out_file, "--pid", str(pid), "--duration", str(duration)]
        print(f"Running: {' '.join(cmd)}")
        print(f"Send requests to the backend during this {duration}s window...")
        try:
            subprocess.run(cmd, check=True)
            print(f"Flame graph saved to {out_file}")
        except FileNotFoundError:
            print("py-spy not found. Install it: pip install py-spy")
        except subprocess.CalledProcessError as e:
            print(f"py-spy failed: {e}")

    else:
        print(f"Unknown profile action: {action}")
        print("Available: stats, reset, cpu-start, cpu-report, memory-start, memory-snapshot, spy")


MIN_PYTHON = (3, 11)
# Exclusive upper bound — keep in sync with requires-python in pyproject.toml.
# Interpreters at or above this haven't been tested and will typically fail to
# resolve prebuilt wheels for native dependencies.
MAX_PYTHON = (3, 15)
MIN_NODE   = (20, 9, 0)


def _warn_if_not_on_path():
    if sys.platform == "win32" and not shutil.which("synapse"):  # type: ignore[unreachable]
        print(  # type: ignore[unreachable]
            "\nNote: 'synapse' is not on your PATH.\n"
            "You can run Synapse with:  python -m synapse\n"
            "To fix permanently, add your Python Scripts folder to PATH.\n",
            file=sys.stderr,
        )


def _warn_versions():
    """Warn (non-fatally) if Python or Node.js versions are below the minimum required.

    On stderr, and only for the commands that build or run the server (D126):
    an API command needs no Node, and its `--json` output must stay JSON.
    """
    # ── Python ───────────────────────────────────────────────────────────────
    py = sys.version_info[:2]
    if py < MIN_PYTHON:
        print(
            f"Warning: Python {py[0]}.{py[1]} detected -- "
            f"Synapse requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+.\n"
            "  Please switch to Python 3.11 or newer (https://www.python.org/downloads/)\n"
            "  and reinstall: pip install synapse-ai",
            file=sys.stderr,
        )
    elif py >= MAX_PYTHON:
        print(
            f"Warning: Python {py[0]}.{py[1]} detected -- "
            f"Synapse is tested up to Python {MAX_PYTHON[0]}.{MAX_PYTHON[1] - 1}.\n"
            "  Dependencies may not have prebuilt wheels for this interpreter.",
            file=sys.stderr,
        )

    # ── Node.js ───────────────────────────────────────────────────────────────
    node = None
    if IS_WIN:
        node, _ = _find_node_exe_win()
        if node is None:
            node = shutil.which("node")
    else:
        node = shutil.which("node")

    if node is None:
        print(
            f"Warning: node not found -- Node.js {'.'.join(str(x) for x in MIN_NODE)}+ is required.\n"
            "  Install from https://nodejs.org/",
            file=sys.stderr,
        )
    else:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
            ver_str = r.stdout.strip().lstrip("v")
            ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
            if ver_tuple < MIN_NODE:
                min_str = ".".join(str(x) for x in MIN_NODE)
                print(
                    f"Warning: Node.js {ver_str} detected -- "
                    f"Synapse requires Node.js {min_str}+.\n"
                    f"  Please upgrade from https://nodejs.org/\n"
                    f"  After upgrading, rebuild the frontend:\n"
                    f"    cd {FRONTEND_DIR} && npm install && npm run build",
                    file=sys.stderr,
                )
        except Exception:
            pass  # version check failed; let downstream tools surface the error


def _force_utf8_streams():
    """Best-effort: ensure console output uses UTF-8 so stray non-ASCII
    characters never crash or render as garbage on Windows (cp1252)."""
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _legacy_install_root() -> "Path | None":
    """A v1.x script install that 2.0's code has just been unpacked into.

    v1.x's `synapse upgrade` downloads the newest source over its own folder
    and re-runs this file with its old venv's Python. 2.0 does not install
    that way, so it hands over (synapse/installer/handoff.py). Recognised by
    the marker v1.x's setup.py wrote and the venv beside the code; never a
    checkout (a .git), never a packaged install.
    """
    if _is_packaged() or (ROOT_DIR / ".git").exists():
        return None
    if not (ROOT_DIR / ".installed").exists() or not (BACKEND_DIR / "venv").is_dir():
        return None
    return ROOT_DIR


def _finish_pending_cleanup() -> None:
    """Delete what an upgrade could not while it was in use (Windows file locks).

    The installer records a 1.x folder it could not remove as `pending_cleanup`
    in install.json; any later command finishes the job, quietly.
    """
    home = synapse_home()
    if home is None:
        return
    record_path = home / "install.json"
    try:
        import json as _json
        record = _json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pending = record.get("pending_cleanup") or []
    if not pending:
        return
    left = []
    for path in pending:
        target = Path(path)
        try:
            if target.is_dir():
                _rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError:
            pass
        if target.exists():
            left.append(path)
    if left != pending:
        from synapse.installer.home import write_install
        write_install(home, pending_cleanup=left or None)


def main():
    # First, before anything the old venv might not have is imported.
    legacy_root = _legacy_install_root()
    if legacy_root is not None:
        from synapse.installer.handoff import run as _handoff
        sys.exit(_handoff(legacy_root, sys.argv[1:]))
    _finish_pending_cleanup()
    _force_utf8_streams()
    _warn_if_not_on_path()
    parser = argparse.ArgumentParser(prog="synapse", description="Run Synapse, and drive it from the terminal")
    # What SECURITY.md asks a reporter to run (F80).
    parser.add_argument("--version", action="version", version=f"synapse {_get_current_version()}")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    p_start = sub.add_parser("start", help="Start backend and frontend")
    p_start.add_argument("--detach", "-d", action="store_true", help="Run processes in background and write pidfiles")
    p_start.add_argument("--no-browser", action="store_true", help="Do not open a browser on start")
    p_start.add_argument(
        "--backend-port", type=int, default=None, metavar="PORT",
        help=f"Port for the backend API server (overrides SYNAPSE_BACKEND_PORT env var, default: {DEFAULT_BACKEND_PORT})",
    )
    p_start.add_argument(
        "--frontend-port", type=int, default=None, metavar="PORT",
        help=f"Port for the frontend web UI (overrides SYNAPSE_FRONTEND_PORT env var, default: {DEFAULT_FRONTEND_PORT})",
    )
    p_start.add_argument("--profile", action="store_true", help="Enable performance profiling (sets SYNAPSE_PROFILING=true)")
    p_start.add_argument("--verbose", "-v", action="store_true",
                         help="Show the backend and frontend output here instead of in their logs")

    sub.add_parser("stop", help="Stop running backend and frontend (reads pidfiles)")
    p_status = sub.add_parser("status", help="Show status of backend and frontend")
    p_status.add_argument("--json", dest="as_json", action="store_true",
                          help="One JSON object: pids, whether it answers, and its link")

    p_logs = sub.add_parser("logs", help="Show the backend's (or frontend's, or the installer's) log")
    p_logs.add_argument("name", nargs="?", default="backend", choices=["backend", "frontend", "install"])
    p_logs.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="Lines to show (default: 50)")
    p_logs.add_argument("-f", "--follow", action="store_true", help="Keep printing new lines")

    p_restart = sub.add_parser("restart", help="Restart backend and frontend")
    p_restart.add_argument("--detach", "-d", action="store_true", help="After restart, leave processes detached")
    p_restart.add_argument(
        "--backend-port", type=int, default=None, metavar="PORT",
        help=f"Port for the backend API server (overrides SYNAPSE_BACKEND_PORT env var, default: {DEFAULT_BACKEND_PORT})",
    )
    p_restart.add_argument(
        "--frontend-port", type=int, default=None, metavar="PORT",
        help=f"Port for the frontend web UI (overrides SYNAPSE_FRONTEND_PORT env var, default: {DEFAULT_FRONTEND_PORT})",
    )
    p_restart.add_argument("--verbose", "-v", action="store_true",
                           help="Show the backend and frontend output here instead of in their logs")
    p_setup = sub.add_parser("setup", help="Run interactive setup wizard to configure Synapse")
    p_setup.add_argument("--apply", metavar="-", default=None,
                         help="Read the installer's answers as JSON from stdin (pass '-') instead of asking")
    p_migrate = sub.add_parser(
        "migrate",
        help="Create or update the database, and import an old backend/data folder if present",
    )
    p_migrate.add_argument(
        "--from", dest="sources", action="append", metavar="DIR",
        help="A v1.x data folder to import (repeatable; the first named wins on a clash)",
    )
    p_migrate.add_argument(
        "--logs", dest="logs", action="append", metavar="DIR",
        help="A v1.x logs folder to import (repeatable)",
    )
    p_migrate.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Print one JSON report on stdout",
    )

    p_upgrade = sub.add_parser(
        "upgrade", help="Install the newest Synapse over this one; your data and settings stay",
    )
    p_upgrade.add_argument("--version", dest="to_version", default="", metavar="X",
                           help="Install this version instead of the newest")

    p_uninstall = sub.add_parser("uninstall", help="Stop Synapse and remove it")
    p_uninstall.add_argument(
        "--keep-data", action="store_true",
        help="Keep your data, settings and backups (~/.synapse) for a later reinstall",
    )

    p_profile = sub.add_parser("profile", help="Query and control backend performance profiling")
    p_profile.add_argument(
        "action",
        choices=["stats", "reset", "cpu-start", "cpu-report", "memory-start", "memory-snapshot", "spy"],
        help="stats: latency table | reset: clear stats | cpu-start/cpu-report: CPU profiling | memory-start/memory-snapshot: memory profiling | spy: py-spy flame graph",
    )
    p_profile.add_argument("--output", "-o", default=None, metavar="FILE", help="Output file (cpu-report: .html, spy: .svg)")
    p_profile.add_argument("--limit", type=int, default=20, metavar="N", help="Number of top allocations to show (memory-snapshot, default: 20)")
    p_profile.add_argument("--duration", type=int, default=30, metavar="SECS", help="Recording duration in seconds (spy, default: 30)")

    # reset-password: reset the UI login password
    sub.add_parser("reset-password", help="Reset the Synapse UI login password")

    # access-link: the link that opens a login-off Synapse (D124)
    p_autostart = sub.add_parser("autostart", help="Start Synapse when you log in: on, off, or status")
    p_autostart.add_argument("action", choices=["on", "off", "status"])

    p_access = sub.add_parser("access-link", help="Print the link that opens Synapse while login is off")
    p_access.add_argument("--rotate", action="store_true",
                          help="Make a new link; everyone using the old one is signed out at the next restart")

    # api-keys: manage external API keys
    p_apikeys = sub.add_parser("api-keys", help="Manage API keys for external /api/v1/ access")
    p_apikeys.add_argument(
        "action",
        choices=["generate", "list", "revoke"],
        help="generate: create a new key | list: show all keys | revoke: delete a key",
    )
    p_apikeys.add_argument("name_or_id", nargs="?", default="", help="Key name (generate) or key ID (revoke)")

    # The API commands: agents, orchestrations, run, runs, schedules, chat, … (D126)
    from synapse.client import commands as _client_commands
    _client_commands.register(sub)

    args = parser.parse_args()

    if getattr(args, "func", None) is not None:
        sys.exit(args.func(args))

    if args.cmd in (None, "start", "restart", "setup", "upgrade"):
        _warn_versions()

    if args.cmd == "start" or args.cmd is None:
        # default to start when invoked without subcommand to preserve previous behaviour
        _start_command(
            detach=getattr(args, "detach", False),
            no_browser=getattr(args, "no_browser", False),
            backend_port=getattr(args, "backend_port", None),
            frontend_port=getattr(args, "frontend_port", None),
            profile=getattr(args, "profile", False),
            verbose=getattr(args, "verbose", False),
        )
    elif args.cmd == "stop":
        _stop_command()
    elif args.cmd == "migrate":
        _migrate_command(args.sources, args.logs, args.as_json)
    elif args.cmd == "setup":
        if args.apply is not None:
            if args.apply != "-":
                parser.error("setup --apply reads JSON from stdin: pass '-'")
            _setup_apply_command()
            return
        try:
            from synapse import setup_wizard
            setup_wizard.run()
        except Exception as e:
            print(f"Failed to run setup wizard: {e}")
    elif args.cmd == "status":
        _status_command(as_json=args.as_json)
    elif args.cmd == "logs":
        _logs_command(args.name, args.lines, args.follow)
    elif args.cmd == "restart":
        _stop_command()
        _start_command(
            detach=getattr(args, "detach", False),
            backend_port=getattr(args, "backend_port", None),
            frontend_port=getattr(args, "frontend_port", None),
            verbose=getattr(args, "verbose", False),
        )
    elif args.cmd == "upgrade":
        _upgrade_command(args.to_version)
    elif args.cmd == "uninstall":
        _uninstall_command(keep_data=getattr(args, "keep_data", False))
    elif args.cmd == "profile":
        _profile_command(
            action=args.action,
            output=args.output,
            limit=args.limit,
            duration=args.duration,
        )
    elif args.cmd == "reset-password":
        _reset_password_command()
    elif args.cmd == "autostart":
        _autostart_command(args.action)
    elif args.cmd == "access-link":
        _access_link_command(rotate=args.rotate)
    elif args.cmd == "api-keys":
        _api_keys_command(
            action=args.action,
            name=args.name_or_id if args.action == "generate" else "",
            key_id=args.name_or_id if args.action == "revoke" else "",
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
