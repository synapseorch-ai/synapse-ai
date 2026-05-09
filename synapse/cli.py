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


def _fix_bin_permissions():
    """Ensure bin/synapse has execute permissions (chmod 755) on Unix."""
    if IS_WIN:
        return
    synapse_bin = ROOT_DIR / "bin" / "synapse"
    if synapse_bin.exists():
        try:
            synapse_bin.chmod(0o755)
        except PermissionError as e:
            print(f"  Warning: could not set permissions on {synapse_bin}: {e}")
            print(f"  Fix manually: chmod 755 {synapse_bin}")

PACKAGE_DIR = Path(__file__).resolve().parent
# When installed as a package, backend is one level up from synapse/
BACKEND_DIR = PACKAGE_DIR.parent / "backend"
FRONTEND_DIR = PACKAGE_DIR.parent / "frontend"
ROOT_DIR = PACKAGE_DIR.parent

# When installed via pip, the Next.js standalone build is bundled here.
_BUNDLED_FRONTEND = PACKAGE_DIR / "_frontend"


def _system_python() -> str:
    """Return the real system Python executable, not one inside a venv."""
    # If we're already inside a venv (VIRTUAL_ENV is set or sys.prefix !=
    # sys.base_prefix), try sys.base_prefix first — that's the system install
    # that owns this interpreter.
    base = getattr(sys, "base_prefix", sys.prefix)
    if base != sys.prefix:
        # Running inside a venv — find python in the base prefix
        candidates = [
            os.path.join(base, "python.exe" if IS_WIN else "bin/python"),
            os.path.join(base, "Scripts", "python.exe") if IS_WIN else None,
        ]
        for c in candidates:
            if c and os.path.isfile(c):
                return c

    # sys.executable itself might be the venv python; walk up to find the real one
    exe = sys.executable
    if IS_WIN:
        # On Windows a venv python lives at <venv>\Scripts\python.exe
        # The base install is typically 3 levels up: <base>\python.exe
        parts = Path(exe).parts
        if "Scripts" in parts:
            idx = list(parts).index("Scripts")
            # <venv>\Scripts\python.exe  → try <base>\python.exe
            base_dir = Path(*parts[:idx - 1])
            for name in ("python.exe", "python3.exe"):
                candidate = base_dir / name
                if candidate.is_file():
                    return str(candidate)

    # Fall back to whatever shutil can find on PATH
    found = shutil.which("python3") or shutil.which("python")
    if found:
        return found

    return sys.executable

# ---------------------------------------------------------------------------
# Load .env from the project root BEFORE reading port defaults so that values
# set by `synapse setup` (or hand-edited .env) are honoured without the user
# having to export them manually in every shell session.
# ---------------------------------------------------------------------------
_ENV_FILE = ROOT_DIR / ".env"

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

DEFAULT_DATA_DIR = Path.home() / ".synapse" / "data"
# Always resolve to absolute path so the value is correct regardless of CWD.
# When SYNAPSE_DATA_DIR is a relative path (e.g. "data" in .env), resolve it
# relative to the project root rather than wherever `synapse` was invoked from.
_raw_data_dir = os.getenv("SYNAPSE_DATA_DIR", str(DEFAULT_DATA_DIR))
if not os.path.isabs(_raw_data_dir):
    DATA_DIR = (ROOT_DIR / _raw_data_dir).resolve()
else:
    DATA_DIR = Path(_raw_data_dir).resolve()

DEFAULT_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
DEFAULT_FRONTEND_PORT = int(os.getenv("SYNAPSE_FRONTEND_PORT", "3000"))

# Runtime ports (may be overridden by CLI args -- module-level aliases kept for
# backwards compatibility; actual values are resolved in _start_command)
BACKEND_PORT = DEFAULT_BACKEND_PORT
FRONTEND_PORT = DEFAULT_FRONTEND_PORT

DEFAULT_JSON_FILES = {
    "user_agents.json": "[]",
    "orchestrations.json": "[]",
    "repos.json": "[]",
    "mcp_servers.json": "[]",
    "custom_tools.json": "[]",
}

# PID files
BACKEND_PID_FILE = DATA_DIR / "backend.pid"
FRONTEND_PID_FILE = DATA_DIR / "frontend.pid"


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
        if shutil.which("npm") is None:
            errors.append(f"npm not found -- install Node.js {'.'.join(str(x) for x in MIN_NODE)}+ from https://nodejs.org/")
    if shutil.which("ollama") is None:
        print("Warning: ollama not found. Local models won't work; cloud API models (Anthropic, OpenAI, Gemini) still work.")
    if errors:
        for e in errors:
            print(f"Error: {e}")
        sys.exit(1)


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for subdir in ("vault", "datasets", "orchestration_runs", "orchestration_logs"):
        (DATA_DIR / subdir).mkdir(exist_ok=True)
    for filename, default in DEFAULT_JSON_FILES.items():
        target = DATA_DIR / filename
        if not target.exists():
            target.write_text(default)


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
        settings_file = DATA_DIR / "settings.json"
        if settings_file.exists():
            with open(settings_file, "r") as f:
                settings = json.load(f)
            settings["playwright_browsers_path"] = str(browsers_path)
            with open(settings_file, "w") as f:
                json.dump(settings, f, indent=4)
    except Exception as e:
        print(f"\n  Warning: Failed to save playwright_browsers_path to settings: {e}")


def start_backend(detach: bool = False, port: int | None = None, profile: bool = False):
    env = os.environ.copy()
    env["SYNAPSE_DATA_DIR"] = str(DATA_DIR)
    env["PYTHONPATH"] = str(BACKEND_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    if port is not None:
        env["SYNAPSE_BACKEND_PORT"] = str(port)
    if profile:
        env["SYNAPSE_PROFILING"] = "true"
    kwargs = {}
    if detach:
        if os.name == "posix":
            kwargs["preexec_fn"] = os.setsid
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        [sys.executable, str(BACKEND_DIR / "main.py")],
        cwd=str(BACKEND_DIR),
        env=env,
        **kwargs,
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


def start_frontend(detach: bool = False, port: int | None = None, backend_port: int | None = None):
    _backend_port = backend_port if backend_port is not None else DEFAULT_BACKEND_PORT
    _frontend_port = port if port is not None else DEFAULT_FRONTEND_PORT
    env = os.environ.copy()
    env["BACKEND_URL"] = f"http://127.0.0.1:{_backend_port}"
    env["NEXT_PUBLIC_BACKEND_PORT"] = str(_backend_port)
    env["SYNAPSE_FRONTEND_PORT"] = str(_frontend_port)
    env["SYNAPSE_BACKEND_PORT"] = str(_backend_port)
    kwargs = {}
    if detach:
        if os.name == "posix":
            kwargs["preexec_fn"] = os.setsid
        else:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # Pip-installed package: frontend is pre-built standalone at synapse/_frontend/
    if _BUNDLED_FRONTEND.exists():
        # Auto-sync if the source has a newer build (e.g. after synapse upgrade pulled new code).
        standalone_src = FRONTEND_DIR / ".next" / "standalone"
        if standalone_src.exists():
            src_id_file = standalone_src / ".next" / "BUILD_ID"
            bundled_id_file = _BUNDLED_FRONTEND / ".next" / "BUILD_ID"
            src_id = src_id_file.read_text().strip() if src_id_file.exists() else None
            bundled_id = bundled_id_file.read_text().strip() if bundled_id_file.exists() else None
            if src_id and src_id != bundled_id:
                print("  Syncing updated frontend build into synapse/_frontend/...")
                _sync_bundled_frontend(verbose=False)
                print("  Frontend sync complete.")

        server_js = _BUNDLED_FRONTEND / "server.js"
        if not server_js.exists():
            print(f"Error: bundled frontend server not found at {server_js}")
            print("Try reinstalling: pip install --upgrade synapse-ai")
            sys.exit(1)
        node = shutil.which("node")
        if not node:
            print("Error: node not found -- install Node.js 20.9.0+ from https://nodejs.org/")
            sys.exit(1)
        env["PORT"] = str(_frontend_port)
        env["HOSTNAME"] = "0.0.0.0"
        env["NODE_ENV"] = "production"
        return subprocess.Popen(
            [node, str(server_js)],
            cwd=str(_BUNDLED_FRONTEND),
            env=env,
            **kwargs,
        )

    # Dev/source mode: use npm start on the frontend source directory
    next_dir = FRONTEND_DIR / ".next"
    if not next_dir.exists():
        print("Error: frontend is not built. Run the following first:")
        print(f"  cd {FRONTEND_DIR} && npm install && npm run build")
        sys.exit(1)
    npm = _npm_command()
    return subprocess.Popen(
        [npm, "start"],
        cwd=str(FRONTEND_DIR),
        env=env,
        **kwargs,
    )


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


def open_browser(url: str):
    time.sleep(1)
    webbrowser.open(url)


def _write_pidfile(path: Path, pid: int):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(pid))
    except Exception as e:
        print(f"Warning: could not write pidfile {path}: {e}")


def _read_pidfile(path: Path):
    try:
        return int(path.read_text().strip())
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

    print("  Coding-agent dependencies missing or outdated — installing now...")
    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", "-q", "--upgrade", "-r", str(coding_req)],
        capture_output=True,
        text=True,
    )
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
    env_file = ROOT_DIR / ".env"
    token_var = "SYNAPSE_INTERNAL_TOKEN"

    # Check if already in environment (e.g. from .env loaded earlier)
    if os.environ.get(token_var):
        return

    # Check if present in .env file
    if env_file.exists():
        try:
            content = env_file.read_text()
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
        with open(env_file, "a") as f:
            f.write(f"\n# Internal token for frontend↔backend security (auto-generated)\n")
            f.write(f"{token_var}={token}\n")
    except Exception as e:
        print(f"  Warning: could not write {token_var} to .env: {e}")
        print(f"  The token is set in memory for this session.")


def _ensure_jwt_secret():
    """Ensure SYNAPSE_JWT_SECRET exists in .env. Generate if missing."""
    env_file = ROOT_DIR / ".env"
    var = "SYNAPSE_JWT_SECRET"

    if os.environ.get(var):
        return

    if env_file.exists():
        try:
            content = env_file.read_text()
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
        with open(env_file, "a") as f:
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

    settings_file = DATA_DIR / "settings.json"
    if not settings_file.exists():
        print("  Error: settings.json not found. Start Synapse first to initialise it.")
        sys.exit(1)

    try:
        settings = _json.loads(settings_file.read_text())
    except Exception as e:
        print(f"  Error reading settings: {e}")
        sys.exit(1)

    if not settings.get("login_enabled"):
        print("  Login is not currently enabled.")
        print("  Enable it first in Settings → General → Require Login.")
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
        settings_file.write_text(_json.dumps(settings, indent=4))
        print("\n  Password reset successfully.")
        print("  Re-login will be required if a session was active.")
    except Exception as e:
        print(f"  Error writing settings: {e}")
        sys.exit(1)


def _api_keys_command(action: str, name: str = "", key_id: str = ""):
    """Manage API keys for external /api/v1/* access."""
    # Ensure backend modules are importable
    backend_dir = str(BACKEND_DIR)
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)

    if action == "generate":
        from core.api_keys import generate_api_key
        key_name = name or "CLI-generated key"
        raw_key, record = generate_api_key(key_name)
        masked_key = f"{raw_key[:6]}...{raw_key[-4:]}" if len(raw_key) >= 10 else "****"
        print(f"\n  API Key generated successfully!")
        print(f"  Name:    {record['name']}")
        print(f"  Key:     {masked_key}  (copy full key below)")
        print(f"  ID:      {record['id']}")
        print(f"\n  ⚠  Save this key now — it cannot be retrieved again.")
        print(f"\n  {raw_key}\n")
        print(f"  Usage:")
        print(f"    curl -X POST http://localhost:8765/api/v1/chat \\")
        print(f"      -H 'Authorization: Bearer <YOUR_API_KEY>' \\")
        print(f"      -H 'Content-Type: application/json' \\")
        print(f"      -d '{{\"message\": \"hello\"}}'")
        print()

    elif action == "list":
        from core.api_keys import list_api_keys
        keys = list_api_keys()
        if not keys:
            print("  No API keys found. Generate one with: synapse api-keys generate \"My App\"")
            return
        print(f"\n  {'PREFIX':<18} {'NAME':<25} {'CREATED':<22} {'LAST USED':<22} {'ACTIVE'}")
        print(f"  {'─' * 18} {'─' * 25} {'─' * 22} {'─' * 22} {'─' * 6}")
        for k in keys:
            active = "✓" if k.get("is_active", True) else "✗"
            last_used = k.get("last_used_at") or "never"
            print(f"  {k['key_prefix']:<18} {k['name']:<25} {k['created_at']:<22} {last_used:<22} {active}")
        print(f"\n  Total: {len(keys)} key(s)")
        print()

    elif action == "revoke":
        if not key_id:
            print("  Error: key ID required. Get IDs with: synapse api-keys list")
            sys.exit(1)
        from core.api_keys import delete_api_key
        if delete_api_key(key_id):
            print(f"  API key {key_id} deleted.")
        else:
            print(f"  API key {key_id} not found.")
            sys.exit(1)

    else:
        print(f"  Unknown action: {action}")
        print(f"  Usage: synapse api-keys [generate|list|revoke]")
        sys.exit(1)


def _start_command(
    detach: bool = False,
    no_browser: bool = False,
    backend_port: int | None = None,
    frontend_port: int | None = None,
    profile: bool = False,
):
    check_prerequisites()
    _ensure_internal_token()
    _ensure_jwt_secret()

    # First-run: no settings.json yet — run setup wizard before starting
    _settings_file = DATA_DIR / "settings.json"
    if not _settings_file.exists():
        try:
            from synapse import setup_wizard
            setup_wizard.run()
        except Exception as e:
            print(f"Note: setup wizard error ({e}). Run 'synapse setup' to configure.")

    # Resolve effective ports: CLI arg > settings.json > env var > default
    _saved_backend_port: int | None = None
    _saved_frontend_port: int | None = None
    try:
        import json as _json
        _s = _json.loads(_settings_file.read_text())
        if "backend_port" in _s:
            _saved_backend_port = int(_s["backend_port"])
        if "frontend_port" in _s:
            _saved_frontend_port = int(_s["frontend_port"])
    except Exception:
        pass

    effective_backend_port = backend_port if backend_port is not None else (_saved_backend_port or DEFAULT_BACKEND_PORT)
    effective_frontend_port = frontend_port if frontend_port is not None else (_saved_frontend_port or DEFAULT_FRONTEND_PORT)

    ensure_data_dir()
    _ensure_playwright_browsers()
    _ensure_coding_deps()

    # Prevent accidental foreground start if processes already running
    if not detach:
        bp = _read_pidfile(BACKEND_PID_FILE)
        fp = _read_pidfile(FRONTEND_PID_FILE)
        if bp and _is_running(bp):
            print(f"Backend already running (pid {bp}).")
            print("Run 'synapse stop' first, or add --detach to run alongside.")
            sys.exit(1)
        if fp and _is_running(fp):
            print(f"Frontend already running (pid {fp}).")
            print("Run 'synapse stop' first, or add --detach to run alongside.")
            sys.exit(1)

    print(f"Starting backend on port {effective_backend_port}...")
    try:
        backend_proc = start_backend(detach=detach, port=effective_backend_port, profile=profile)
        _write_pidfile(BACKEND_PID_FILE, backend_proc.pid)
    except Exception as e:
        print(f"Failed to start backend: {e}")
        sys.exit(1)

    if not wait_for_url(f"http://127.0.0.1:{effective_backend_port}/docs", "Backend"):
        try:
            backend_proc.terminate()
        except Exception:
            pass
        sys.exit(1)

    print(f"Starting frontend on port {effective_frontend_port}...")
    try:
        frontend_proc = start_frontend(
            detach=detach,
            port=effective_frontend_port,
            backend_port=effective_backend_port,
        )
        _write_pidfile(FRONTEND_PID_FILE, frontend_proc.pid)
    except Exception as e:
        print(f"Failed to start frontend: {e}")
        try:
            backend_proc.terminate()
        except Exception:
            pass
        sys.exit(1)

    if not wait_for_url(f"http://127.0.0.1:{effective_frontend_port}", "Frontend"):
        try:
            backend_proc.terminate()
        except Exception:
            pass
        try:
            frontend_proc.terminate()
        except Exception:
            pass
        sys.exit(1)

    url = f"http://localhost:{effective_frontend_port}"
    if not no_browser and not detach:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    print(f"\nSynapse is running at {url}")
    if detach:
        print(f"  Backend pid:  {_read_pidfile(BACKEND_PID_FILE)}  (port {effective_backend_port})")
        print(f"  Frontend pid: {_read_pidfile(FRONTEND_PID_FILE)}  (port {effective_frontend_port})")
        print()
        print("Run 'synapse stop' to stop  |  'synapse status' to check")
        return

    print("Press Ctrl+C to stop.\n")

    def _shutdown(sig, frame):
        print("\nStopping Synapse...")
        # Kill full process trees -- on Windows terminate() leaves node children alive
        _kill_proc_tree(frontend_proc)
        _kill_proc_tree(backend_proc)
        try:
            if BACKEND_PID_FILE.exists():
                BACKEND_PID_FILE.unlink()
        except Exception:
            pass
        try:
            if FRONTEND_PID_FILE.exists():
                FRONTEND_PID_FILE.unlink()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    backend_proc.wait()


def _stop_command():
    for name, pidfile in (("frontend", FRONTEND_PID_FILE), ("backend", BACKEND_PID_FILE)):
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


def _status_command():
    for name, pidfile in (("backend", BACKEND_PID_FILE), ("frontend", FRONTEND_PID_FILE)):
        pid = _read_pidfile(pidfile)
        if not pid:
            print(f"{name}: not running")
            continue
        running = _is_running(pid)
        print(f"{name}: {'running' if running else 'stale pid ' + str(pid)}")


def _get_current_version() -> str:
    """Read the version string from pyproject.toml."""
    try:
        content = (ROOT_DIR / "pyproject.toml").read_text()
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("version") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return "0.0.0"


def _parse_version(v: str) -> tuple:
    v = v.lstrip("v")
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _register_synapse_pth(venv_dir: str, root_dir: str) -> None:
    """Add root_dir to the venv's site-packages via a .pth file.

    Equivalent to pip install -e but skips the build hook entirely —
    no bash, no npm, no hatchling required.
    """
    import glob as _glob
    if IS_WIN:
        site_pkgs = os.path.join(venv_dir, "Lib", "site-packages")
    else:
        candidates = sorted(
            _glob.glob(os.path.join(venv_dir, "lib", "python*", "site-packages"))
        )
        if not candidates:
            print(f"  Warning: could not locate site-packages inside {venv_dir}")
            return
        site_pkgs = candidates[-1]
    os.makedirs(site_pkgs, exist_ok=True)
    pth = os.path.join(site_pkgs, "synapse-source.pth")
    with open(pth, "w") as f:
        f.write(str(root_dir) + "\n")


def _get_latest_github_release() -> "tuple[str, str] | tuple[None, None]":
    """Return (tag_name, tarball_url) for the latest GitHub release, or (None, None) on failure."""
    import urllib.request as _req
    import json as _json
    url = "https://api.github.com/repos/naveenraj-17/synapse-ai/releases/latest"
    try:
        req = _req.Request(url, headers={"User-Agent": "synapse-upgrade/1.0"})
        with _req.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
        return data.get("tag_name", ""), data.get("tarball_url", "")
    except Exception as e:
        print(f"  Warning: could not fetch release info: {e}")
        return None, None


def _download_and_apply_release(tarball_url: str) -> bool:
    """Download the release tarball and overwrite source files, preserving user data."""
    import tempfile, tarfile
    import urllib.request as _req

    SKIP = {
        # User data — must never be overwritten
        "backend/data",
        # Runtime / generated — large and not part of releases
        "backend/venv",
        "backend/logs",
        "backend/chroma_db",
        "backend/.playwright-mcp",
        "backend/node_modules",
        "frontend/node_modules",
        # Note: frontend/.next and synapse/_frontend are intentionally NOT skipped —
        # they get rebuilt by npm build + _sync_bundled_frontend() during upgrade.
    }

    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, "release.tar.gz")
        print("  Downloading...", end="", flush=True)
        req = _req.Request(tarball_url, headers={"User-Agent": "synapse-upgrade/1.0"})
        try:
            with _req.urlopen(req, timeout=120) as resp, open(tar_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            print(f"\n  Error downloading release: {e}")
            return False
        print(" done.")

        print("  Extracting...", end="", flush=True)
        extract_dir = os.path.join(tmp, "src")
        os.makedirs(extract_dir)
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                tf.extractall(extract_dir)
        except Exception as e:
            print(f"\n  Error extracting archive: {e}")
            return False
        print(" done.")

        entries = os.listdir(extract_dir)
        if not entries:
            print("  Warning: tarball was empty — skipping file copy.")
            return False
        src_root = os.path.join(extract_dir, entries[0])

        print("  Applying update...")
        for item in os.listdir(src_root):
            if item.startswith(".env"):
                continue
            src = os.path.join(src_root, item)
            dst = os.path.join(str(ROOT_DIR), item)

            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for sub in os.listdir(src):
                    sub_rel = os.path.join(item, sub)
                    if any(sub_rel == s or sub_rel.startswith(s + os.sep) for s in SKIP):
                        continue
                    # Never overwrite log files
                    if sub.endswith(".log"):
                        continue
                    sub_src = os.path.join(src, sub)
                    sub_dst = os.path.join(dst, sub)
                    if os.path.isdir(sub_src):
                        if os.path.exists(sub_dst):
                            _rmtree(sub_dst)
                        shutil.copytree(sub_src, sub_dst)
                    else:
                        shutil.copy2(sub_src, sub_dst)
            else:
                shutil.copy2(src, dst)

    return True


def _upgrade_command():
    """Upgrade Synapse AI to the latest version.

    - pip-installed  → pip install --upgrade synapse-orch-ai
    - source / editable install → download latest GitHub release + rebuild venv + rebuild frontend
    """
    print("\n=== Synapse AI -- Upgrade ===")

    # Detect whether we're running from a pip-installed wheel or a source tree.
    # When installed from PyPI, ROOT_DIR is inside site-packages/.
    _is_pip_install = any(p in ("site-packages", "dist-packages") for p in ROOT_DIR.parts)

    if _is_pip_install:
        # ── pip-installed path ────────────────────────────────────────────────
        print("\nUpgrading via pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "synapse-orch-ai"],
            # Show live output so the user can see progress
            stdout=None, stderr=None,
        )
        if result.returncode != 0:
            print("\n  Upgrade failed.")
            print("  Try manually: pip install --upgrade synapse-orch-ai")
            sys.exit(result.returncode)

        print("\n=== Upgrade complete! ===")
        print("Run 'synapse start' to launch the updated Synapse.")
        return

    # ── source / editable install path ───────────────────────────────────────

    # Ensure the internal token exists before ANYTHING else so that any npm
    # build in this upgrade (or the next) picks it up from the environment.
    # This must run before the release download so the token is set even when
    # the downloaded cli.py replaces us on disk mid-upgrade.
    _ensure_internal_token()
    _ensure_jwt_secret()

    # When we successfully apply a new release we re-exec this script using the
    # freshly-downloaded cli.py so that any changes to the upgrade logic itself
    # take effect immediately — no "run synapse upgrade twice" required.
    # The re-exec'd process receives this flag and skips straight to the rebuild.
    _skip_download = os.environ.get("SYNAPSE_UPGRADE_SKIP_DOWNLOAD") == "1"

    if not _skip_download:
        # 1. Stop running services first
        print("\nStopping running services...")
        _stop_command()

        # 2. Download latest release from GitHub
        print("\n==> Checking for latest release...")
        current_ver = _get_current_version()
        latest_tag, tarball_url = _get_latest_github_release()

        if latest_tag and tarball_url:
            if _parse_version(latest_tag) > _parse_version(current_ver):
                print(f"  New version available: {latest_tag} (current: {current_ver})")
                if _download_and_apply_release(tarball_url):
                    print(f"  Source updated to {latest_tag}.")
                    # Re-exec with the newly-downloaded cli.py so the rest of
                    # the upgrade (venv rebuild, npm build, etc.) runs with the
                    # new code.  The environment — including SYNAPSE_INTERNAL_TOKEN
                    # set above — is inherited by the child process.
                    new_cli = ROOT_DIR / "synapse" / "cli.py"
                    if new_cli.exists():
                        print("  Restarting upgrade with updated CLI...")
                        env = os.environ.copy()
                        env["SYNAPSE_UPGRADE_SKIP_DOWNLOAD"] = "1"
                        result = subprocess.run(
                            [sys.executable, str(new_cli), "upgrade"],
                            env=env,
                        )
                        sys.exit(result.returncode)
                else:
                    print("  Warning: file apply failed — continuing with existing code.")
            else:
                print(f"  Already at latest version ({current_ver}).")
        else:
            print("  Warning: could not reach GitHub releases — continuing with existing code.")
    else:
        print("\n(Continuing upgrade with updated CLI...)")

    # 3. Rebuild Python venv
    print("\n==> Rebuilding backend virtual environment...")
    venv_dir = BACKEND_DIR / "venv"
    python_exe = venv_dir / ("Scripts/python.exe" if IS_WIN else "bin/python")

    def _venv_is_healthy() -> bool:
        """Return True if the venv exists and its Python is actually usable."""
        if not python_exe.exists():
            return False
        result = subprocess.run([str(python_exe), "--version"],
                                capture_output=True, timeout=10)
        return result.returncode == 0

    if _venv_is_healthy():
        # Upgrade in place — avoids deleting locked/protected files on Windows
        # and is faster on all platforms. Safe because requirements.txt uses
        # loose pinning (bare package names), so --upgrade gets the same result
        # as a clean install.
        print("  Existing virtual environment found, upgrading packages in place...")
        pip_extra = ["--upgrade"]
    else:
        # Venv is missing or broken — delete (best-effort) and recreate.
        if venv_dir.exists():
            print("  Removing old virtual environment...")
            _rmtree(venv_dir)
        print("  Creating virtual environment...")
        subprocess.check_call([_system_python(), "-m", "venv", str(venv_dir)])
        pip_extra = []

    # Upgrade pip
    print("  Upgrading pip...")
    subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"],
                   capture_output=True)

    # Install requirements
    req_txt = BACKEND_DIR / "requirements.txt"
    if req_txt.exists():
        print("  Installing backend requirements...")
        subprocess.check_call([str(python_exe), "-m", "pip", "install",
                               *pip_extra, "-r", str(req_txt)])
    else:
        print(f"  Warning: {req_txt} not found -- skipping requirements.")

    # Always install coding-agent deps (cocoindex, psycopg, numpy).
    # These are small and needed as soon as the user enables "Code Indexing"
    # in the UI — we don't want them to have to run upgrade again just because
    # they toggled a setting after the initial install.
    coding_req = BACKEND_DIR / "requirements-coding.txt"
    if coding_req.exists():
        print("  Installing coding-agent requirements (cocoindex, psycopg)...")
        subprocess.check_call([str(python_exe), "-m", "pip", "install",
                               *pip_extra, "-r", str(coding_req)])
    else:
        print(f"  Warning: {coding_req} not found -- skipping.")

    # Messaging deps are heavier — only install when the user has opted in.
    import json as _json
    settings_file = DATA_DIR / "settings.json"
    _settings: dict = {}
    if settings_file.exists():
        try:
            _settings = _json.loads(settings_file.read_text())
        except Exception:
            pass

    if _settings.get("messaging_enabled", False):
        messaging_req = BACKEND_DIR / "requirements-messaging.txt"
        if messaging_req.exists():
            print("  Installing messaging requirements...")
            subprocess.check_call([str(python_exe), "-m", "pip", "install",
                                   *pip_extra, "-r", str(messaging_req)])
        else:
            print(f"  Warning: {messaging_req} not found -- skipping.")

    # Register synapse package via .pth file — no build hook, no bash required
    print("  Registering Synapse package...")
    _register_synapse_pth(str(venv_dir), str(ROOT_DIR))
    print("  Backend rebuild complete.")

    # 4. Rebuild frontend
    # Ensure the internal token is in os.environ so the build subprocess
    # inherits it — Next.js bundles process.env.SYNAPSE_INTERNAL_TOKEN into
    # the Edge Middleware at build time.
    _ensure_internal_token()

    print("\n==> Rebuilding frontend (npm install + npm run build)...")
    npm = _npm_command()

    # Remove node_modules so we get a clean install
    node_modules = FRONTEND_DIR / "node_modules"
    if node_modules.exists():
        print("  Removing old node_modules...")
        _rmtree(node_modules)

    print("  Running npm install...")
    subprocess.check_call([npm, "install"], cwd=str(FRONTEND_DIR))

    print("  Building frontend...")
    subprocess.check_call([npm, "run", "build"], cwd=str(FRONTEND_DIR))

    # Sync new standalone build into _BUNDLED_FRONTEND if this is a traditionally-installed instance.
    if _BUNDLED_FRONTEND.exists():
        print("  Updating bundled frontend (synapse/_frontend/)...")
        if _sync_bundled_frontend(verbose=True):
            print("  Bundled frontend updated.")

    print("  Frontend rebuild complete.")

    # 5. Re-apply execute permissions on the synapse bin script
    # (git pull can reset the execute bit, especially on WSL/Linux)
    _fix_bin_permissions()

    print("\n=== Upgrade complete! ===")
    print("Run 'synapse start' to launch the updated Synapse.")
    if not IS_WIN:
        bin_dir = str(ROOT_DIR / "bin")
        print(f"\nIf 'synapse' is not found in your terminal, run:")
        print(f"  export PATH=\"{bin_dir}:$PATH\"")
        print("(Already saved to ~/.bashrc / ~/.zshrc for new terminals.)")


def _get_synapse_install_dir() -> Path | None:
    """Return the platform-specific SynapseAI install directory written by setup.sh / setup.ps1."""
    if IS_WIN:
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            return Path(local_app_data) / "Programs" / "SynapseAI"
        return None
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SynapseAI"
    else:  # Linux
        return Path.home() / ".local" / "share" / "SynapseAI"


def _uninstall_command(keep_data: bool = False):
    """Stop services and remove all Synapse AI files."""
    print("\n=== Synapse AI -- Uninstall ===")
    print()

    # Resolve the platform install directory up-front so later steps can reference it.
    platform_install = _get_synapse_install_dir()

    # Confirm
    try:
        answer = input(
            "This will PERMANENTLY remove Synapse AI and all its files.\n"
            "Type 'yes' to confirm: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return

    if answer != "yes":
        print("Aborted.")
        return

    # 1. Stop running services
    print("\nStopping running services...")
    try:
        _stop_command()
    except Exception as e:
        print(f"  Warning: could not stop services cleanly: {e}")

    # 2. Remove startup entries (systemd / LaunchAgent / Registry)
    print("Removing startup registration...")
    _platform = sys.platform
    if IS_WIN:
        try:
            import winreg  # type: ignore
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE,
            )
            try:
                winreg.DeleteValue(key, "SynapseAI")
                print("  Removed Windows startup entry.")
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
        except Exception:
            pass
    elif _platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.synapse-ai.server.plist"
        if plist.exists():
            try:
                subprocess.run(["launchctl", "unload", str(plist)], check=False, capture_output=True)
                plist.unlink()
                print("  Removed macOS LaunchAgent.")
            except Exception:
                pass
    else:  # Linux
        service = Path.home() / ".config" / "systemd" / "user" / "synapse-ai.service"
        if service.exists():
            try:
                subprocess.run(["systemctl", "--user", "disable", "synapse-ai.service"],
                               check=False, capture_output=True)
                service.unlink()
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
                print("  Removed systemd user service.")
            except Exception:
                pass

    # 3. Remove data directory (optional)
    if not keep_data and DATA_DIR.exists():
        try:
            _rmtree(DATA_DIR)
            print(f"  Removed data directory: {DATA_DIR}")
        except Exception as e:
            print(f"  Warning: could not remove data dir {DATA_DIR}: {e}")
    # Also remove ~/.synapse parent directory (config files, etc.)
    if not keep_data:
        synapse_home = Path.home() / ".synapse"
        if synapse_home.exists():
            try:
                _rmtree(synapse_home)
                print(f"  Removed Synapse home: {synapse_home}")
            except Exception as e:
                print(f"  Warning: could not fully remove {synapse_home}: {e}")

    # 4. Remove the installation directory/directories
    # Collect unique dirs: the running ROOT_DIR plus the platform standard install location
    # (e.g. ~/.local/share/SynapseAI on Linux, %LOCALAPPDATA%\Programs\SynapseAI on Windows).
    # Skip ROOT_DIR when pip-installed: it resolves to site-packages/, which must not be deleted.
    _is_pip_install = any(p in ("site-packages", "dist-packages") for p in ROOT_DIR.parts)
    if _is_pip_install:
        _dirs_to_remove: list[Path] = []
    else:
        _dirs_to_remove = [ROOT_DIR]
    if platform_install and platform_install.resolve() != ROOT_DIR.resolve():
        _dirs_to_remove.append(platform_install)

    for _install_dir in _dirs_to_remove:
        if not _install_dir.exists():
            continue
        print(f"\nRemoving installation directory: {_install_dir}")
        try:
            # Remove large subdirectories first to avoid partial-removal hangs
            for _big in (
                _install_dir / "backend" / "venv",
                _install_dir / "frontend" / "node_modules",
            ):
                if _big.exists():
                    _rmtree(_big)
            _rmtree(_install_dir)
            print("  Removed.")
        except Exception as e:
            print(f"  Warning: could not fully remove {_install_dir}: {e}")
            print("  You may need to delete it manually.")

    # 5. Remove the pip-installed `synapse` console script
    print("\nUninstalling Python package...")
    _pip_names = ["synapse-orch-ai", "synapse-ai", "synapse"]
    try:
        _removed = False
        for _pkg in _pip_names:
            _r = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", _pkg],
                capture_output=True, text=True,
            )
            if _r.returncode == 0:
                print(f"  Removed pip package {_pkg}.")
                _removed = True
                break
        if not _removed:
            print("  Package not found in pip (may already be removed).")
    except Exception as e:
        print(f"  Warning: pip uninstall failed: {e}")

    # Fallback: remove the synapse executable directly if it still exists on PATH
    synapse_exe = shutil.which("synapse")
    if synapse_exe:
        try:
            Path(synapse_exe).unlink(missing_ok=True)
            print(f"  Removed executable: {synapse_exe}")
        except PermissionError:
            print(f"  Warning: no permission to remove {synapse_exe} -- delete it manually.")
        except Exception as e:
            print(f"  Warning: could not remove {synapse_exe}: {e}")

    # Windows: also scrub leftover files from the Python Scripts directory
    if IS_WIN:
        scripts_dir = Path(sys.executable).parent / "Scripts"
        for name in ("synapse.exe", "synapse-script.py"):
            candidate = scripts_dir / name
            if candidate.exists():
                try:
                    candidate.unlink()
                    print(f"  Removed: {candidate}")
                except Exception as e:
                    print(f"  Warning: could not remove {candidate}: {e}")

    # 6. Clean PATH entries from shell rc files (Unix) / registry + PS profiles (Windows)
    # Build the set of bin-dir strings to purge (covers both ROOT_DIR and the platform
    # install dir written by setup.sh / setup.ps1).
    _bin_dirs_lower = {str(ROOT_DIR / "bin").lower(), str(ROOT_DIR).lower()}
    if platform_install:
        _bin_dirs_lower.add(str(platform_install / "bin").lower())
        _bin_dirs_lower.add(str(platform_install).lower())

    if IS_WIN:
        # --- Windows registry (user PATH) ---
        try:
            import winreg  # type: ignore
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Environment",
                0, winreg.KEY_READ | winreg.KEY_SET_VALUE,
            )
            try:
                path_val, reg_type = winreg.QueryValueEx(key, "PATH")
                parts = [p for p in path_val.split(";") if p]
                new_parts = [p for p in parts if p.lower() not in _bin_dirs_lower]
                if len(new_parts) != len(parts):
                    winreg.SetValueEx(key, "PATH", 0, reg_type, ";".join(new_parts))
                    print("  Cleaned PATH from Windows user environment registry.")
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
        except Exception:
            pass

        # --- Windows PowerShell profiles (written by setup.ps1) ---
        docs = Path.home() / "Documents"
        for ps_dir in ("PowerShell", "WindowsPowerShell"):
            ps_profile = docs / ps_dir / "profile.ps1"
            if not ps_profile.exists():
                continue
            try:
                lines = ps_profile.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                new_lines = [l for l in lines if "SynapseAI" not in l and "Synapse AI" not in l]
                if len(new_lines) != len(lines):
                    ps_profile.write_text("".join(new_lines), encoding="utf-8")
                    print(f"  Cleaned PATH entry from PowerShell profile: {ps_profile}")
            except Exception:
                pass
    else:
        for rc_file in (
            Path.home() / ".bashrc",
            Path.home() / ".zshrc",
            Path.home() / ".bash_profile",
            Path.home() / ".profile",
        ):
            if rc_file.exists():
                try:
                    lines = rc_file.read_text().splitlines(keepends=True)
                    new_lines = [l for l in lines
                                 if "SynapseAI" not in l and "Synapse AI" not in l
                                 and not any(d in l for d in _bin_dirs_lower)]
                    if len(new_lines) != len(lines):
                        rc_file.write_text("".join(new_lines))
                        print(f"  Cleaned PATH entry from {rc_file}")
                except Exception:
                    pass

    print("\n=== Synapse AI has been uninstalled. Goodbye! ===")


def _profile_command(action: str, output: str | None = None, limit: int = 20, duration: int = 30):
    backend_port = int(os.getenv("SYNAPSE_BACKEND_PORT", str(DEFAULT_BACKEND_PORT)))
    base_url = f"http://127.0.0.1:{backend_port}/api/profiling"

    def _api(method: str, path: str, params: str = "") -> dict | str | None:
        url = f"{base_url}{path}"
        if params:
            url += f"?{params}"
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get("Content-Type", "")
                body = resp.read()
                if "application/json" in ct:
                    import json
                    return json.loads(body)
                return body.decode()
        except urllib.error.HTTPError as e:
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
            Path(output).write_text(result)
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
        pid = _read_pidfile(BACKEND_PID_FILE)
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
    """Warn (non-fatally) if Python or Node.js versions are below the minimum required."""
    # ── Python ───────────────────────────────────────────────────────────────
    py = sys.version_info[:2]
    if py < MIN_PYTHON:
        print(
            f"Warning: Python {py[0]}.{py[1]} detected -- "
            f"Synapse requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+.\n"
            "  Please switch to Python 3.11 or newer (https://www.python.org/downloads/)\n"
            "  and reinstall: pip install synapse-ai"
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
            "  Install from https://nodejs.org/"
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
                    f"    cd {FRONTEND_DIR} && npm install && npm run build"
                )
        except Exception:
            pass  # version check failed; let downstream tools surface the error


def main():
    _warn_if_not_on_path()
    _warn_versions()
    parser = argparse.ArgumentParser(prog="synapse", description="Manage Synapse server (backend + frontend)")
    sub = parser.add_subparsers(dest="cmd")

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

    sub.add_parser("stop", help="Stop running backend and frontend (reads pidfiles)")
    sub.add_parser("status", help="Show status of backend and frontend")

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
    sub.add_parser("setup", help="Run interactive setup wizard to configure Synapse")

    # upgrade: pull code and rebuild everything
    sub.add_parser(
        "upgrade",
        help="Pull latest code, rebuild backend venv + requirements, rebuild frontend (npm install + npm run build)",
    )

    # uninstall: stop + wipe everything
    p_uninstall = sub.add_parser("uninstall", help="Stop services and remove all Synapse AI files")
    p_uninstall.add_argument(
        "--keep-data", action="store_true",
        help="Keep the data directory (~/.synapse) when uninstalling",
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

    # api-keys: manage external API keys
    p_apikeys = sub.add_parser("api-keys", help="Manage API keys for external /api/v1/ access")
    p_apikeys.add_argument(
        "action",
        choices=["generate", "list", "revoke"],
        help="generate: create a new key | list: show all keys | revoke: delete a key",
    )
    p_apikeys.add_argument("name_or_id", nargs="?", default="", help="Key name (generate) or key ID (revoke)")

    args = parser.parse_args()

    if args.cmd == "start" or args.cmd is None:
        # default to start when invoked without subcommand to preserve previous behaviour
        _start_command(
            detach=getattr(args, "detach", False),
            no_browser=getattr(args, "no_browser", False),
            backend_port=getattr(args, "backend_port", None),
            frontend_port=getattr(args, "frontend_port", None),
            profile=getattr(args, "profile", False),
        )
    elif args.cmd == "stop":
        _stop_command()
    elif args.cmd == "setup":
        try:
            from synapse import setup_wizard
            setup_wizard.run()
        except Exception as e:
            print(f"Failed to run setup wizard: {e}")
    elif args.cmd == "status":
        _status_command()
    elif args.cmd == "restart":
        _stop_command()
        _start_command(
            detach=getattr(args, "detach", False),
            backend_port=getattr(args, "backend_port", None),
            frontend_port=getattr(args, "frontend_port", None),
        )
    elif args.cmd == "upgrade":
        _upgrade_command()
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
