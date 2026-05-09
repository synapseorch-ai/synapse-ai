"""
Synapse AI -- Interactive Setup Wizard
Guides the user through configuration, installs dependencies, and starts both servers.
Uses only Python stdlib so it works before the venv exists.
"""
import json
import os
import signal
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(ROOT_DIR, "backend")
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
ENV_FILE = os.path.join(ROOT_DIR, ".env")

# ---------------------------------------------------------------------------
# Load .env BEFORE computing DATA_DIR so that setup.py and cli.py always
# agree on the same data directory (e.g. SYNAPSE_DATA_DIR=backend/data).
# ---------------------------------------------------------------------------
def _load_dotenv_early(path):
    """Minimal .env loader -- only sets vars not already in the environment."""
    if not os.path.exists(path):
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
                if _key and _key not in os.environ:
                    os.environ[_key] = _val
    except Exception:
        pass

_load_dotenv_early(ENV_FILE)

# Resolve DATA_DIR: relative paths are anchored to ROOT_DIR (same logic as cli.py)
_raw_data_dir = os.environ.get("SYNAPSE_DATA_DIR", os.path.join(BACKEND_DIR, "data"))
if not os.path.isabs(_raw_data_dir):
    DATA_DIR = os.path.normpath(os.path.join(ROOT_DIR, _raw_data_dir))
else:
    DATA_DIR = _raw_data_dir

EXAMPLES_DIR = os.path.join(BACKEND_DIR, "examples")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "credentials.json")

# Port defaults -- read from env first so an existing .env is respected
DEFAULT_BACKEND_PORT = int(os.environ.get("SYNAPSE_BACKEND_PORT", "8765"))
DEFAULT_FRONTEND_PORT = int(os.environ.get("SYNAPSE_FRONTEND_PORT", "3000"))

IS_WIN = sys.platform == "win32"
VENV_DIR = os.path.join(BACKEND_DIR, "venv")
PYTHON_EXE = os.path.join(VENV_DIR, "Scripts" if IS_WIN else "bin", "python" + (".exe" if IS_WIN else ""))
PIP_EXE    = os.path.join(VENV_DIR, "Scripts" if IS_WIN else "bin", "pip" + (".exe" if IS_WIN else ""))

# ---------------------------------------------------------------------------
# ANSI colours
# ---------------------------------------------------------------------------
class C:
    BOLD   = '\033[1m'
    BLUE   = '\033[94m'
    CYAN   = '\033[96m'
    GREEN  = '\033[92m'
    YELLOW = '\033[93m'
    RED    = '\033[91m'
    RESET  = '\033[0m'

def _c(color, text): return f"{color}{text}{C.RESET}"
def step(msg):    print(f"\n{C.BLUE}{C.BOLD}==> {msg}{C.RESET}")
def ok(msg):      print(f"{C.GREEN}[OK]  {msg}{C.RESET}")
def warn(msg):    print(f"{C.YELLOW}[!!]  {msg}{C.RESET}")
def err(msg):     print(f"{C.RED}[X]  {msg}{C.RESET}")
def info(msg):    print(f"   {msg}")

def _redact_url(url: str) -> str:
    """Return a copy of a connection URL with the password redacted."""
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        if parsed.password:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
            url = parsed._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return url

# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------
def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"   {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val if val else default

def ask_yn(prompt, default="n"):
    hint = "(Y/n)" if default.lower() == "y" else "(y/N)"
    val = ask(f"{prompt} {hint}", default).lower()
    return val in ("y", "yes")

def ask_choice(prompt, options):
    """Show numbered list and return the chosen item."""
    for i, opt in enumerate(options, 1):
        print(f"   {_c(C.CYAN, str(i))}.  {opt}")
    while True:
        raw = ask(prompt)
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        warn(f"Enter a number between 1 and {len(options)}.")

# ---------------------------------------------------------------------------
# OS Detection & Auto-Install Helpers
# ---------------------------------------------------------------------------
def get_os_type():
    """Get OS type: 'linux', 'darwin', 'windows'"""
    return sys.platform

def get_linux_distro():
    """Get Linux distribution type"""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("ID="):
                    return line.split("=")[1].strip().strip('"')
    except:
        pass
    return None

def install_npm():
    """Auto-install npm/Node.js if not found"""
    step("Installing Node.js and npm")
    
    os_type = get_os_type()
    
    if IS_WIN:
        info("Please download and install Node.js (v20.9.0 or higher) from https://nodejs.org/")
        info("Once installed, restart your terminal and re-run this setup script.")
        err("Node.js installation required.")
        sys.exit(1)
    elif os_type == "darwin":
        info("Installing Node.js via Homebrew...")
        try:
            subprocess.check_call(["brew", "install", "node"])
            ok("Node.js installed successfully.")
        except FileNotFoundError:
            err("Homebrew not found. Please install from https://brew.sh")
            sys.exit(1)
        except subprocess.CalledProcessError:
            err("Failed to install Node.js via Homebrew.")
            sys.exit(1)
    else:  # Linux
        distro = get_linux_distro()
        
        if distro in ("ubuntu", "debian"):
            info("Installing Node.js on Ubuntu/Debian via NodeSource...")
            try:
                # Use NodeSource setup script for latest LTS
                subprocess.check_call(["curl", "-fsSL", "https://deb.nodesource.com/setup_20.x", "-o", "nodesource_setup.sh"])
                subprocess.check_call(["sudo", "bash", "nodesource_setup.sh"])
                subprocess.check_call(["sudo", "apt-get", "install", "-y", "nodejs"])
                subprocess.check_call(["rm", "nodesource_setup.sh"])
                ok("Node.js installed successfully.")
            except Exception as e:
                warn(f"NodeSource installation failed: {e}. Trying default repos...")
                try:
                    subprocess.check_call(["sudo", "apt-get", "update"])
                    subprocess.check_call(["sudo", "apt-get", "install", "-y", "nodejs", "npm"])
                    ok("Node.js installed successfully.")
                except subprocess.CalledProcessError:
                    err("Failed to install Node.js.")
                    sys.exit(1)
        elif distro in ("fedora", "rhel", "centos"):
            info("Installing Node.js on Fedora/RHEL...")
            try:
                subprocess.check_call(["sudo", "dnf", "install", "-y", "nodejs", "npm"])
                ok("Node.js installed successfully.")
            except subprocess.CalledProcessError:
                err("Failed to install Node.js.")
                sys.exit(1)
        elif distro in ("arch", "manjaro"):
            info("Installing Node.js on Arch/Manjaro...")
            try:
                subprocess.check_call(["sudo", "pacman", "-S", "--noconfirm", "nodejs", "npm"])
                ok("Node.js installed successfully.")
            except subprocess.CalledProcessError:
                err("Failed to install Node.js.")
                sys.exit(1)
        else:
            warn(f"Unknown Linux distribution: {distro}")
            info("Please install Node.js manually from https://nodejs.org/")
            sys.exit(1)

def install_postgresql():
    """Auto-install PostgreSQL if not found"""
    step("Installing PostgreSQL")
    
    os_type = get_os_type()
    
    if IS_WIN:
        info("PostgreSQL is required for the Coding Agent on Windows.")
        info("1. Download the installer from: https://www.postgresql.org/download/windows/")
        info("2. Run the installer and follow the on-screen prompts.")
        info("3. IMPORTANT: Add the PostgreSQL bin directory to your System PATH:")
        info("   - Search for 'Edit the system environment variables' in the Start menu")
        info("   - Click 'Environment Variables', then find 'Path' under System variables")
        info("   - Click 'Edit' -> 'New', and add the bin path (e.g. C:\\Program Files\\PostgreSQL\\17\\bin)")
        info("4. Restart your terminal so the updated PATH takes effect.")
        info("5. Verify the installation by running: psql --version")
        info("   Make sure it prints a version number before continuing.")
        info("")
        warn("Please complete all steps above, then re-run this setup script.")
        err("PostgreSQL installation or PATH configuration is required.")
        sys.exit(1)
    elif os_type == "darwin":
        info("Installing PostgreSQL via Homebrew...")
        try:
            subprocess.check_call(["brew", "install", "postgresql@15"])
            subprocess.check_call(["brew", "services", "start", "postgresql@15"])
            ok("PostgreSQL installed and started.")
        except FileNotFoundError:
            err("Homebrew not found. Please install from https://brew.sh")
            sys.exit(1)
        except subprocess.CalledProcessError:
            err("Failed to install PostgreSQL.")
            sys.exit(1)
    else:  # Linux
        distro = get_linux_distro()
        
        if distro in ("ubuntu", "debian"):
            info("Installing PostgreSQL on Ubuntu/Debian...")
            try:
                subprocess.check_call(["sudo", "apt-get", "update"])
                subprocess.check_call(["sudo", "apt-get", "install", "-y", "postgresql", "postgresql-contrib"])
                subprocess.check_call(["sudo", "systemctl", "start", "postgresql"])
                ok("PostgreSQL installed and started.")
            except subprocess.CalledProcessError:
                err("Failed to install PostgreSQL.")
                sys.exit(1)
        elif distro in ("fedora", "rhel", "centos"):
            info("Installing PostgreSQL on Fedora/RHEL...")
            try:
                subprocess.check_call(["sudo", "dnf", "install", "-y", "postgresql-server", "postgresql-contrib"])
                subprocess.check_call(["sudo", "systemctl", "start", "postgresql"])
                ok("PostgreSQL installed and started.")
            except subprocess.CalledProcessError:
                err("Failed to install PostgreSQL.")
                sys.exit(1)
        elif distro in ("arch", "manjaro"):
            info("Installing PostgreSQL on Arch/Manjaro...")
            try:
                subprocess.check_call(["sudo", "pacman", "-S", "--noconfirm", "postgresql"])
                ok("PostgreSQL installed. Start with: sudo systemctl start postgresql")
            except subprocess.CalledProcessError:
                err("Failed to install PostgreSQL.")
                sys.exit(1)
        else:
            err(f"Unknown Linux distribution: {distro}")
            sys.exit(1)

def install_pgvector():
    """Install pgvector extension in PostgreSQL"""
    step("Installing pgvector Extension")
    
    os_type = get_os_type()
    
    if IS_WIN:
        warn("On Windows, please install pgvector manually or use WSL.")
        return False
    
    distro = get_linux_distro() if os_type != "darwin" else "darwin"
    
    try:
        if os_type == "darwin":
            subprocess.check_call(["brew", "install", "pgvector"])
        elif distro in ("ubuntu", "debian"):
            subprocess.check_call(["sudo", "apt-get", "install", "-y", "postgresql-contrib"])
            subprocess.check_call(["sudo", "apt-get", "install", "-y", "postgresql-15-pgvector"])
        elif distro in ("fedora", "rhel"):
            subprocess.check_call(["sudo", "dnf", "install", "-y", "pgvector"])
        elif distro in ("arch", "manjaro"):
            subprocess.check_call(["sudo", "pacman", "-S", "--noconfirm", "pgvector"])
        else:
            warn(f"pgvector installation not automated for {distro}. Please install manually.")
            return False
        ok("pgvector installed.")
        return True
    except subprocess.CalledProcessError:
        warn("pgvector installation had issues. You may need to install manually.")
        return False

def create_postgresql_db(db_user, db_password, db_name="synapse"):
    """Create a PostgreSQL database and return the connection URL"""
    step("Setting up PostgreSQL Database")
    
    try:
        # Try to create database and user using psql
        # First, get superuser password or use peer authentication
        info(f"Creating database '{db_name}' and user '{db_user}'...")
        
        # Create user if not exists
        create_user_sql = f"CREATE USER {db_user} PASSWORD '{db_password}';"
        create_db_sql = f"CREATE DATABASE {db_name} OWNER {db_user};"
        alter_priv_sql = f"ALTER ROLE {db_user} CREATEDB;"
        
        try:
            # Try with sudo -u postgres (Linux)
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c", alter_priv_sql],
                check=True, capture_output=True, timeout=10
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Try direct connection
            pass
        
        try:
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c", create_user_sql],
                check=False, capture_output=True, timeout=10
            )
        except:
            pass
        
        try:
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-c", create_db_sql],
                check=False, capture_output=True, timeout=10
            )
        except:
            pass
        
        # Try to create vector extension
        try:
            subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-d", db_name, "-c", "CREATE EXTENSION IF NOT EXISTS vector;"],
                check=False, capture_output=True, timeout=10
            )
            ok("Vector extension created.")
        except:
            warn("Could not create vector extension. You may need to do it manually.")
        
        url = f"postgresql+psycopg://{db_user}:{db_password}@localhost:5432/{db_name}"
        return url
    
    except Exception as e:
        err(f"Failed to setup database: {e}")
        return None

# ---------------------------------------------------------------------------
# System checks
# ---------------------------------------------------------------------------
def check_python():
    step("Checking Python version")
    v = sys.version_info
    if v < (3, 11):
        err(f"Python 3.11+ required. You have {v.major}.{v.minor}.{v.micro}")
        sys.exit(1)
    ok(f"Python {v.major}.{v.minor}.{v.micro}")

    # Check that the venv module is available for this Python installation.
    # On some Linux distros (Ubuntu/Debian) it ships as a separate package.
    try:
        import importlib.util
        if importlib.util.find_spec("venv") is None:
            raise ImportError
        ok("venv module available.")
    except ImportError:
        err(f"Python venv module not found for Python {v.major}.{v.minor}.")
        distro = get_linux_distro()
        if distro in ("ubuntu", "debian"):
            info(f"  Install it with:  sudo apt-get install python{v.major}.{v.minor}-venv")
        elif distro in ("fedora", "rhel", "centos"):
            info(f"  Install it with:  sudo dnf install python{v.major}-venv")
        else:
            info(f"  Install the python{v.major}.{v.minor}-venv package for your distribution.")
        sys.exit(1)

def _find_all_node_versions():
    """Return list of (version_tuple, bin_dir) for every discoverable Node install, newest first."""
    candidates = []
    seen = set()

    def _probe(node_path):
        if not node_path or not os.path.isfile(node_path):
            return
        real = os.path.realpath(node_path)
        if real in seen:
            return
        seen.add(real)
        try:
            r = subprocess.run([node_path, "--version"], capture_output=True, text=True, timeout=5)
            ver_str = r.stdout.strip().lstrip("v")
            ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
            candidates.append((ver_tuple, os.path.dirname(node_path)))
        except Exception:
            pass

    # nvm
    nvm_versions = os.path.join(
        os.path.expanduser(os.environ.get("NVM_DIR", "~/.nvm")), "versions", "node"
    )
    if os.path.isdir(nvm_versions):
        for entry in sorted(os.listdir(nvm_versions)):
            _probe(os.path.join(nvm_versions, entry, "bin", "node"))

    # fnm
    for fnm_root in [
        os.path.expanduser("~/.local/share/fnm/node-versions"),
        os.path.expanduser("~/.fnm/node-versions"),
    ]:
        if os.path.isdir(fnm_root):
            for entry in sorted(os.listdir(fnm_root)):
                _probe(os.path.join(fnm_root, entry, "installation", "bin", "node"))

    # common system paths
    for p in ["/usr/local/bin/node", "/usr/bin/node", "/opt/homebrew/bin/node"]:
        _probe(p)

    # which -a (Unix) -- catches anything else on PATH
    if not IS_WIN:
        try:
            r = subprocess.run(["which", "-a", "node"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                _probe(line.strip())
        except Exception:
            pass

    candidates.sort(reverse=True)
    return candidates


def _find_all_node_versions_win():
    """Windows-specific: probe all known Node.js install locations.
    Returns list of (version_tuple, node_exe_path, bin_dir) sorted newest-first."""
    candidates = []
    seen = set()

    def _probe(node_exe):
        if not node_exe or not os.path.isfile(node_exe):
            return
        real = os.path.realpath(node_exe).lower()
        if real in seen:
            return
        seen.add(real)
        try:
            r = subprocess.run([node_exe, "--version"], capture_output=True, text=True, timeout=5)
            ver_str = r.stdout.strip().lstrip("v")
            ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
            candidates.append((ver_tuple, node_exe, os.path.dirname(node_exe)))
        except Exception:
            pass

    pf   = os.environ.get("ProgramFiles",       r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)",  r"C:\Program Files (x86)")
    lad  = os.environ.get("LocalAppData",        "")
    appd = os.environ.get("APPDATA",             "")

    # Standard winget / MSI install directories
    standard_dirs = [
        os.path.join(pf,   "nodejs"),
        os.path.join(pf86, "nodejs"),
        os.path.join(lad,  "Programs", "nodejs"),
        os.path.join(lad,  "nodejs"),
    ]
    for d in standard_dirs:
        _probe(os.path.join(d, "node.exe"))

    # nvm-windows: %APPDATA%\nvm\<version>\node.exe
    nvm_root = os.path.join(appd, "nvm")
    if os.path.isdir(nvm_root):
        for entry in os.listdir(nvm_root):
            _probe(os.path.join(nvm_root, entry, "node.exe"))

    # fnm on Windows
    for fnm_root in [
        os.path.join(lad, "fnm", "node-versions"),
        os.path.join(lad, ".fnm", "node-versions"),
    ]:
        if os.path.isdir(fnm_root):
            for entry in os.listdir(fnm_root):
                _probe(os.path.join(fnm_root, entry, "installation", "node.exe"))

    # Also walk PATH entries -- catches a freshly refreshed PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        _probe(os.path.join(path_dir.strip(), "node.exe"))

    candidates.sort(reverse=True)
    return candidates  # (ver_tuple, node_exe, bin_dir)


def _find_node_exe_win():
    """Return (node_exe, bin_dir) for the best Node >= 20.9.0 on Windows, else (None, None)."""
    MIN = (20, 9, 0)
    for ver_tuple, node_exe, bin_dir in _find_all_node_versions_win():
        if ver_tuple >= MIN:
            return node_exe, bin_dir
    return None, None


def check_npm():
    if IS_WIN:
        # On Windows, shutil.which / os.environ["PATH"] may be stale after a
        # winget install in the same session.  Probe known install paths directly.
        step("Checking Node.js / npm (Windows)")
        node_exe, bin_dir = _find_node_exe_win()
        if node_exe:
            try:
                r = subprocess.run([node_exe, "--version"], capture_output=True, text=True, timeout=5)
                ver_str = r.stdout.strip().lstrip("v")
                ok(f"Node.js v{ver_str} found at {node_exe}")
                # UNCONDITIONALLY prepend the bin dir to PATH for this Python session so that
                # any subsequent `npm`/`npx` calls use this specific version, overriding any
                # older versions that might be present earlier in the PATH.
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                info(f"Prepended {bin_dir} to PATH for this session.")
                ok("npm ready")
                return
            except Exception as e:
                warn(f"Failed to invoke node at {node_exe}: {e}")
        # Nothing found -- show a clear error
        err("Node.js 20.9.0+ is required but was not found on this Windows system.")
        info("Searched: Program Files\\nodejs, LocalAppData\\Programs\\nodejs, nvm-windows, fnm, PATH.")
        info("Install the latest Node.js LTS from https://nodejs.org/ and re-run setup.")
        sys.exit(1)

    # ---- Non-Windows path (unchanged) ----
    if not shutil.which("npm"):
        warn("npm not found. Attempting to install Node.js and npm automatically...")
        install_npm()

    # Verify the *currently active* Node.js version.
    node_exe = shutil.which("node")
    if node_exe:
        try:
            result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
            version_str = result.stdout.strip().lstrip("v")  # e.g. "18.17.0"
            parts = [int(p) for p in version_str.split(".")[:3]]
            major = parts[0]
            minor = parts[1] if len(parts) > 1 else 0

            # Require >= 20.9.0
            if major < 20 or (major == 20 and minor < 9):
                warn(f"Active Node.js version is v{version_str} (< 20.9.0). Searching for a newer install...")
                all_versions = _find_all_node_versions()
                suitable = [(v, d) for v, d in all_versions if v[0] > 20 or (v[0] == 20 and v[1] >= 9)]
                if suitable:
                    best_ver, best_dir = suitable[0]
                    best_ver_str = ".".join(str(x) for x in best_ver)
                    ok(f"Found Node.js v{best_ver_str} at {best_dir}")
                    # Prepend the better node's bin dir so all subsequent subprocess
                    # calls (npm install, npm run build, ...) use the right version.
                    os.environ["PATH"] = best_dir + os.pathsep + os.environ.get("PATH", "")
                    ok(f"Switched to Node.js v{best_ver_str} for this setup session.")
                else:
                    err(f"Node.js 20.9.0+ required. Active version: v{version_str}, none found >= 20.9.0.")
                    info("Install the latest Node.js from https://nodejs.org/ or via nvm/fnm, then re-run setup.")
                    sys.exit(1)
            else:
                ok(f"Node.js v{version_str}")
        except Exception as e:
            warn(f"Could not verify Node.js version: {e}")
    else:
        warn("node executable not found -- npm may not work correctly.")

    ok("npm found")


# ---------------------------------------------------------------------------
# Check / install uv + uvx
# ---------------------------------------------------------------------------
def _install_uv_unix():
    """Install uv via the official installer (Linux / macOS)."""
    try:
        curl = shutil.which("curl")
        wget = shutil.which("wget")
        if curl:
            subprocess.check_call(
                f'{curl} -LsSf https://astral.sh/uv/install.sh | sh',
                shell=True,
            )
        elif wget:
            subprocess.check_call(
                f'{wget} -qO- https://astral.sh/uv/install.sh | sh',
                shell=True,
            )
        else:
            # Fallback to pip install
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "uv"])
        # Extend PATH so uv is findable in this process
        for extra in (
            os.path.join(os.path.expanduser("~"), ".local", "bin"),
            os.path.join(os.path.expanduser("~"), ".cargo", "bin"),
        ):
            if extra not in os.environ.get("PATH", ""):
                os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")
        return True
    except Exception as e:
        warn(f"uv auto-install failed: {e}")
        return False


def check_uvx():
    """Ensure uv (and therefore uvx) is available.  Auto-installs if missing."""
    step("Checking uv / uvx")

    # Extend PATH to common install locations before the first check
    for extra in (
        os.path.join(os.path.expanduser("~"), ".local", "bin"),
        os.path.join(os.path.expanduser("~"), ".cargo", "bin"),
    ):
        if extra not in os.environ.get("PATH", ""):
            os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")

    if shutil.which("uv"):
        try:
            r = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
            ver = r.stdout.strip()
            ok(f"{ver} found (uvx available)")
            return
        except Exception:
            pass

    warn("uv/uvx not found. Attempting to install...")
    if IS_WIN:
        # Try pip install on Windows
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--user", "uv"])
            # Add user Scripts to PATH
            try:
                import site as _site
                scripts = os.path.join(_site.getusersitepackages(), "..", "Scripts")
                scripts = os.path.normpath(scripts)
                if os.path.isdir(scripts) and scripts not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = scripts + os.pathsep + os.environ.get("PATH", "")
            except Exception:
                pass
        except Exception as e:
            warn(f"pip install uv failed: {e}")
    else:
        _install_uv_unix()

    if shutil.which("uv"):
        try:
            r = subprocess.run(["uv", "--version"], capture_output=True, text=True, timeout=5)
            ok(f"{r.stdout.strip()} installed and available.")
        except Exception:
            ok("uv installed.")
    else:
        warn("uv/uvx not available -- install from https://astral.sh/uv")
        info("  Some CLI tools that rely on uvx will not work until uv is installed.")


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------
DEFAULT_MODEL_PRICING = {
    "gpt-4o": { "provider": "openai", "input_per_1m": 2.5, "output_per_1m": 10 },
    "gpt-4o-mini": { "provider": "openai", "input_per_1m": 0.15, "output_per_1m": 0.6 },
    "gpt-4.1": { "provider": "openai", "input_per_1m": 2, "output_per_1m": 8 },
    "gpt-4.1-mini": { "provider": "openai", "input_per_1m": 0.4, "output_per_1m": 1.6 },
    "gpt-4.1-nano": { "provider": "openai", "input_per_1m": 0.1, "output_per_1m": 0.4 },
    "claude-sonnet-4-20250514": { "provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15 },
    "claude-opus-4-20250514": { "provider": "anthropic", "input_per_1m": 15, "output_per_1m": 75 },
    "claude-3-5-haiku-20241022": { "provider": "anthropic", "input_per_1m": 0.8, "output_per_1m": 4 },
    "gemini-2.5-pro": { "provider": "gemini", "input_per_1m": 1.25, "output_per_1m": 10 },
    "gemini-2.5-flash": { "provider": "gemini", "input_per_1m": 0.3, "output_per_1m": 2.5 },
    "grok-3": { "provider": "grok", "input_per_1m": 3, "output_per_1m": 15 },
    "grok-3-mini": { "provider": "grok", "input_per_1m": 0.3, "output_per_1m": 0.5 },
    "deepseek-chat": { "provider": "deepseek", "input_per_1m": 0.27, "output_per_1m": 1.1 },
    "deepseek-reasoner": { "provider": "deepseek", "input_per_1m": 0.55, "output_per_1m": 2.19 },
    "gemini-3.1-pro-preview": { "provider": "gemini", "input_per_1m": 2, "output_per_1m": 12 },
    "gemini-3-flash-preview": { "provider": "gemini", "input_per_1m": 0.5, "output_per_1m": 3 },
    "gemini-3.1-flash-lite-preview": { "provider": "gemini", "input_per_1m": 0.125, "output_per_1m": 0.75 },
    "gemini-2.5-flash-lite": { "provider": "gemini", "input_per_1m": 0.1, "output_per_1m": 0.4 },
    "claude-sonnet-4-5-20250929": { "provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15 },
    "claude-sonnet-4-6": { "provider": "anthropic", "input_per_1m": 3, "output_per_1m": 15 },
    "claude-opus-4-5-20251101": { "provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25 },
    "claude-opus-4-6": { "provider": "anthropic", "input_per_1m": 5, "output_per_1m": 25 }
}

DEFAULT_SETTINGS = {
    "agent_name": "Synapse",
    "model": "",
    "mode": "cloud",
    "openai_key": "",
    "anthropic_key": "",
    "gemini_key": "",
    "deepseek_key": "",
    "xai_key": "",
    "openai_compatible_key": "",
    "openai_compatible_base_url": "",
    "openai_compatible_models": "",
    "local_compatible_base_url": "",
    "local_compatible_key": "",
    "local_compatible_models": "",
    "openai_compatible_embed_models": "",
    "local_compatible_embed_models": "",
    "google_maps_api_key": "",
    "bedrock_api_key": "",
    "bedrock_inference_profile": "",
    "embedding_model": "",
    "aws_access_key_id": "",
    "aws_secret_access_key": "",
    "aws_session_token": "",
    "aws_region": "us-east-1",
    "sql_connection_string": "",
    "ollama_base_url": "",
    "n8n_url": "http://localhost:5678",
    "n8n_api_key": "",
    "n8n_table_id": "",
    "global_config": {},
    "vault_enabled": True,
    "vault_threshold": 100000,
    "allow_db_write": False,
    "coding_agent_enabled": False,
    "report_agent_enabled": False,
    "browser_automation_enabled": True,
    "playwright_browsers_path": "",
    "messaging_enabled": True,
    "embed_code": False,
}

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **saved}
    except Exception:
        return dict(DEFAULT_SETTINGS)

def save_settings(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    # Stamp installation date on first save (fresh install detection for in-app banner)
    if "installed_at" not in cfg:
        import datetime
        cfg["installed_at"] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f, indent=4)
        
    # Always save model pricing on setup in data folder
    src_pricing = os.path.join(BACKEND_DIR, "data", "model_pricing.json")
    dst_pricing = os.path.join(DATA_DIR, "model_pricing.json")
    if os.path.exists(src_pricing):
        try:
            if os.path.abspath(src_pricing) != os.path.abspath(dst_pricing):
                shutil.copy2(src_pricing, dst_pricing)
        except Exception:
            pass
    elif not os.path.exists(dst_pricing):
        try:
            with open(dst_pricing, "w") as f:
                json.dump(DEFAULT_MODEL_PRICING, f, indent=4)
        except Exception:
            pass

    # Persist the data directory path into .env so cli.py and synapse start
    # always find settings in the same location, even with relative paths.
    _rel = os.path.relpath(DATA_DIR, ROOT_DIR)
    _update_env_file("SYNAPSE_DATA_DIR", _rel)

# ---------------------------------------------------------------------------
# Q1 -- Coding Agent
# ---------------------------------------------------------------------------
def ask_coding_agent(cfg):
    step("Coding Agent")
    info("Enables code-aware agents that can read, search, and understand your codebase.")
    enabled = ask_yn("Enable the Coding Agent?")
    cfg["coding_agent_enabled"] = enabled
    if not enabled:
        ok("Coding Agent disabled.")


def ask_embed_code(cfg):
    step("Code Repository Indexing (PostgreSQL + pgvector)")
    info("Enables semantic search across your code repositories using vector embeddings.")

    psql_found = shutil.which("psql") is not None
    if not psql_found:
        warn("PostgreSQL (psql) not found on your system.")
        info("Code repository indexing will be disabled.")
        info("You can enable it later in Settings → General after installing PostgreSQL.")
        cfg["embed_code"] = False
        return

    ok("PostgreSQL found.")
    enabled = ask_yn("Do you want to enable code repository indexing?", default="n")
    cfg["embed_code"] = enabled

    if not enabled:
        ok("Code indexing disabled -- skipping PostgreSQL setup.")
        return

    # Check if PostgreSQL was pre-installed or needs setup
    if ask_yn("Is this a fresh PostgreSQL install (use default credentials)?", default="n"):
        db_user = "postgres"
        db_password = ""
        db_name = "synapse"
        ok("Using default PostgreSQL credentials (postgres user, peer authentication).")
    else:
        info("Configuring PostgreSQL database for code indexing...")
        db_user = ask("PostgreSQL username", default="postgres")
        db_password = ask("PostgreSQL password", default="")
        db_name = ask("Database name", default="synapse")

    # Try to install pgvector
    install_pgvector()

    # Create database and get URL
    db_url = create_postgresql_db(db_user, db_password, db_name)

    if db_url:
        cfg["sql_connection_string"] = db_url
        ok(f"Database URL: {_redact_url(db_url)}")

        # Test connection
        info("Testing PostgreSQL connection...")
        try:
            import psycopg2  # type: ignore
            try:
                conn = psycopg2.connect(db_url, connect_timeout=5)
                conn.close()
                ok("PostgreSQL connection successful!")
                return
            except Exception as e:
                warn(f"Connection test failed: {e}")
                if ask_yn("Save URL anyway?", default="y"):
                    ok("Saved URL (verify before starting server)")
                    return
        except ImportError:
            info("(psycopg2 will be installed with backend dependencies)")
            ok(f"Database URL saved: {_redact_url(db_url)}")
            return
    else:
        warn("Could not auto-create database. Please set it up manually.")
        url = ask("PostgreSQL connection URL",
                  default="postgresql://postgres:@localhost:5432/synapse")
        if url.startswith("postgresql"):
            cfg["sql_connection_string"] = url
            ok(f"Saved: {_redact_url(url)}")
        else:
            err("Invalid URL format.")
            sys.exit(1)


# ---------------------------------------------------------------------------
# Browser Automation -- install playwright automatically, no prompt
# ---------------------------------------------------------------------------
def setup_browser_automation(cfg):
    cfg["browser_automation_enabled"] = True

    system = platform.system()
    user_home = os.path.expanduser("~")
    if system == "Windows":
        default_pw_path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.join(user_home, "AppData", "Local")), "ms-playwright")
    elif system == "Darwin":
        default_pw_path = os.path.join(user_home, "Library", "Caches", "ms-playwright")
    else:  # Linux
        default_pw_path = os.path.join(user_home, ".cache", "ms-playwright")

    if os.path.exists(default_pw_path) and os.path.isdir(default_pw_path) and os.listdir(default_pw_path):
        ok("Playwright browsers found.")
        cfg["playwright_browsers_path"] = default_pw_path
    else:
        info("Playwright browsers not found. Installing... (this may take a minute)")
        try:
            if IS_WIN:
                npx_exe, bin_dir = _find_node_exe_win()
                npx_cmd = os.path.join(os.path.dirname(npx_exe) if npx_exe else "", "npx.cmd")
                if not os.path.isfile(npx_cmd):
                    npx_cmd = shutil.which("npx.cmd") or shutil.which("npx") or "npx"
                subprocess.check_call([npx_cmd, "-y", "playwright", "install", "chromium"])
            else:
                subprocess.check_call(["npx", "-y", "playwright", "install", "chromium"])
            ok("Playwright installed successfully.")
            cfg["playwright_browsers_path"] = default_pw_path
        except subprocess.CalledProcessError as e:
            warn(f"Failed to install Playwright browsers automatically: {e}")
            info("You can install it manually by running:")
            info("  npx -y playwright install chromium")
            cfg["playwright_browsers_path"] = default_pw_path


# ---------------------------------------------------------------------------
# Messaging App -- enabled by default, no prompt
# ---------------------------------------------------------------------------
def setup_messaging_app(cfg):
    cfg["messaging_enabled"] = True
    ok("Messaging App support enabled.")



GOOGLE_APIS = [
    ("Gmail",    "gmail.googleapis.com"),
    ("Drive",    "drive.googleapis.com"),
    ("Calendar", "calendar-json.googleapis.com"),
    ("Docs",     "docs.googleapis.com"),
    ("Sheets",   "sheets.googleapis.com"),
    ("Slides",   "slides.googleapis.com"),
    ("Forms",    "forms.googleapis.com"),
    ("Tasks",    "tasks.googleapis.com"),
    ("Contacts", "people.googleapis.com"),
]

def _gcloud_enable_apis(project_id):
    """Enable the three Google Workspace APIs via gcloud."""
    api_ids = ",".join(api for _, api in GOOGLE_APIS)
    try:
        subprocess.check_call(
            ["gcloud", "services", "enable"] + [api for _, api in GOOGLE_APIS]
            + ["--project", project_id],
            timeout=60
        )
        ok("APIs enabled: Gmail, Drive, Calendar, Docs, Sheets, Slides, Forms, Tasks, Contacts.")
        return True
    except subprocess.CalledProcessError as e:
        warn(f"gcloud services enable failed: {e}")
        return False


def ask_google_workspace(cfg):
    """Optional step: Set up Google Workspace OAuth credentials."""
    step("Google Workspace Integration")
    info("Powers Gmail, Drive, Calendar, Docs, Tasks, and more in Synapse.")

    # Get backend port from config (or default)
    backend_port = cfg.get("backend_port", DEFAULT_BACKEND_PORT)

    # Skip if credentials already exist
    if os.path.exists(CREDENTIALS_FILE):
        ok(f"credentials.json already exists at {CREDENTIALS_FILE} -- skipping.")
        return

    if not ask_yn("Configure Google Workspace now?", default="n"):
        ok("Skipped -- you can configure this later in Settings -> Integrations.")
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    has_gcloud = shutil.which("gcloud") is not None

    if has_gcloud:
        info("gcloud CLI detected -- using it to streamline setup.")
        step("Step 1/3 -- Authenticate with Google")
        info("Running: gcloud auth login")
        try:
            subprocess.check_call(["gcloud", "auth", "login", "--update-adc"])
            ok("Authenticated with Google.")
        except subprocess.CalledProcessError:
            warn("gcloud auth login failed. Continuing to manual step.")

        # List projects
        step("Step 2/3 -- Select or create a Google Cloud Project")
        try:
            result = subprocess.run(
                ["gcloud", "projects", "list", "--format=value(projectId,name)"],
                capture_output=True, text=True, timeout=15
            )
            lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
            if lines:
                info("Your projects:")
                for i, line in enumerate(lines, 1):
                    parts = line.split()
                    pid = parts[0] if parts else line
                    name = " ".join(parts[1:]) if len(parts) > 1 else ""
                    print(f"   {_c(C.CYAN, str(i))}.  {pid}  {_c(C.YELLOW, name)}")
                raw = ask("Enter project number or type a project ID")
                if raw.isdigit() and 1 <= int(raw) <= len(lines):
                    project_id = lines[int(raw)-1].split()[0]
                else:
                    project_id = raw.strip()
            else:
                project_id = ask("Enter your Google Cloud project ID")
        except Exception:
            project_id = ask("Enter your Google Cloud project ID")

        if project_id:
            ok(f"Using project: {project_id}")
            step("Step 2b -- Enabling Gmail, Drive, Calendar and other APIs")
            _gcloud_enable_apis(project_id)
        else:
            warn("No project selected -- skipping API enable.")
            project_id = None

        # Deep link for OAuth client creation
        step("Step 3/3 -- Create OAuth 2.0 Client ID (requires browser)")
        console_url = (
            f"https://console.cloud.google.com/apis/credentials/oauthclient?project={project_id}"
            if project_id else
            "https://console.cloud.google.com/apis/credentials"
        )
        info("The gcloud CLI cannot create OAuth Desktop App clients automatically.")
        info(f"Open this link to create one (pre-filled to your project):")
        print(f"\n   {_c(C.CYAN, console_url)}\n")
        info("Instructions:")
        info("  1. Choose 'OAuth client ID'")
        info("  2. Set application type to 'Web application'")
        info(f"  3. Set 'Authorized redirect URIs' to: http://localhost:{backend_port}/auth/callback")
        info("  4. Make sure the OAuth consent screen has all required scopes configured")
        info("  5. Click Create, download the JSON file, open it, copy all its content, and paste it below")
    else:
        # No gcloud -- full manual flow
        info("gcloud CLI not found -- using manual setup.")
        info("Use this link to create OAuth credentials:")
        print(f"\n   {_c(C.CYAN, 'https://console.cloud.google.com/apis/credentials')}\n")
        info("  1. Create a project (or select an existing one)")
        print(f"   Enable APIs: {_c(C.CYAN, 'https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com,drive.googleapis.com,calendar-json.googleapis.com,docs.googleapis.com,sheets.googleapis.com,slides.googleapis.com,forms.googleapis.com,tasks.googleapis.com,people.googleapis.com')}")
        info("  2. Configure the OAuth consent screen and add these scopes:")
        info("     openid, userinfo.email, userinfo.profile,")
        info("     gmail.modify, gmail.send, gmail.settings.basic,")
        info("     drive, calendar,")
        info("     documents, spreadsheets, presentations,")
        info("     forms.body, forms.body.readonly, forms.responses.readonly,")
        info("     tasks, contacts")
        info("  3. Create OAuth Client ID directly at:")
        print(f"     {_c(C.CYAN, 'https://console.cloud.google.com/auth/clients/create')}")
        info("     Choose 'Web application' as the application type.")
        info(f"  4. Set 'Authorized redirect URIs' to: http://localhost:{backend_port}/auth/callback")
        info("  5. Download the JSON file, open it, copy all its content, and paste it below.")

    # Paste area
    print()
    info("Paste the downloaded credentials JSON here (multi-line OK).")
    info("When done, press Enter twice (blank line) to save and continue:")
    lines = []
    try:
        while True:
            line = input()
            if line == "" and lines:
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        print()
        warn("No credentials pasted -- skipping Google Workspace setup.")
        return

    raw_json = "\n".join(lines).strip()
    if not raw_json:
        warn("Empty input -- skipping.")
        return

    try:
        parsed = json.loads(raw_json)
        with open(CREDENTIALS_FILE, "w") as f:
            json.dump(parsed, f, indent=4)
        ok(f"credentials.json saved to {CREDENTIALS_FILE}")
        info("After Synapse starts, go to Settings -> Integrations -> 'Connect Google Account' to complete OAuth.")
    except json.JSONDecodeError as e:
        err(f"Invalid JSON: {e}")
        warn("credentials.json was NOT saved. Configure via Settings -> Integrations later.")


# ---------------------------------------------------------------------------
# Q3 -- Agent Name
# ---------------------------------------------------------------------------
def ask_agent_name(cfg):
    step("Agent Name")
    name = ask("Enter a name for your AI Setup", default=cfg.get("agent_name") or "Synapse")
    cfg["agent_name"] = name or "Synapse"
    ok(f"Agent name set to: {cfg['agent_name']}")

# ---------------------------------------------------------------------------
# Q4 -- LLM Provider / Model
# ---------------------------------------------------------------------------
def _fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())

def _fetch_ollama_models(base_url="http://127.0.0.1:11434"):
    """Returns list of installed Ollama model names via HTTP API, or [] on failure."""
    try:
        data = _fetch_json(f"{base_url}/api/tags")
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []

def _detect_claude_cli():
    """Returns Claude CLI model list if 'claude' binary is found, else []."""
    if not shutil.which("claude"):
        return []
    return [
        "cli.claude.claude-sonnet-4-6",
        "cli.claude.claude-sonnet-4-6-thinking",
        "cli.claude.claude-opus-4-6",
        "cli.claude.claude-opus-4-6-thinking",
        "cli.claude.claude-haiku-4-5-20251001",
        "cli.claude.claude-sonnet-4-5-20250929",
        "cli.claude.claude-opus-4-5-20251101",
        "cli.claude.claude-opus-4-5-20251101-thinking",
        "cli.claude",
    ]

def _detect_gemini_cli():
    """Returns Gemini CLI model list if 'gemini' binary is found, else []."""
    if not shutil.which("gemini"):
        return []
    return ["cli.gemini.pro", "cli.gemini.flash", "cli.gemini"]

def _detect_codex_cli():
    """Returns Codex CLI model list if 'codex' binary is found, else []."""
    if not shutil.which("codex"):
        return []
    return ["cli.codex"]

_COPILOT_FALLBACK_MODELS = [
    "cli.copilot",
    "cli.copilot.claude-sonnet-4-5",
    "cli.copilot.claude-opus-4-5",
    "cli.copilot.claude-haiku-4-5",
    "cli.copilot.gpt-4o",
    "cli.copilot.gpt-4.1",
    "cli.copilot.o3",
    "cli.copilot.o4-mini",
]

def _get_github_token_sync():
    """Discover a GitHub token from env vars, gh CLI, or copilot config files (cross-platform)."""
    import os as _os, json as _json, platform
    from pathlib import Path
    token = (_os.getenv("COPILOT_GITHUB_TOKEN")
             or _os.getenv("GH_TOKEN")
             or _os.getenv("GITHUB_TOKEN"))
    if token:
        return token
    if shutil.which("gh"):
        try:
            r = subprocess.run(["gh", "auth", "token"], capture_output=True, timeout=5)
            if r.returncode == 0 and (t := r.stdout.decode().strip()):
                return t
        except Exception:
            pass
    system = platform.system()
    if system == "Windows":
        config_dirs = [Path(_os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "github-copilot"]
    elif system == "Darwin":
        config_dirs = [
            Path.home() / "Library" / "Application Support" / "github-copilot",
            Path.home() / ".config" / "github-copilot",
        ]
    else:
        config_dirs = [
            Path(_os.getenv("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "github-copilot",
        ]
    for config_dir in config_dirs:
        p = config_dir / "hosts.json"
        if p.exists():
            try:
                data = _json.loads(p.read_text())
                t = (data.get("github.com", {}).get("oauth_token")
                     or data.get("github.com", {}).get("token"))
                if t:
                    return t
            except Exception:
                pass
    p = Path.home() / ".copilot" / "config.json"
    if p.exists():
        try:
            data = _json.loads(p.read_text())
            if t := (data.get("oauth_token") or data.get("token")):
                return t
        except Exception:
            pass
    return None


def _fetch_copilot_models_sync():
    """Fetch live model list from GitHub Models catalog API; falls back to hardcoded list."""
    import urllib.request, json as _json
    token = _get_github_token_sync()
    if not token:
        return _COPILOT_FALLBACK_MODELS
    try:
        req = urllib.request.Request(
            "https://api.github.com/catalog/models",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read())
        names = [m["name"] for m in data if isinstance(m, dict) and m.get("name")]
        return (["cli.copilot"] + [f"cli.copilot.{n}" for n in names]) if names else _COPILOT_FALLBACK_MODELS
    except Exception:
        return _COPILOT_FALLBACK_MODELS


def _detect_github_copilot_cli():
    """Returns GitHub Copilot CLI model list if 'copilot' binary is found, else []."""
    if not shutil.which("copilot"):
        return []
    try:
        r = subprocess.run(["copilot", "--version"], capture_output=True, timeout=5)
        if r.returncode != 0:
            return []
    except Exception:
        return []
    return _fetch_copilot_models_sync()

def _fetch_gemini_models(api_key):
    try:
        data = _fetch_json(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        )
        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            if "generateContent" in m.get("supportedGenerationMethods", []) and name.startswith("models/"):
                models.append(name.replace("models/", ""))
        return sorted(set(models))
    except Exception as e:
        warn(f"Could not fetch Gemini models: {e}")
        return []

def _fetch_openai_models(api_key):
    try:
        data = _fetch_json(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        models = sorted(set(
            m["id"] for m in data.get("data", [])
            if m.get("id", "").startswith(("gpt-4", "gpt-3.5"))
            and "instruct" not in m.get("id", "")
        ), reverse=True)
        return models
    except Exception as e:
        warn(f"Could not fetch OpenAI models: {e}")
        return []

def _fetch_anthropic_models(api_key):
    try:
        data = _fetch_json(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        )
        models = sorted(set(m["id"] for m in data.get("data", []) if m.get("id")), reverse=True)
        return models
    except Exception as e:
        warn(f"Could not fetch Anthropic models: {e}")
        return []

def _fetch_deepseek_models(api_key):
    try:
        data = _fetch_json(
            "https://api.deepseek.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        models = sorted(set(m["id"] for m in data.get("data", []) if m.get("id")), reverse=True)
        return models
    except Exception as e:
        warn(f"Could not fetch DeepSeek models: {e}")
        return []

def _fetch_grok_models(api_key):
    try:
        data = _fetch_json(
            "https://api.x.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        models = sorted(set(m["id"] for m in data.get("data", []) if m.get("id")), reverse=True)
        return models
    except Exception as e:
        warn(f"Could not fetch Grok models: {e}")
        return []

def _fetch_v1_compatible_models(base_url, api_key=""):
    try:
        url = base_url.rstrip("/") + "/v1/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = _fetch_json(url, headers=headers)
        models = sorted(set(m["id"] for m in data.get("data", []) if m.get("id")), reverse=True)
        return models
    except Exception as e:
        warn(f"Could not fetch models from {base_url}: {e}")
        return []

def _fetch_bedrock_models(api_key, region):
    """List Bedrock foundation models using direct HTTPS bearer auth.

    boto3's AWS_BEARER_TOKEN_BEDROCK env var is only supported for bedrock-runtime
    (inference), not for the bedrock management API (list_foundation_models).
    Direct HTTP with Authorization: Bearer is the correct path for API key auth.
    """
    # Primary: direct HTTP with bearer token — works for both ABSK and bedrock-api-key formats.
    try:
        import urllib.request
        import json as _json
        url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = _json.loads(r.read())
        models = sorted(set(
            s["modelId"] for s in data.get("modelSummaries", [])
            if s.get("modelId")
        ))
        return models
    except Exception as e:
        warn(f"Direct HTTP Bedrock listing failed ({type(e).__name__}: {e}), trying boto3...")

    # Fallback: boto3 (works when IAM credentials are available in the environment)
    try:
        import boto3  # type: ignore
        client = boto3.client("bedrock", region_name=region)
        resp = client.list_foundation_models()
        models = sorted(set(
            s["modelId"] for s in resp.get("modelSummaries", [])
            if s.get("modelId")
        ))
        return models
    except Exception as e:
        warn(f"Could not fetch Bedrock models: {e}")
        return []

def ask_llm(cfg):
    step("LLM Provider & Model")

    # Detect all local / CLI providers
    info("Checking for local and CLI providers...")
    ollama_models     = _fetch_ollama_models()
    claude_cli_models = _detect_claude_cli()
    gemini_cli_models = _detect_gemini_cli()
    codex_cli_models  = _detect_codex_cli()

    # Build the list of detected local/CLI options
    local_choices = []
    if ollama_models:
        local_choices.append(f"Ollama  ({len(ollama_models)} model(s) detected)")
    if claude_cli_models:
        local_choices.append("Claude CLI  (claude)")
    if gemini_cli_models:
        local_choices.append("Gemini CLI  (gemini)")
    if codex_cli_models:
        local_choices.append("Codex CLI  (codex)")

    if local_choices:
        ok(f"Detected: {', '.join(c.split('  ')[0] for c in local_choices)}")
        choice = ask_choice("Select provider", local_choices + ["Cloud Provider"])

        if choice.startswith("Ollama"):
            model = ask_choice("Select model", ollama_models)
            cfg["model"] = model
            cfg["mode"]  = "local"
            ok(f"Model set to: {model}  (can be updated later in Settings)")
            return

        elif choice.startswith("Claude CLI"):
            model = ask_choice("Select model", claude_cli_models)
            cfg["model"] = model
            cfg["mode"]  = "cli"
            ok(f"Model set to: {model}  (can be updated later in Settings)")
            return

        elif choice.startswith("Gemini CLI"):
            model = ask_choice("Select model", gemini_cli_models)
            cfg["model"] = model
            cfg["mode"]  = "cli"
            ok(f"Model set to: {model}  (can be updated later in Settings)")
            return

        elif choice.startswith("Codex CLI"):
            model = ask_choice("Select model", codex_cli_models)
            cfg["model"] = model
            cfg["mode"]  = "cli"
            ok(f"Model set to: {model}  (can be updated later in Settings)")
            return

        # "Cloud Provider" selected — fall through to cloud section below

    else:
        # Nothing detected locally — ask if user has Ollama on a custom URL
        if ask_yn("No local/CLI providers detected. Do you have Ollama running?"):
            base_url = ask("Ollama base URL", default="http://127.0.0.1:11434").rstrip("/")
            cfg["ollama_base_url"] = base_url
            _update_env_file("OLLAMA_BASE_URL", base_url)
            info("Checking for models at that URL...")
            ollama_models = _fetch_ollama_models(base_url)
            if ollama_models:
                ok(f"Found {len(ollama_models)} model(s).")
                model = ask_choice("Select model", ollama_models)
            else:
                warn("No models found. Enter a model name manually.")
                model = ask("Ollama model name", default="llama3")
            cfg["model"] = model
            cfg["mode"]  = "local"
            ok(f"Model set to: {model}  (can be updated later in Settings)")
            return

    info("Select a cloud LLM provider:")
    providers = ["Gemini", "OpenAI", "Claude (Anthropic)", "DeepSeek", "Grok (xAI)", "OpenAI Compatible", "Local V1 Compatible", "Bedrock (AWS)"]
    choice = ask_choice("Select provider", providers)

    if choice == "Gemini":
        key = ask("Enter Gemini API key")
        cfg["gemini_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_gemini_models(key)
        if not models:
            warn("No models returned. Check your key.")
            cfg["model"] = ask("Enter model name manually", default="gemini-2.0-flash")
        else:
            cfg["model"] = ask_choice("Select model", models)

    elif choice == "OpenAI":
        key = ask("Enter OpenAI API key")
        cfg["openai_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_openai_models(key)
        if not models:
            warn("No models returned. Check your key.")
            cfg["model"] = ask("Enter model name manually", default="gpt-4o")
        else:
            cfg["model"] = ask_choice("Select model", models)

    elif choice == "Claude (Anthropic)":
        key = ask("Enter Anthropic API key")
        cfg["anthropic_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_anthropic_models(key)
        if not models:
            warn("No models returned. Check your key.")
            cfg["model"] = ask("Enter model name manually", default="claude-sonnet-4-6")
        else:
            cfg["model"] = ask_choice("Select model", models)

    elif choice == "DeepSeek":
        key = ask("Enter DeepSeek API key")
        cfg["deepseek_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_deepseek_models(key)
        if not models:
            warn("No models returned. Check your key.")
            cfg["model"] = ask("Enter model name manually", default="deepseek-chat")
        else:
            cfg["model"] = ask_choice("Select model", models)

    elif choice == "Grok (xAI)":
        key = ask("Enter xAI API key")
        cfg["xai_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_grok_models(key)
        if not models:
            warn("No models returned. Check your key.")
            cfg["model"] = ask("Enter model name manually", default="grok-3")
        else:
            cfg["model"] = ask_choice("Select model", models)

    elif choice == "OpenAI Compatible":
        key = ask("Enter API key")
        base_url = ask("Enter base URL (without /v1)", default="https://openrouter.ai/api")
        cfg["openai_compatible_key"] = key
        cfg["openai_compatible_base_url"] = base_url.rstrip("/")
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_v1_compatible_models(base_url, key)
        if not models:
            warn("No models returned. Enter model names manually.")
            manual = ask("Enter model names (comma-separated)")
            cfg["openai_compatible_models"] = manual
            if manual:
                first = manual.split(",")[0].strip()
                cfg["model"] = f"oaic.{first}"
            else:
                cfg["model"] = ask("Enter model name manually")
        else:
            choice_model = ask_choice("Select model", models)
            cfg["model"] = f"oaic.{choice_model}"

    elif choice == "Local V1 Compatible":
        base_url = ask("Enter base URL (without /v1)", default="http://localhost:8000")
        cfg["local_compatible_base_url"] = base_url.rstrip("/")
        key = ask("Enter API key (leave blank if not required)")
        if key:
            cfg["local_compatible_key"] = key
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_v1_compatible_models(base_url, key)
        if not models:
            warn("No models returned. Enter model names manually.")
            manual = ask("Enter model names (comma-separated)")
            cfg["local_compatible_models"] = manual
            if manual:
                first = manual.split(",")[0].strip()
                cfg["model"] = f"locv1.{first}"
            else:
                cfg["model"] = ask("Enter model name manually")
        else:
            choice_model = ask_choice("Select model", models)
            cfg["model"] = f"locv1.{choice_model}"

    elif choice == "Bedrock (AWS)":
        key = ask("Enter Bedrock API Key (ABSK... or bedrock-api-key... format)")
        region = ask("AWS Region", default=cfg.get("aws_region") or "us-east-1")
        cfg["bedrock_api_key"] = key
        cfg["aws_region"] = region
        cfg["mode"] = "cloud"
        info("Fetching available models...")
        models = _fetch_bedrock_models(key, region)
        if not models:
            model_id = ask("Enter Bedrock model ID manually")
        else:
            model_id = ask_choice("Select model", models)
        cfg["bedrock_inference_profile"] = model_id
        cfg["model"] = model_id

    ok(f"Provider configured. Model: {cfg.get('model', '(not set)')}")

# ---------------------------------------------------------------------------
# Default Agent Creation
# ---------------------------------------------------------------------------
DEFAULT_AGENT = {
    "id": "agent_synapse_ai",
    "name": "Synapse AI",
    "description": "Your all-purpose AI assistant with access to every capability -- browsing, code execution, file management, and more.",
    "avatar": "default",
    "type": "conversational",
    "tools": ["all"],
    "repos": [],
    "system_prompt": (
        "# Role & Identity\n"
        "You are Synapse AI -- an elite, all-purpose AI assistant with access to the full suite of tools on this platform. "
        "You exist to help the user accomplish any task with speed, accuracy, and clarity.\n\n"
        "# Core Capabilities\n"
        "You can browse the web and extract real information from any source.\n"
        "You can read, write, and manage files and directories on the local filesystem.\n"
        "You can execute Python code for calculations, data processing, and automation.\n"
        "You can interact with APIs, databases, and external services via configured tools.\n"
        "You can store and retrieve information between sessions using vault tools.\n"
        "You understand images, PDFs, spreadsheets, and structured data.\n\n"
        "# Approach & Methodology\n"
        "**Think before acting:** Understand the full request before choosing tools.\n"
        "**Use tools over memory:** Always fetch real data with tools -- never fabricate information.\n"
        "**Be concise and direct:** Give the most useful answer with minimal fluff.\n"
        "**Confirm and verify:** When you take an action (write a file, run code, browse a site), confirm what was done.\n"
        "**Adapt to complexity:** Short answers for simple questions; structured, detailed responses for complex tasks.\n\n"
        "# Output Style\n"
        "Use Markdown for structured outputs -- tables, lists, and code blocks where appropriate.\n"
        "For multi-step tasks, briefly outline what you're doing before you do it.\n"
        "When something fails or is uncertain, explain clearly and suggest next steps.\n\n"
        "# Constraints\n"
        "Never fabricate data, file contents, statistics, or API responses -- use tools.\n"
        "Never expose sensitive information (API keys, passwords) in responses.\n"
        "Always respect the user's filesystem and data -- ask before destructive operations."
    ),
}

def create_default_agent():
    """Ensure the default 'Synapse AI' agent exists in user_agents.json."""
    step("Creating Default Agent")
    agents_file = os.path.join(DATA_DIR, "user_agents.json")
    os.makedirs(DATA_DIR, exist_ok=True)

    agents = []
    if os.path.exists(agents_file):
        try:
            with open(agents_file) as f:
                agents = json.load(f)
        except Exception:
            agents = []

    # Check if already exists
    if any(a.get("id") == DEFAULT_AGENT["id"] for a in agents):
        ok("Default 'Synapse AI' agent already exists -- skipping.")
        return

    # Prepend so it appears first
    agents.insert(0, DEFAULT_AGENT)
    with open(agents_file, "w") as f:
        json.dump(agents, f, indent=4)
    ok("Created default 'Synapse AI' agent with access to all tools.")

# ---------------------------------------------------------------------------
# Install helpers
# ---------------------------------------------------------------------------
def _register_synapse_pth(venv_dir, root_dir):
    """Add root_dir to the venv's site-packages via a .pth file.

    This makes 'import synapse' work from the venv without running
    pip install -e (which triggers hatchling's build hook and requires bash).
    """
    import glob as _glob
    if IS_WIN:
        site_pkgs = os.path.join(venv_dir, "Lib", "site-packages")
    else:
        candidates = sorted(
            _glob.glob(os.path.join(venv_dir, "lib", "python*", "site-packages"))
        )
        if not candidates:
            warn(f"Could not locate site-packages inside {venv_dir}")
            return
        site_pkgs = candidates[-1]
    os.makedirs(site_pkgs, exist_ok=True)
    pth = os.path.join(site_pkgs, "synapse-source.pth")
    with open(pth, "w") as f:
        f.write(str(root_dir) + "\n")


def _run_with_retry(cmd, retries=4, delay=5, **kwargs):
    """Run a subprocess command with retries. Output flows to terminal so the user can see progress."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            subprocess.check_call(cmd, **kwargs)
            return
        except subprocess.CalledProcessError as e:
            last_exc = e
            if attempt < retries:
                warn(f"Command failed (attempt {attempt}/{retries}). Retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise last_exc

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
def install_backend(coding_enabled, messaging_enabled=False):
    step("Installing Backend Dependencies")

    if os.path.exists(VENV_DIR):
        info("Removing existing virtual environment...")
        shutil.rmtree(VENV_DIR)
    info("Creating virtual environment...")
    subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])

    info("Installing base requirements...")
    _run_with_retry([PYTHON_EXE, "-m", "pip", "install", "--upgrade", "pip"])
    _run_with_retry([PYTHON_EXE, "-m", "pip", "install", "-r", os.path.join(BACKEND_DIR, "requirements.txt")])
    ok("Base dependencies installed.")

    # Always install coding deps (cocoindex, psycopg, numpy) — they are needed
    # as soon as the user enables Code Indexing in the UI.  Installing them up
    # front avoids a second install step (and confusing "not found" errors).
    coding_req = os.path.join(BACKEND_DIR, "requirements-coding.txt")
    if os.path.exists(coding_req):
        info("Installing coding-agent dependencies (cocoindex, psycopg, numpy)...")
        _run_with_retry([PYTHON_EXE, "-m", "pip", "install", "-r", coding_req])
        ok("Coding-agent dependencies installed.")
    else:
        warn(f"requirements-coding.txt not found at {coding_req}")

    if messaging_enabled:
        messaging_req = os.path.join(BACKEND_DIR, "requirements-messaging.txt")
        if os.path.exists(messaging_req):
            info("Installing messaging integration dependencies...")
            _run_with_retry([PYTHON_EXE, "-m", "pip", "install", "-r", messaging_req])
            ok("Messaging dependencies installed.")
        else:
            warn(f"requirements-messaging.txt not found at {messaging_req}")

    info("Registering Synapse package...")
    _register_synapse_pth(VENV_DIR, ROOT_DIR)
    ok("Synapse package registered.")

def install_frontend():
    step("Installing Frontend Dependencies")
    if IS_WIN:
        npm = _find_npm_cmd_win()
    else:
        npm = shutil.which("npm")
        if not npm:
            err("npm not found.")
            sys.exit(1)
    node_modules = os.path.join(FRONTEND_DIR, "node_modules")
    if os.path.exists(node_modules):
        info("Removing existing node_modules...")
        shutil.rmtree(node_modules)
    info("Running npm install (this may take a while)...")
    _run_with_retry([npm, "install"], cwd=FRONTEND_DIR)
    ok("Frontend dependencies installed.")

    info("Building frontend...")
    _run_with_retry([npm, "run", "build"], cwd=FRONTEND_DIR)
    ok("Frontend built.")

# ---------------------------------------------------------------------------
# Port Configuration
# ---------------------------------------------------------------------------
def _update_env_file(key: str, value: str):
    """Set KEY=value in the root .env file, creating it if needed."""
    env_lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            env_lines = f.readlines()
        for i, line in enumerate(env_lines):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"# {key}"):
                env_lines[i] = f"{key}={value}\n"
                found = True
                break
    if not found:
        env_lines.append(f"{key}={value}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(env_lines)


def ask_ports(cfg):
    """Ask the user for backend and frontend ports and persist them in .env."""
    step("Server Ports")
    info(f"Backend API server port  (current default: {DEFAULT_BACKEND_PORT})")
    info(f"Frontend web UI port     (current default: {DEFAULT_FRONTEND_PORT})")
    if not ask_yn("Change the default ports?", default="n"):
        ok(f"Keeping backend={DEFAULT_BACKEND_PORT}, frontend={DEFAULT_FRONTEND_PORT}.")
        cfg["backend_port"] = DEFAULT_BACKEND_PORT
        cfg["frontend_port"] = DEFAULT_FRONTEND_PORT
        return

    backend_port = int(ask("Backend port", default=str(DEFAULT_BACKEND_PORT)))
    frontend_port = int(ask("Frontend port", default=str(DEFAULT_FRONTEND_PORT)))

    cfg["backend_port"] = backend_port
    cfg["frontend_port"] = frontend_port

    _update_env_file("SYNAPSE_BACKEND_PORT", str(backend_port))
    _update_env_file("NEXT_PUBLIC_BACKEND_PORT", str(backend_port))
    _update_env_file("SYNAPSE_FRONTEND_PORT", str(frontend_port))
    ok(f"Ports saved to .env -- backend={backend_port}, frontend={frontend_port}.")


# ---------------------------------------------------------------------------
# Start servers
# ---------------------------------------------------------------------------
def start_backend(backend_port: int = DEFAULT_BACKEND_PORT):
    step("Starting Backend Server")
    env = os.environ.copy()
    env["SYNAPSE_BACKEND_PORT"] = str(backend_port)
    # Always pass SYNAPSE_DATA_DIR as an absolute path so the backend subprocess
    # resolves it correctly regardless of its working directory.
    env["SYNAPSE_DATA_DIR"] = os.path.abspath(DATA_DIR)
    return subprocess.Popen([PYTHON_EXE, "main.py"], cwd=BACKEND_DIR, env=env)

def _find_npm_cmd_win():
    """Return the full path to npm.cmd on Windows."""
    # Look next to the node.exe we already discovered
    node_exe, bin_dir = _find_node_exe_win()
    if bin_dir:
        npm_cmd = os.path.join(bin_dir, "npm.cmd")
        if os.path.isfile(npm_cmd):
            return npm_cmd
    # Fallback: let shutil.which find it (may work if PATH is current)
    return shutil.which("npm.cmd") or shutil.which("npm") or "npm"


def start_frontend(frontend_port: int = DEFAULT_FRONTEND_PORT, backend_port: int = DEFAULT_BACKEND_PORT):
    step("Starting Frontend Server")
    env = os.environ.copy()
    env["SYNAPSE_FRONTEND_PORT"] = str(frontend_port)
    env["BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
    if IS_WIN:
        npm = _find_npm_cmd_win()
        return subprocess.Popen(
            [npm, "start"],
            cwd=FRONTEND_DIR,
            env=env,
        )
    return subprocess.Popen(
        ["npm", "start"],
        cwd=FRONTEND_DIR,
        env=env,
    )

def wait_for_server(url: str, name: str, timeout: int = 120) -> bool:
    """Wait for a server to be ready by polling HTTP, with a live elapsed counter."""
    start = time.time()
    while True:
        elapsed = int(time.time() - start)
        if elapsed >= timeout:
            print()
            warn(f"Timed out waiting for {name} after {timeout}s.")
            return False
        try:
            urllib.request.urlopen(url, timeout=3)
            print(f"\r", end="")  # clear the progress line
            return True
        except Exception:
            print(f"\r   Waiting for {name}... {elapsed}s", end="", flush=True)
            time.sleep(2)

# ---------------------------------------------------------------------------
# PATH Setup Helpers
# ---------------------------------------------------------------------------
def add_to_bashrc():
    """Add bin directory to PATH and ensure binary is executable"""
    bashrc = os.path.expanduser("~/.bashrc")
    bin_dir = os.path.join(ROOT_DIR, "bin")
    
    # Ensure the binary has full execution permissions (chmod 755)
    synapse_bin = os.path.join(bin_dir, "synapse")
    if os.path.exists(synapse_bin):
        os.chmod(synapse_bin, 0o755)
        ok(f"Set execution permissions for {synapse_bin}")
    # Also chmod any .sh wrapper if present
    synapse_sh = os.path.join(bin_dir, "synapse.sh")
    if os.path.exists(synapse_sh):
        os.chmod(synapse_sh, 0o755)

    export_line = f"\nexport PATH=\"{bin_dir}:$PATH\"  # Synapse AI"
    
    if not os.path.exists(bashrc):
        with open(bashrc, "w") as f:
            f.write(export_line + "\n")
        ok(f"Created {bashrc} with Synapse PATH")
        return True
    
    with open(bashrc, "r") as f:
        content = f.read()
    
    if bin_dir in content:
        ok("Synapse already in PATH (bashrc)")
        return True

    with open(bashrc, "a") as f:
        f.write(export_line + "\n")
    ok(f"Added Synapse to PATH (bashrc)")
    return True

def add_to_zshrc():
    """Add bin directory to PATH in ~/.zshrc"""
    zshrc = os.path.expanduser("~/.zshrc")
    if not os.path.exists(zshrc):
        return False
    
    bin_dir = os.path.join(ROOT_DIR, "bin")
    export_line = f"\nexport PATH=\"{bin_dir}:$PATH\"  # Synapse AI"
    
    with open(zshrc, "r") as f:
        content = f.read()
    
    if bin_dir in content:
        ok("Synapse already in PATH (zshrc)")
        return True
    
    with open(zshrc, "a") as f:
        f.write(export_line + "\n")
    ok(f"Added Synapse to PATH (zshrc)")
    return True

def _add_to_windows_path(bin_dir):
    """Persist bin_dir in the user PATH via setx and the PowerShell profile."""
    # 1. setx -- persists across new cmd/powershell sessions
    try:
        current = subprocess.run(
            ["reg", "query", "HKCU\\Environment", "/v", "PATH"],
            capture_output=True, text=True
        ).stdout
        # Extract existing user PATH
        user_path = ""
        for line in current.splitlines():
            if "PATH" in line and "REG_" in line:
                parts = line.split(None, 2)
                if len(parts) >= 3:
                    user_path = parts[2].strip()
                    break
        if bin_dir.lower() not in user_path.lower():
            new_path = (user_path + ";" + bin_dir) if user_path else bin_dir
            subprocess.run(["setx", "PATH", new_path], capture_output=True)
            ok(f"Added {bin_dir} to user PATH (setx). Changes take effect in new terminals.")
        else:
            ok("Synapse bin directory is already in user PATH.")
    except Exception as e:
        warn(f"setx PATH update failed: {e}")

    # 2. Also prepend to the current process PATH so synapse.bat is findable now
    if bin_dir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def setup_path():
    """Setup PATH for the current platform"""
    step("Setting up Synapse command")
    bin_dir = os.path.join(ROOT_DIR, "bin")

    if IS_WIN:
        info("Registering 'synapse' command in PATH...")
        _add_to_windows_path(bin_dir)
        info("In your CURRENT session you can already run: synapse start")
        info(f"(If 'synapse' is not found, open a new terminal -- setx takes effect then.)")
        ok("Windows PATH setup complete.")
    else:
        # Unix: Try to add to .bashrc / .zshrc
        info("Checking for shell configuration files...")
        add_to_bashrc()
        add_to_zshrc()

        # Update PATH in the current process so synapse is immediately usable
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        info("")
        info("To use 'synapse' in your CURRENT terminal, run:")
        info(f"  export PATH=\"{bin_dir}:$PATH\"")
        info("(Already saved to ~/.bashrc / ~/.zshrc for future sessions.)")
        ok("PATH setup complete.")

def show_restart_instructions():
    """Show instructions for restarting Synapse"""
    step("To Start Synapse Again Later")

    if IS_WIN:
        info("Open a new terminal (cmd or PowerShell) and run:")
        info("  synapse start")
        info("")
        info("If 'synapse' is not recognised yet (PATH not refreshed), use:")
        info(f"  python -m synapse start")
        info(f"  (run this from inside the synapse-ai folder)")
        info("")
        info("Other useful commands:")
        info("  synapse stop      # Stop running services")
        info("  synapse status    # Check service status")
        info("  synapse restart   # Restart services")
        info("  synapse upgrade   # Upgrade to the latest version")
        info("")
        info("Installed via pip or npm instead?")
        info("  pip upgrade:  pip install --upgrade synapse-orch-ai")
        info("  npm upgrade:  npm update -g synapse-orch-ai")
    else:
        info("Simply run:")
        info(f"  synapse start")
        info("")
        info("If the command is not found, either:")
        info(f"  1. Run: source ~/.bashrc   (or source ~/.zshrc)")
        info(f"  2. Or open a new terminal")
        info(f"  3. Or use: python -m synapse start")
        info("")
        info("Other useful commands:")
        info(f"  synapse stop      # Stop running services")
        info(f"  synapse status    # Check service status")
        info(f"  synapse restart   # Restart services")
        info(f"  synapse upgrade   # Upgrade to the latest version")
        info("")
        info("Installed via pip or npm instead?")
        info(f"  pip upgrade:  pip install --upgrade synapse-orch-ai")
        info(f"  npm upgrade:  npm update -g synapse-orch-ai")


# ---------------------------------------------------------------------------
# Install Location & Already-Installed Detection
# ---------------------------------------------------------------------------
def _get_default_install_dir():
    """Return the OS-standard directory where Synapse AI should be installed."""
    if IS_WIN:
        local_app = os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        )
        return os.path.join(local_app, "Programs", "SynapseAI")
    elif sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", "SynapseAI"
        )
    else:  # Linux
        return os.path.join(os.path.expanduser("~"), ".local", "share", "synapse-ai")


def _is_already_installed():
    """Return (True, install_dir) if a previous install is found, else (False, None)."""
    # Check the directory this setup.py lives in (most common case)
    if os.path.exists(os.path.join(ROOT_DIR, ".installed")):
        return True, ROOT_DIR
    # Check the OS-standard location (installed by a previous run from elsewhere)
    default_dir = _get_default_install_dir()
    if os.path.exists(os.path.join(default_dir, ".installed")):
        return True, default_dir
    return False, None


def _write_install_marker():
    """Write a .installed marker so future runs detect an existing install."""
    import datetime
    marker = os.path.join(ROOT_DIR, ".installed")
    with open(marker, "w") as f:
        json.dump(
            {
                "installed_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "install_dir": ROOT_DIR,
                "platform": sys.platform,
            },
            f,
            indent=2,
        )
    ok(f"Install marker written.")


def _stop_running_services(install_dir):
    """Stop any running Synapse services before rebuilding."""
    step("Stopping Running Services")
    synapse_bin = os.path.join(install_dir, "bin", "synapse" + (".bat" if IS_WIN else ""))
    stopped = False

    # Try calling `synapse stop` via the installed bin script
    if os.path.exists(synapse_bin):
        try:
            result = subprocess.run(
                [synapse_bin, "stop"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                ok("Services stopped.")
                stopped = True
            else:
                warn("synapse stop returned non-zero; trying PID files...")
        except Exception as e:
            warn(f"Could not invoke synapse stop: {e}")

    # Fallback: kill PIDs recorded in run/ directory
    if not stopped:
        run_dir = os.path.join(install_dir, "run")
        for pid_file in ("backend.pid", "frontend.pid"):
            pid_path = os.path.join(run_dir, pid_file)
            if os.path.exists(pid_path):
                try:
                    with open(pid_path) as pf:
                        pid = int(pf.read().strip())
                    if IS_WIN:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            capture_output=True,
                        )
                    else:
                        import signal as _sig
                        try:
                            os.killpg(os.getpgid(pid), _sig.SIGTERM)
                        except ProcessLookupError:
                            pass
                    ok(f"Stopped PID {pid} (from {pid_file}).")
                    os.remove(pid_path)
                except Exception as e:
                    warn(f"Could not stop process from {pid_file}: {e}")

    # Brief pause to let ports free up
    import time as _time
    _time.sleep(2)


def _rebuild_backend(install_dir):
    """Re-install backend Python dependencies."""
    step("Rebuilding Backend")
    backend_dir = os.path.join(install_dir, "backend")
    venv_dir    = os.path.join(backend_dir, "venv")
    python_exe  = os.path.join(venv_dir, "Scripts" if IS_WIN else "bin",
                               "python" + (".exe" if IS_WIN else ""))

    pyvenv_cfg = os.path.join(venv_dir, "pyvenv.cfg")
    venv_broken = os.path.exists(venv_dir) and not os.path.exists(pyvenv_cfg)
    if not os.path.exists(python_exe) or venv_broken:
        if venv_broken:
            warn("Virtual environment is corrupted (missing pyvenv.cfg) -- recreating...")
            import shutil as _shutil
            _shutil.rmtree(venv_dir, ignore_errors=True)
        else:
            warn("Virtual environment not found -- creating one now...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
        ok("Virtual environment created.")

    pip_cmd = [python_exe, "-m", "pip", "install"]
    req_txt  = os.path.join(backend_dir, "requirements.txt")

    info("Upgrading pip...")
    subprocess.run([python_exe, "-m", "pip", "install", "--upgrade", "pip"],
                   capture_output=True)

    info("Installing / upgrading backend requirements...")
    subprocess.check_call(pip_cmd + ["-r", req_txt])

    # Always install coding deps (cocoindex, psycopg, numpy).
    coding_req = os.path.join(backend_dir, "requirements-coding.txt")
    if os.path.exists(coding_req):
        info("Installing coding-agent requirements (cocoindex, psycopg, numpy)...")
        subprocess.check_call(pip_cmd + ["-r", coding_req])
    else:
        warn(f"requirements-coding.txt not found at {coding_req}")

    # Messaging deps are heavier — only install when opted in.
    _settings: dict = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            import json as _json
            with open(SETTINGS_FILE) as _f:
                _settings = _json.load(_f)
        except Exception:
            pass

    if _settings.get("messaging_enabled", False):
        messaging_req = os.path.join(backend_dir, "requirements-messaging.txt")
        if os.path.exists(messaging_req):
            info("Installing messaging requirements...")
            subprocess.check_call(pip_cmd + ["-r", messaging_req])
        else:
            warn(f"requirements-messaging.txt not found at {messaging_req}")

    info("Registering Synapse package...")
    _register_synapse_pth(venv_dir, install_dir)
    ok("Backend dependencies updated.")


def _rebuild_frontend(install_dir):
    """Re-install and rebuild the frontend."""
    step("Rebuilding Frontend")
    frontend_dir = os.path.join(install_dir, "frontend")

    if IS_WIN:
        npm = _find_npm_cmd_win()
    else:
        import shutil as _sh
        npm = _sh.which("npm")
        if not npm:
            warn("npm not found -- skipping frontend rebuild.")
            return

    info("Running npm install...")
    subprocess.check_call([npm, "install"], cwd=frontend_dir)

    info("Building frontend...")
    subprocess.check_call([npm, "run", "build"], cwd=frontend_dir)
    ok("Frontend rebuilt successfully.")


def _handle_already_installed(install_dir):
    """Stop services, pull latest, rebuild, show start instructions."""
    print(f"\n{C.BOLD}{C.GREEN}{'=' * 54}{C.RESET}")
    print(f"{C.BOLD}{C.GREEN}   Synapse AI is already installed!{C.RESET}")
    print(f"{C.BOLD}{C.GREEN}{'=' * 54}{C.RESET}")
    print(f"\n   Location: {_c(C.CYAN, install_dir)}\n")

    # ------------------------------------------------------------------
    # 1. Stop any running services before we touch the code
    # ------------------------------------------------------------------
    _stop_running_services(install_dir)

    # ------------------------------------------------------------------
    # 2. Pull latest changes
    # ------------------------------------------------------------------
    step("Pulling Latest Changes")
    updated = False
    try:
        result = subprocess.run(
            ["git", "-C", install_dir, "pull", "--ff-only"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            if "Already up to date" in output:
                ok("Already up to date -- no code changes.")
            else:
                ok("Updated to the latest version.")
                updated = True
                for line in output.splitlines():
                    info(f"  {line}")
        else:
            warn(f"git pull exited with code {result.returncode}.")
            if result.stderr.strip():
                info(result.stderr.strip())
    except FileNotFoundError:
        warn("git not found -- skipping update check.")
    except Exception as e:
        warn(f"Update check failed: {e}")

    # ------------------------------------------------------------------
    # 3. Rebuild backend and frontend
    # ------------------------------------------------------------------
    try:
        _rebuild_backend(install_dir)
    except Exception as e:
        warn(f"Backend rebuild failed: {e}")

    try:
        _rebuild_frontend(install_dir)
    except Exception as e:
        warn(f"Frontend rebuild failed: {e}")

    # ------------------------------------------------------------------
    # 4. Refresh PATH and execute permissions
    # ------------------------------------------------------------------
    setup_path()

    # ------------------------------------------------------------------
    # 5. Show instructions
    # ------------------------------------------------------------------
    print()
    print(f"{C.BOLD}{C.GREEN}Synapse AI has been updated and rebuilt!{C.RESET}")
    print()
    print(f"{C.BOLD}To start Synapse:{C.RESET}")
    print(f"   {_c(C.CYAN, 'synapse start')}")
    print()
    print(f"Other commands:")
    print(f"   synapse stop      -- stop running services")
    print(f"   synapse status    -- check service status")
    print(f"   synapse restart   -- restart services")
    print()
    sys.exit(0)


# ---------------------------------------------------------------------------
# Startup on Boot
# ---------------------------------------------------------------------------
def _register_startup_win():
    """Register synapse start --detach in HKCU Run (no admin required)."""
    try:
        import winreg  # type: ignore  # stdlib on Windows
        synapse_bat = os.path.join(ROOT_DIR, "bin", "synapse.bat")
        command = f'"{synapse_bat}" start --detach'
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "SynapseAI", 0, winreg.REG_SZ, command)
        winreg.CloseKey(key)
        ok("Synapse registered to start on login (Registry > Run).")
    except Exception as e:
        warn(f"Could not register startup: {e}")
        info("You can add it manually: search 'Task Scheduler' in the Start menu.")


def _unregister_startup_win():
    """Remove the HKCU Run registry entry."""
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "SynapseAI")
            ok("Removed Synapse from startup (Registry > Run).")
        except FileNotFoundError:
            pass  # not registered -- fine
        winreg.CloseKey(key)
    except Exception as e:
        warn(f"Could not remove startup entry: {e}")


def _is_startup_registered_win():
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, "SynapseAI")
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False


def _register_startup_mac():
    """Install a LaunchAgent plist so Synapse starts on login."""
    launch_agents = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    os.makedirs(launch_agents, exist_ok=True)
    plist_path = os.path.join(launch_agents, "com.synapse-ai.server.plist")
    synapse_bin = os.path.join(ROOT_DIR, "bin", "synapse")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.synapse-ai.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>"{synapse_bin}" start --detach</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>/tmp/synapse-ai.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/synapse-ai-error.log</string>
</dict>
</plist>
"""
    try:
        with open(plist_path, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", plist_path], check=False, capture_output=True)
        ok(f"LaunchAgent installed -- Synapse will start on login.")
        info(f"  Plist: {plist_path}")
    except Exception as e:
        warn(f"Could not install LaunchAgent: {e}")


def _unregister_startup_mac():
    plist_path = os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", "com.synapse-ai.server.plist"
    )
    if os.path.exists(plist_path):
        try:
            subprocess.run(["launchctl", "unload", plist_path], check=False, capture_output=True)
            os.remove(plist_path)
            ok("Removed Synapse LaunchAgent.")
        except Exception as e:
            warn(f"Could not remove LaunchAgent: {e}")


def _is_startup_registered_mac():
    plist_path = os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents", "com.synapse-ai.server.plist"
    )
    return os.path.exists(plist_path)


def _register_startup_linux():
    """Install a systemd user service so Synapse starts on login."""
    service_dir = os.path.join(
        os.path.expanduser("~"), ".config", "systemd", "user"
    )
    os.makedirs(service_dir, exist_ok=True)
    service_path = os.path.join(service_dir, "synapse-ai.service")
    synapse_bin = os.path.join(ROOT_DIR, "bin", "synapse")
    bin_dir = os.path.join(ROOT_DIR, "bin")

    # Try to find node bin dir for the service PATH
    node_dir = ""
    node_exe = shutil.which("node")
    if node_exe:
        node_dir = os.path.dirname(node_exe) + ":"

    service_content = f"""[Unit]
Description=Synapse AI Server
After=network.target

[Service]
Type=forking
ExecStart={synapse_bin} start --detach
ExecStop={synapse_bin} stop
WorkingDirectory={ROOT_DIR}
Environment="PATH={bin_dir}:{node_dir}/usr/local/bin:/usr/bin:/bin"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    try:
        with open(service_path, "w") as f:
            f.write(service_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "synapse-ai.service"], check=False, capture_output=True)
        ok("systemd user service installed -- Synapse will start on login.")
        info(f"  Service: {service_path}")
        info("  To start now (without rebooting): systemctl --user start synapse-ai")
    except Exception as e:
        warn(f"Could not install systemd service: {e}")


def _unregister_startup_linux():
    service_path = os.path.join(
        os.path.expanduser("~"), ".config", "systemd", "user", "synapse-ai.service"
    )
    if os.path.exists(service_path):
        try:
            subprocess.run(["systemctl", "--user", "disable", "synapse-ai.service"],
                           check=False, capture_output=True)
            os.remove(service_path)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
            ok("Removed Synapse systemd user service.")
        except Exception as e:
            warn(f"Could not remove systemd service: {e}")


def _is_startup_registered_linux():
    service_path = os.path.join(
        os.path.expanduser("~"), ".config", "systemd", "user", "synapse-ai.service"
    )
    return os.path.exists(service_path)


def ask_startup_on_boot(cfg):
    """Ask the user whether Synapse should start automatically on login/boot."""
    step("Start on Login")

    # Check current registration state
    if IS_WIN:
        currently_registered = _is_startup_registered_win()
    elif sys.platform == "darwin":
        currently_registered = _is_startup_registered_mac()
    else:
        currently_registered = _is_startup_registered_linux()

    if currently_registered:
        info("Synapse is currently set to start automatically on login.")
        keep = ask_yn("Keep this setting?", default="y")
        if keep:
            ok("Auto-start on login kept.")
            cfg["start_on_boot"] = True
            return
        else:
            # Unregister
            if IS_WIN:
                _unregister_startup_win()
            elif sys.platform == "darwin":
                _unregister_startup_mac()
            else:
                _unregister_startup_linux()
            cfg["start_on_boot"] = False
            ok("Auto-start on login disabled.")
            return

    info("Synapse can start automatically in the background when you log in.")
    info("It runs silently -- just open your browser to http://localhost:3000.")
    enable = ask_yn("Start Synapse automatically on login?", default="n")
    cfg["start_on_boot"] = enable

    if not enable:
        ok("Auto-start disabled.")
        return

    if IS_WIN:
        _register_startup_win()
    elif sys.platform == "darwin":
        _register_startup_mac()
    else:
        _register_startup_linux()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # ------------------------------------------------------------------
    # --upgrade flag: skip the wizard, just rebuild and exit
    # ------------------------------------------------------------------
    upgrade_mode = "--upgrade" in sys.argv

    if upgrade_mode:
        print(f"\n{C.BOLD}{C.CYAN}{'=' * 50}{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}   Synapse AI -- Rebuild{C.RESET}")
        print(f"{C.BOLD}{C.CYAN}{'=' * 50}{C.RESET}\n")
        check_python()
        check_npm()
        check_uvx()
        try:
            _rebuild_backend(ROOT_DIR)
        except Exception as e:
            warn(f"Backend rebuild failed: {e}")
        try:
            _rebuild_frontend(ROOT_DIR)
        except Exception as e:
            warn(f"Frontend rebuild failed: {e}")
        setup_path()
        print()
        ok("Rebuild complete.")
        sys.exit(0)

    print(f"\n{C.BOLD}{C.CYAN}{'=' * 50}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}   Synapse AI -- Setup Wizard{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'=' * 50}{C.RESET}\n")

    # ------------------------------------------------------------------
    # Already-installed: stop services, rebuild, show instructions
    # ------------------------------------------------------------------
    already_installed, install_dir = _is_already_installed()
    if already_installed:
        _handle_already_installed(install_dir)
        # _handle_already_installed always calls sys.exit(0)

    check_python()
    check_npm()
    check_uvx()

    cfg = load_settings()

    ask_coding_agent(cfg)
    ask_embed_code(cfg)

    setup_browser_automation(cfg)
    setup_messaging_app(cfg)
    ask_ports(cfg)
    ask_google_workspace(cfg)
    ask_agent_name(cfg)
    ask_llm(cfg)

    step("Writing Settings")
    save_settings(cfg)
    ok(f"Settings saved to {SETTINGS_FILE}")

    create_default_agent()

    try:
        install_backend(
            cfg.get("coding_agent_enabled", False),
            cfg.get("messaging_enabled", False),
        )
        install_frontend()
    except subprocess.CalledProcessError as e:
        err(f"Installation failed: {e}")
        sys.exit(1)

    setup_path()

    # Write the install marker so future runs detect this installation
    _write_install_marker()

    # Ask about auto-start on login
    ask_startup_on_boot(cfg)
    save_settings(cfg)  # persist start_on_boot preference

    print()
    start_now = ask_yn("Start Synapse now?", default="y")

    if not start_now:
        print()
        show_restart_instructions()
        print(f"\n{C.GREEN}Setup complete! Synapse is ready to use.{C.RESET}\n")
        sys.exit(0)

    _backend_port = cfg.get("backend_port", DEFAULT_BACKEND_PORT)
    _frontend_port = cfg.get("frontend_port", DEFAULT_FRONTEND_PORT)

    backend_proc = start_backend(backend_port=_backend_port)

    if not wait_for_server(f"http://127.0.0.1:{_backend_port}/docs", "Backend", timeout=300):
        err("Backend did not start in time. Check the output above for errors.")
        backend_proc.terminate()
        sys.exit(1)
    ok("Backend is ready.")

    frontend_proc = start_frontend(frontend_port=_frontend_port, backend_port=_backend_port)

    if not wait_for_server(f"http://127.0.0.1:{_frontend_port}", "Frontend", timeout=120):
        err("Frontend did not start in time. Check the output above for errors.")
        backend_proc.terminate()
        frontend_proc.terminate()
        sys.exit(1)
    ok("Frontend is ready.")

    print(f"\n{C.BOLD}{C.GREEN}Application is running!{C.RESET}")
    print(f"   Frontend: {_c(C.CYAN, f'http://localhost:{_frontend_port}')}")
    print(f"   Backend:  {_c(C.CYAN, f'http://localhost:{_backend_port}')}")
    print(f"\n{C.YELLOW}Press Ctrl+C to stop.{C.RESET}\n")

    try:
        while True:
            time.sleep(1)
            if backend_proc.poll() is not None:
                err("Backend crashed! Check the logs above.")
                break
            if frontend_proc.poll() is not None:
                err("Frontend crashed! Check the logs above.")
                break
    except KeyboardInterrupt:
        print("\nStopping servers...")
        if IS_WIN:
            # terminate() only kills the outermost .cmd wrapper on Windows;
            # taskkill /F /T kills the entire process tree (npm -> node -> next)
            for proc in (frontend_proc, backend_proc):
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
            # Kill the whole process group so npm -> node children all exit
            for proc in (backend_proc, frontend_proc):
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        print("Goodbye!")
        sys.exit(0)



if __name__ == "__main__":
    main()

