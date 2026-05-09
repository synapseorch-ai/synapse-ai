import asyncio
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Optional
from contextlib import asynccontextmanager, AsyncExitStack
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import timedelta
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Default timeout for all MCP session requests — prevents any call from hanging
# forever.  Per-request read_timeout_seconds overrides this when supplied.
_SESSION_READ_TIMEOUT = timedelta(seconds=60)

try:
    from core.memory import MemoryStore
except ImportError:
    print("Warning: MemoryStore dependencies not found. Memory disabled.", file=sys.stderr)
    MemoryStore = None

from core.mcp_client import MCPClientManager
from core.config import load_settings, get_or_create_jwt_secret
from core.routes.settings import _init_memory_store

# Ensure JWT secret is available before any auth route is used
get_or_create_jwt_secret()

# Route routers
from core.routes.auth import router as auth_router
from core.routes.settings import router as settings_router
from core.routes.agents import router as agents_router
from core.routes.tools import router as tools_router
from core.routes.n8n import router as n8n_router
from core.routes.data import router as data_router
from core.routes.chat import router as chat_router
from core.routes.repos import router as repos_router
from core.routes.db_configs import router as db_configs_router
from core.routes.orchestrations import router as orchestrations_router
from core.routes.logs import router as logs_router
from core.routes.messaging import router as messaging_router
from core.routes.sessions import router as sessions_router
from core.routes.usage import router as usage_router
from core.routes.profiling import router as profiling_router
from core.routes.schedules import router as schedules_router
from core.routes.import_export import router as import_export_router
from core.routes.vault import router as vault_router
from core.routes.builder import router as builder_router
from core.routes.api_keys import router as api_keys_router
from core.routes.api_v1 import router as api_v1_router
from core.profiling import TimingMiddleware
from core.internal_auth import InternalTokenMiddleware

# Configuration
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = "llama3"

# On Windows, npx is a .cmd file and must be invoked as "npx.cmd" for subprocess
_IS_WIN = platform.system() == "Windows"
_NPX_CMD = "npx.cmd" if _IS_WIN else "npx"

# Agent Configuration
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = BACKEND_ROOT.parent
_data_dir_env = os.getenv("SYNAPSE_DATA_DIR", "")
if _data_dir_env:
    _data_dir_p = Path(_data_dir_env)
    DATA_DIR = _data_dir_p if _data_dir_p.is_absolute() else _PROJECT_ROOT / _data_dir_p
else:
    DATA_DIR = BACKEND_ROOT / "data"
GOOGLE_CREDENTIALS_DIR = DATA_DIR / "google-credentials"

_settings = load_settings()

# Propagate ollama_base_url from settings to env so llm_providers picks it up
if _settings.get("ollama_base_url"):
    os.environ["OLLAMA_BASE_URL"] = _settings["ollama_base_url"]

TOOLS_LIST = {
    "time": str(TOOLS_DIR / "time.py"),
    "sql": str(TOOLS_DIR / "sql_agent.py"),
    "personal_details": str(TOOLS_DIR / "personal_details.py"),
    "collect_data": str(TOOLS_DIR / "collect_data.py"),
    "pdf_parser": str(TOOLS_DIR / "pdf_parser.py"),
    "xlsx_parser": str(TOOLS_DIR / "xlsx_parser.py"),
    "vault_sandbox": str(TOOLS_DIR / "sandbox.py"),
    "code_vault_search": str(TOOLS_DIR / "code_search.py"),
    "web_scraper": str(TOOLS_DIR / "web_scraper.py"),
    "bash": str(TOOLS_DIR / "bash.py"),
}

REPOS_FILE = DATA_DIR / "repos.json"

def _get_repo_paths() -> list[str]:
    """Load repo paths from repos.json for filesystem MCP server permissions."""
    if not REPOS_FILE.exists():
        return []
    try:
        repos = json.loads(REPOS_FILE.read_text())
        return [r["path"] for r in repos if r.get("path") and os.path.isdir(r["path"])]
    except Exception as e:
        print(f"Warning: Could not load repo paths: {e}")
        return []


def _get_google_oauth_env() -> dict[str, str]:
    """Extract OAuth client_id and client_secret from credentials.json for workspace-mcp.
    Also reads user email from token.json to pass USER_GOOGLE_EMAIL for single-user mode."""
    creds_file = DATA_DIR / "credentials.json"
    token_file = DATA_DIR / "token.json"
    if not creds_file.exists():
        return {}
    try:
        creds = json.loads(creds_file.read_text())
        installed = creds.get("installed", creds.get("web", {}))
        client_id = installed.get("client_id", "")
        client_secret = installed.get("client_secret", "")
        if not (client_id and client_secret):
            return {}

        env = {
            "GOOGLE_OAUTH_CLIENT_ID": client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
            "OAUTHLIB_INSECURE_TRANSPORT": "1",  # allow http:// redirect URIs for localhost
            "GOOGLE_MCP_CREDENTIALS_DIR": str(GOOGLE_CREDENTIALS_DIR.resolve()),
        }

        # Read user email from token.json so workspace-mcp can skip the email prompt
        if token_file.exists():
            try:
                import base64
                token_data = json.loads(token_file.read_text())
                email = token_data.get("email")
                if not email and token_data.get("token"):
                    id_token = token_data["token"]
                    payload_b64 = id_token.split(".")[1]
                    payload_b64 += "=" * (4 - len(payload_b64) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    email = payload.get("email")
                if email:
                    env["USER_GOOGLE_EMAIL"] = email
            except Exception as e:
                print(f"Warning: Could not read user email from token.json: {e}")

        return env
    except Exception as e:
        print(f"Warning: Could not read Google OAuth credentials: {e}")
    return {}


def _build_native_mcp_servers() -> list[dict]:
    """
    Build the list of native MCP servers to connect at startup.
    Returns a list of dicts with keys: name, command, args, env (optional).
    """
    servers = []

    # --- Filesystem MCP Server ---
    repo_paths = _get_repo_paths()
    vault_path = str(DATA_DIR / "vault")
    # Always start with vault; include any configured repo paths on top.
    fs_paths = repo_paths + [vault_path]
    if not repo_paths:
        print("Warning: No repos configured — starting filesystem MCP server with vault access only.")
    servers.append({
        "name": "Filesystem",
        "command": _NPX_CMD,
        "args": ["-y", "@modelcontextprotocol/server-filesystem"] + fs_paths,
    })

    # --- Playwright MCP Server (browser automation) ---
    if _settings.get("browser_automation_enabled", True):
        env_dict = {}
        pw_path = _settings.get("playwright_browsers_path")
        if pw_path:
            env_dict["PLAYWRIGHT_BROWSERS_PATH"] = pw_path
        else:
            # Fallback for old configs
            env_dict["PLAYWRIGHT_BROWSERS_PATH"] = os.path.expanduser("~/.cache/ms-playwright")

        servers.append({
            "name": "Browser Automation",
            "command": _NPX_CMD,
            "args": ["-y", "@playwright/mcp@latest", "--browser", "chromium", "--output-dir", "data/vault/playwright"],
            "env": env_dict,
        })

    # --- Google Workspace MCP Server (Gmail, Drive, Calendar) ---
    google_env = _get_google_oauth_env()
    if google_env:
        servers.append({
            "name": "Google Workspace",
            "command": "uvx",
            "args": ["workspace-mcp", "--single-user", "--tools", "gmail", "drive", "calendar", "docs", "sheets", "slides", "forms", "tasks", "contacts"],
            "env": google_env,
        })
    else:
        print("Warning: No Google OAuth credentials found — skipping Google Workspace MCP server.")


    # --- Memory MCP Server ---
    memory_file_path = DATA_DIR / "memory" / "memory.jsonl"
    servers.append({
        "name": "Memory",
        "command": _NPX_CMD,
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {"MEMORY_FILE_PATH": str(memory_file_path)},
    })

    # --- Sequential Thinking MCP Server ---
    servers.append({
        "name": "Sequential Thinking",
        "command": _NPX_CMD,
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
    })

    return servers


async def _connect_filesystem_mcp(label: str = "started") -> None:
    """
    Connect the Filesystem MCP subprocess and register its tools.
    MUST only be called from _filesystem_mcp_manager — anyio cancel scopes
    must be entered and exited by the same task.
    """
    global _filesystem_stack

    fs_cfg = next((c for c in _build_native_mcp_servers() if c["name"] == "Filesystem"), None)
    if not fs_cfg:
        print("Warning: Could not build Filesystem MCP config — skipping.")
        return

    cmd = fs_cfg["command"]
    if not shutil.which(cmd):
        print(f"Warning: '{cmd}' not found — cannot start Filesystem MCP server.")
        return

    _filesystem_stack = AsyncExitStack()
    try:
        env = os.environ.copy()
        env.update(fs_cfg.get("env", {}))
        server_params = StdioServerParameters(command=cmd, args=fs_cfg["args"], env=env)
        read, write = await _filesystem_stack.enter_async_context(stdio_client(server_params))
        session = await _filesystem_stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
        )
        await session.initialize()
        tools = await session.list_tools()
        # Only register the session after list_tools() succeeds — avoids a dead
        # session entry in agent_sessions that causes repeated "Error listing tools"
        # log spam on every /api/tools/available poll.
        agent_sessions["Filesystem"] = session
        for tool in tools.tools:
            tool_router[tool.name] = ("Filesystem", tool.name)
        print(f"Filesystem MCP server {label} ({len(tools.tools)} tools registered).")
    except Exception as e:
        print(f"Failed to start Filesystem MCP server: {e}")
        # Ensure no stale session entry is left behind
        agent_sessions.pop("Filesystem", None)
        try:
            await _filesystem_stack.aclose()
        except Exception:
            pass
        _filesystem_stack = None


async def _filesystem_mcp_manager() -> None:
    """
    Long-running task that exclusively owns the Filesystem MCP subprocess lifecycle.

    anyio requires that cancel scopes are exited by the same task that entered them.
    Previously, restart_filesystem_mcp() called _filesystem_stack.aclose() from an
    HTTP request-handler task (a different task than lifespan), which caused a cancel-
    scope violation that propagated a CancelledError to the lifespan, tearing down
    ALL MCP sessions.  Concentrating every open/close operation here eliminates that.

    Route handlers request a restart by putting an asyncio.Future on
    _filesystem_restart_queue; this task performs the work and resolves the future.
    """
    global _filesystem_stack

    await _connect_filesystem_mcp(label="started")

    if _filesystem_ready is not None:
        _filesystem_ready.set()

    try:
        while True:
            future: Optional[asyncio.Future] = await _filesystem_restart_queue.get()

            # Clear stale Filesystem tools from shared routing tables
            stale_tools = [k for k, v in tool_router.items() if v[0] == "Filesystem"]
            for t in stale_tools:
                del tool_router[t]
            agent_sessions.pop("Filesystem", None)

            # Close old stack in the task that created it — no cancel-scope violation
            if _filesystem_stack:
                try:
                    await _filesystem_stack.aclose()
                except Exception as e:
                    print(f"Warning: Error closing old filesystem MCP stack: {e}")
                _filesystem_stack = None

            print("Restarting Filesystem MCP server with updated repo paths...")
            await _connect_filesystem_mcp(label="restarted")

            if future is not None and not future.done():
                future.set_result(None)

    except asyncio.CancelledError:
        # Graceful shutdown — clean up the subprocess we own
        if _filesystem_stack:
            try:
                await _filesystem_stack.aclose()
            except Exception as e:
                print(f"Warning: Error closing filesystem MCP stack during shutdown: {e}")
            _filesystem_stack = None


async def restart_filesystem_mcp() -> None:
    """
    Signal the filesystem manager task to restart with the latest repo paths.
    Called from route handlers when repos are added, updated, or deleted.
    Awaits completion so the caller knows the new path list is active.
    """
    if _filesystem_restart_queue is None:
        return
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await _filesystem_restart_queue.put(future)
    await future


# ---------------------------------------------------------------------------
# Module-level mutable state.
# Accessed by routes via `import core.server as _server; _server.agent_sessions`.
# The react_engine receives this module as a parameter for testability.
# ---------------------------------------------------------------------------
agent_sessions: dict[str, ClientSession] = {}   # client_name -> MCP session
tool_router: dict[str, tuple[str, str]] = {}     # tool_key -> (session_name, actual_tool_name)
exit_stack: Optional[AsyncExitStack] = None
_filesystem_stack: Optional[AsyncExitStack] = None          # owned exclusively by _filesystem_mcp_manager
_filesystem_restart_queue: Optional[asyncio.Queue] = None   # route handlers put Futures here to request restarts
_filesystem_manager_task: Optional[asyncio.Task] = None     # the long-running manager task
_filesystem_ready: Optional[asyncio.Event] = None           # set once initial FS MCP connection attempt finishes
memory_store: Any = None
mcp_manager: Optional[MCPClientManager] = None
messaging_manager: Any = None  # MessagingManager (set in lifespan if enabled)
schedule_manager: Any = None   # ScheduleManager (set in lifespan)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global exit_stack
    print("Starting Multi-Agent Orchestrator...")
    exit_stack = AsyncExitStack()
    
    if _settings.get("coding_agent_enabled"):
        try:
            from services.code_indexer import init_cocoindex
            init_cocoindex()
        except Exception as e:
            print(f"Failed to init cocoindex: {e}")

    try:
        for agent_name, script_path in TOOLS_LIST.items():
            print(f"Connecting to {agent_name} agent at {script_path}...")

            # Prepare environment with PYTHONPATH specifically pointing to backend root
            # This is crucial so agents can assume 'services' and 'core' are importable
            env = os.environ.copy()
            env["PYTHONPATH"] = str(BACKEND_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

            server_params = StdioServerParameters(
                command=sys.executable,
                args=[script_path],
                env=env
            )

            inner_stack = AsyncExitStack()
            try:
                read, write = await inner_stack.enter_async_context(stdio_client(server_params))
                session = await inner_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                await exit_stack.enter_async_context(inner_stack)

                agent_sessions[agent_name] = session

                # Register tools
                tools = await session.list_tools()
                for tool in tools.tools:
                    tool_router[tool.name] = (agent_name, tool.name)
                    print(f"  Registered tool: {tool.name} -> {agent_name}")
            except BaseException as e:
                print(f"  Warning: Failed to connect agent '{agent_name}': {e}")
                try:
                    await inner_stack.aclose()
                except BaseException:
                    pass

        # --- Start Filesystem MCP Manager Task ---
        # The Filesystem MCP is managed by a dedicated asyncio task so that its
        # AsyncExitStack is always opened and closed by the SAME task.  anyio
        # cancel scopes are task-local; calling aclose() from an HTTP request
        # handler (a different task) caused a cancel-scope violation that
        # propagated a CancelledError to the lifespan and tore down ALL sessions.
        global _filesystem_restart_queue, _filesystem_manager_task, _filesystem_ready
        _filesystem_restart_queue = asyncio.Queue()
        _filesystem_ready = asyncio.Event()
        _filesystem_manager_task = asyncio.create_task(_filesystem_mcp_manager())
        await _filesystem_ready.wait()  # wait for initial connection attempt before continuing

        # --- Initialize Native MCP Servers ---
        for mcp_cfg in _build_native_mcp_servers():
            mcp_name = mcp_cfg["name"]

            if mcp_name == "Filesystem":
                continue  # already handled by _filesystem_mcp_manager task above

            cmd = mcp_cfg["command"]

            # Check that the command binary is available
            if not shutil.which(cmd):
                print(f"Warning: '{cmd}' not found — skipping native MCP server '{mcp_name}'.")
                continue

            print(f"Connecting native MCP server '{mcp_name}'...")
            try:
                env = os.environ.copy()
                # Don't leak the backend's PYTHONPATH into isolated external MCP processes —
                # it causes their `from main import main` to resolve to backend/main.py instead.
                env.pop("PYTHONPATH", None)
                # Merge any extra env vars from the config (e.g. OAuth credentials)
                env.update(mcp_cfg.get("env", {}))

                server_params = StdioServerParameters(
                    command=cmd,
                    args=mcp_cfg["args"],
                    env=env,
                    cwd=str(Path.home()),
                )
                read, write = await exit_stack.enter_async_context(stdio_client(server_params))
                session = await exit_stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()

                agent_sessions[mcp_name] = session

                tools = await session.list_tools()
                for tool in tools.tools:
                    tool_router[tool.name] = (mcp_name, tool.name)
                    print(f"  Registered tool: {tool.name} -> {mcp_name}")
            except Exception as e:
                print(f"  Failed to connect native MCP server '{mcp_name}': {e}")

        # --- Initialize External MCP Servers ---
        global mcp_manager
        mcp_manager = MCPClientManager(exit_stack)
        print("Connecting to external MCP servers...")
        external_sessions = await mcp_manager.connect_all()
        
        for name, session in external_sessions.items():
            # Prefix to avoid collision with internal agents
            agent_key = f"ext_mcp_{name}"
            agent_sessions[agent_key] = session
            print(f"Connected external MCP server: {name}")
            
            try:
                tools = await session.list_tools()
                print(f"  MCP Server '{name}' returned {len(tools.tools)} tools.")
                for tool in tools.tools:
                    tool_router[f"{name}__{tool.name}"] = (agent_key, tool.name)
                    print(f"  Registered external tool: {name}__{tool.name} -> {agent_key}")
            except Exception as e:
                print(f"  Error listing tools for {name}: {e}")
                import traceback
                traceback.print_exc()
                
        # Initialize Memory Store
        if MemoryStore:
            print("Initializing Memory Store...")
            global memory_store
            memory_store = _init_memory_store(load_settings())
            # Clear the legacy chat_history ChromaDB collection — chat turns are
            # now persisted as JSON files. The collection may still contain stale
            # data from before this refactor; clear it once at startup.
            if memory_store:
                try:
                    memory_store.clear_memory()
                    print("INFO: Cleared legacy ChromaDB chat_history collection (chat history is now JSON-persisted).")
                except Exception as _clr_err:
                    print(f"WARNING: Could not clear ChromaDB chat_history: {_clr_err}")

        print("All agents connected.")

        # Expose server module on app.state for orchestration routes
        import core.server as _self_module
        app.state.server_module = _self_module

        # --- Seed the native builder orchestration (idempotent) ---
        try:
            from core.native_builder import seed_native_builder
            seed_result = seed_native_builder()
            if (seed_result["agents_added"] or seed_result["agents_updated"]
                    or seed_result.get("agents_removed")
                    or seed_result["orchestration"] != "unchanged"):
                print(f"Native builder seeded: {seed_result}")
        except Exception as e:
            print(f"Warning: Failed to seed native builder: {e}")

        # --- Sweep zombie orchestration runs (stale "running" from prior server crash) ---
        try:
            from core.orchestration.state import SharedState
            zombie_runs = [r for r in SharedState.list_runs(limit=200) if r.get("status") == "running"]
            if zombie_runs:
                now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                for zr in zombie_runs:
                    try:
                        restored = SharedState.restore(zr["run_id"])
                        restored.run.status = "failed"
                        restored.run.ended_at = now_str
                        restored.checkpoint()
                    except Exception:
                        pass
                print(f"Swept {len(zombie_runs)} zombie orchestration run(s) (marked failed)")
        except Exception as e:
            print(f"Warning: Zombie run sweep failed: {e}")

        # --- Initialize Messaging Manager (if enabled) ---
        if _settings.get("messaging_enabled", False):
            try:
                from core.messaging.manager import MessagingManager
                global messaging_manager
                messaging_manager = MessagingManager(server_module=_self_module)
                await messaging_manager.start_all()
                app.state.messaging_manager = messaging_manager
                print("Messaging manager started.")
            except Exception as e:
                print(f"Warning: Failed to start messaging manager: {e}")
        else:
            app.state.messaging_manager = None

        # --- Initialize Schedule Manager ---
        try:
            from core.scheduler import ScheduleManager
            global schedule_manager
            schedule_manager = ScheduleManager()
            await schedule_manager.start(server_module=_self_module)
            app.state.schedule_manager = schedule_manager
            print("Schedule manager started.")
        except Exception as e:
            print(f"Warning: Failed to start schedule manager: {e}")
            app.state.schedule_manager = None

        yield
        
    except Exception as e:
        print(f"Error starting agents: {e}")
        yield
    finally:
        print("Shutting down agents...")
        if messaging_manager:
            try:
                await messaging_manager.stop_all()
            except Exception as e:
                print(f"Warning: Messaging manager shutdown error: {e}")
        if schedule_manager:
            try:
                await schedule_manager.stop()
            except Exception as e:
                print(f"Warning: Schedule manager shutdown error: {e}")
        if _filesystem_manager_task and not _filesystem_manager_task.done():
            _filesystem_manager_task.cancel()
            try:
                await _filesystem_manager_task
            except (asyncio.CancelledError, Exception):
                pass
        if exit_stack:
            try:
                await exit_stack.aclose()
            except BaseException as e:
                print(f"Warning: Error during shutdown cleanup: {e}")

app = FastAPI(lifespan=lifespan)

_frontend_port = os.getenv("SYNAPSE_FRONTEND_PORT", "3000")
_backend_port_cors = os.getenv("SYNAPSE_BACKEND_PORT", "8765")
_cors_defaults = {
    f"http://localhost:{_frontend_port}",
    "http://localhost:3000",
    "http://localhost:5173",
    f"http://localhost:{_backend_port_cors}",
}
CORS_ORIGINS = os.getenv("CORS_ORIGINS", ",".join(_cors_defaults)).split(",")

class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    """
    Chrome's Private Network Access (PNA) protection blocks external OAuth
    providers from redirecting back to localhost unless the server explicitly
    opts in via the Access-Control-Allow-Private-Network header.

    This middleware:
    1. Responds to Chrome's PNA preflight (OPTIONS with
       Access-Control-Request-Private-Network: true) with a 200 + the
       Allow-Private-Network header so the real request is permitted.
    2. Injects Access-Control-Allow-Private-Network: true on every response
       so Chrome allows the subsequent navigation.
    """
    async def dispatch(self, request: Request, call_next):
        # PNA preflight: Chrome sends OPTIONS with this header before the real
        # document navigation from a public origin → localhost.
        if (
            request.method == "OPTIONS"
            and request.headers.get("access-control-request-private-network") == "true"
        ):
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
                    "Access-Control-Allow-Private-Network": "true",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                },
            )
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


app.add_middleware(PrivateNetworkAccessMiddleware)
app.add_middleware(InternalTokenMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TimingMiddleware)

# --- Include Route Routers ---
app.include_router(auth_router)
app.include_router(settings_router)
app.include_router(agents_router)
app.include_router(tools_router)
app.include_router(n8n_router)
app.include_router(data_router)
app.include_router(chat_router)
app.include_router(repos_router)
app.include_router(db_configs_router)
app.include_router(orchestrations_router)
app.include_router(logs_router)
app.include_router(messaging_router)
app.include_router(sessions_router)
app.include_router(usage_router)
app.include_router(schedules_router)
app.include_router(profiling_router)
app.include_router(import_export_router)
app.include_router(vault_router)
app.include_router(builder_router)
app.include_router(api_keys_router)
app.include_router(api_v1_router, prefix="/api/v1")

if __name__ == "__main__":
    import uvicorn
    _port = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=_port)
