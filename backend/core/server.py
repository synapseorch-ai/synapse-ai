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
from core.config import MCP_SESSION_READ_TIMEOUT
_SESSION_READ_TIMEOUT = timedelta(seconds=MCP_SESSION_READ_TIMEOUT)

try:
    from core.memory import MemoryStore
except ImportError:
    print("Warning: MemoryStore dependencies not found. Memory disabled.", file=sys.stderr)
    MemoryStore = None

from core.mcp_client import MCPClientManager
from core.tool_router import ToolRouter
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
from core.routes.evals import router as evals_router
from core.routes.skills import router as skills_router
from core.routes.widget import router as widget_router
from core.routes.api_v1 import router as api_v1_router
from core.routes.api_v2 import router as api_v2_router
from core.routes.scale import router as scale_router
from core.routes.notifications import router as notifications_router
from core.profiling import TimingMiddleware
from core.internal_auth import InternalTokenMiddleware

# Configuration
OLLAMA_MODEL = "llama3"

# On Windows, npx is a .cmd file and must be invoked as "npx.cmd" for subprocess
_IS_WIN = platform.system() == "Windows"
_NPX_CMD = "npx.cmd" if _IS_WIN else "npx"

# Agent Configuration
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = BACKEND_ROOT.parent

# Settings are deliberately NOT read at import time. There is no store, no
# event loop and no settings provider installed yet at import, so a module-level
# load_settings() would capture the shipped defaults and hold them for the
# process's life — silently discarding anything the user had configured. Every
# reader below calls load_settings() itself, after lifespan has bound them.

# ollama_base_url is NOT copied into os.environ here. Writing one tenant's
# setting into the process environment made it the default for every tenant the
# process later served. llm_providers._ollama_base_url() reads the setting it is
# given and falls back to the OLLAMA_BASE_URL env var as a deployment default.

from core.tools_registry import ALL_NATIVE_TOOLS
TOOLS_LIST = dict(ALL_NATIVE_TOOLS)

async def _get_repo_paths() -> list[str]:
    """Load repo paths for the filesystem MCP server's permissions.

    Read from the store; this was a fifth reader of `DATA_DIR/repos.json`
    after repos moved, so the filesystem server's roots were whatever the
    repos looked like before the migration.
    """
    from core.store import collections

    try:
        return [
            r["path"] for r in await collections.load("repos")
            if r.get("path") and os.path.isdir(r["path"])
        ]
    except Exception as e:
        print(f"Warning: Could not load repo paths: {e}")
        return []


async def _get_google_oauth_env() -> dict[str, str]:
    """Build workspace-mcp's environment from the stored Google credentials.

    Also materialises the credential directory the subprocess reads, which is
    process scratch rebuilt from the store rather than durable state — see
    services/google.google_credentials_dir.
    """
    from services import google as google_svc

    client = await google_svc.load_client_config()
    if not client:
        return {}

    # Refresh the Google token (if expired-but-refreshable) before launching
    # workspace-mcp so the subprocess inherits valid creds and doesn't emit
    # "ACTION REQUIRED: Google Authentication Needed" on the first tool call.
    try:
        await google_svc.get_google_credentials()
    except Exception as e:
        print(f"Warning: Token refresh at startup failed: {e}")

    try:
        installed = client.get("installed", client.get("web", {}))
        client_id = installed.get("client_id", "")
        client_secret = installed.get("client_secret", "")
        if not (client_id and client_secret):
            return {}

        directory = await google_svc.materialise_mcp_dir()
        if directory is None:
            return {}

        env = {
            "GOOGLE_OAUTH_CLIENT_ID": client_id,
            "GOOGLE_OAUTH_CLIENT_SECRET": client_secret,
            "OAUTHLIB_INSECURE_TRANSPORT": "1",  # allow http:// redirect URIs for localhost
            "GOOGLE_MCP_CREDENTIALS_DIR": str(directory.resolve()),
        }

        # So workspace-mcp can skip the email prompt. One implementation of
        # "which Google account is this" now, rather than the four that had
        # drifted apart — this one read token_data["token"] as a JWT, which it
        # never is, so it never produced an email at all.
        token_data = await google_svc.load_token() or {}
        email = google_svc._email_from_token_data(token_data)
        if email:
            env["USER_GOOGLE_EMAIL"] = email

        return env
    except Exception as e:
        print(f"Warning: Could not read Google OAuth credentials: {e}")
    return {}


async def _build_native_mcp_servers() -> list[dict]:
    """
    Build the list of native MCP servers to connect at startup.
    Returns a list of dicts with keys: name, command, args, env (optional).
    """
    servers = []

    # --- Filesystem MCP Server ---
    repo_paths = await _get_repo_paths()
    # The tenant's vault, from the blob store. Was DATA_DIR/"vault", which the
    # tenant-scoped storage change left pointing at a directory the vault no
    # longer writes to — so the filesystem server's one guaranteed root was
    # stale content.
    from core.vault import _vault_root
    vault_path = str(_vault_root())
    # Always start with vault; include any configured repo paths on top.
    fs_paths = repo_paths + [vault_path]
    # Also include user-configured directories from General Settings ("Allowed
    # Directories"). Read fresh so a restart picks up edits without a backend
    # reboot. Filter to existing dirs and de-duplicate against paths we already
    # have so the MCP server doesn't see the same root twice.
    extra_dirs = [
        d for d in load_settings().get("bash_allowed_dirs", [])
        if d and os.path.isdir(d)
    ]
    seen = {os.path.realpath(p) for p in fs_paths}
    for d in extra_dirs:
        rp = os.path.realpath(d)
        if rp not in seen:
            fs_paths.append(d)
            seen.add(rp)
    if not repo_paths:
        print("Warning: No repos configured — starting filesystem MCP server with vault access only.")
    servers.append({
        "name": "Filesystem",
        "command": _NPX_CMD,
        "args": ["-y", "@modelcontextprotocol/server-filesystem"] + fs_paths,
    })

    # --- Playwright MCP Server (browser automation) ---
    _settings = load_settings()
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
            # Was the relative string "data/vault/playwright", resolved
            # against the npx subprocess's cwd rather than anything the engine
            # controls — so screenshots landed wherever the server happened to
            # be started from.
            "args": ["-y", "@playwright/mcp", "--browser", "chrome",
                     "--output-dir", str(_vault_root() / "playwright")],
            "env": env_dict,
        })

    # --- Google Workspace MCP Server (Gmail, Drive, Calendar) ---
    google_env = await _get_google_oauth_env()
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
    # The knowledge graph this server accumulates is durable tenant state — it
    # is what the user told it to remember — so it goes beside the rest of the
    # tenant's content rather than into scratch.
    from core.runtime_dirs import tenant_dir
    memory_file_path = tenant_dir("memory") / "memory.jsonl"
    servers.append({
        "name": "Memory",
        "command": _NPX_CMD,
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {"MEMORY_FILE_PATH": str(memory_file_path)},
    })

    # Sequential Thinking used to be spawned here as an npx server. It is now
    # the native task list (core/task_list.py) — engine-owned, journaled,
    # resume-safe — and the old name survives as a dispatch alias only.

    return servers


async def _connect_filesystem_mcp(label: str = "started") -> None:
    """
    Connect the Filesystem MCP subprocess and register its tools.
    MUST only be called from _filesystem_mcp_manager — anyio cancel scopes
    must be entered and exited by the same task.
    """
    global _filesystem_stack

    fs_cfg = next((c for c in await _build_native_mcp_servers() if c["name"] == "Filesystem"), None)
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
            # Beside the vault, never over it (F79): its `read_file` and
            # `search_files` keep their bare names.
            tool_router.register_beside("Filesystem", tool.name)
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
tool_router: ToolRouter = ToolRouter()           # {server}__{tool} -> (session_name, actual_tool_name)

#: What each session advertised, cached after the first successful list_tools().
#: Lives beside the sessions it describes rather than in core/tools.py, because
#: that made it process-global: a worker serving many tenants would have served
#: them all from whichever tenant's server answered first.
_session_tools: dict[str, list] = {}
exit_stack: Optional[AsyncExitStack] = None
_filesystem_stack: Optional[AsyncExitStack] = None          # owned exclusively by _filesystem_mcp_manager
_filesystem_restart_queue: Optional[asyncio.Queue] = None   # route handlers put Futures here to request restarts
_filesystem_manager_task: Optional[asyncio.Task] = None     # the long-running manager task
_filesystem_ready: Optional[asyncio.Event] = None           # set once initial FS MCP connection attempt finishes
memory_store: Any = None
mcp_manager: Optional[MCPClientManager] = None
messaging_manager: Any = None  # MessagingManager (set in lifespan if enabled)
schedule_manager: Any = None   # ScheduleManager (set in lifespan)

def _log_security_posture() -> None:
    """Warn loudly when the backend starts with the exact weak-default posture
    that GHSA-3j67-x3j8-r32x exploits: no internal token, login disabled, and
    stdio MCP registration allowed. Docker images auto-generate a token, so this
    normally only fires for a misconfigured bare-metal deployment."""
    try:
        from core.mcp_client import stdio_mcp_allowed
        no_token = not os.getenv("SYNAPSE_INTERNAL_TOKEN", "")
        login_off = not load_settings().get("login_enabled")
        if no_token and login_off and stdio_mcp_allowed():
            print(
                "[SECURITY] No SYNAPSE_INTERNAL_TOKEN set and login is disabled. "
                "Remote /api/* access is refused (loopback-only), but if this backend "
                "is reachable through the frontend without login, stdio MCP registration "
                "can execute local commands. Enable login and/or set allow_stdio_mcp=false "
                "for any network-reachable deployment. See GHSA-3j67-x3j8-r32x."
            )
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global exit_stack
    print("Starting Multi-Agent Orchestrator...")
    exit_stack = AsyncExitStack()

    # Bring a pre-store install's JSON files across, once, on first boot after
    # the upgrade. No-ops when the store already holds content, so this is safe
    # on every start. Deliberately not a command the user has to run: an upgrade
    # step that can be missed becomes a support thread for everyone who misses it.
    try:
        from core.store.importer import import_legacy_data_if_present
        await import_legacy_data_if_present()
    except Exception as e:
        print(f"Warning: legacy data import skipped: {e}")

    # And the logs, which live in `backend/logs/` and so were never part of the
    # DATA_DIR import above. Its own call because its triggers are different in
    # both directions: an install that already upgraded has `data.migrated/`,
    # and by the time anyone notices their history is missing the store is not
    # empty. See `core/store/log_importer.py`.
    try:
        from core.store.log_importer import import_legacy_logs_if_present
        await import_legacy_logs_if_present()
    except Exception as e:
        print(f"Warning: legacy log import skipped: {e}")

    # Settings come from the store from here on. Order matters: after the
    # importer, which is what puts an upgrading install's settings.json into the
    # store, and before _build_native_mcp_servers() and _init_memory_store()
    # below, both of which read settings and would otherwise see bare defaults.
    from core import settings_runtime
    settings_runtime.install_provider()
    await settings_runtime.refresh()

    # The blob store, once the settings that can name a bucket are readable and
    # before anything below writes a blob (F57).
    try:
        from core.storage import install_configured_blob_store
        if install_configured_blob_store():
            print("Blob store: S3")
    except Exception as e:
        print(f"Warning: S3 blob store not installed, using the local directory: {e}")

    # The rate card. Adds only the models the table is missing, so an edited
    # price is never reverted by a restart — and loads the snapshot that
    # calculate_cost() reads synchronously on the per-turn path.
    try:
        from core.usage_tracker import seed_pricing_table
        await seed_pricing_table()
    except Exception as e:
        print(f"Warning: model pricing seed skipped: {e}")

    # After the refresh, not before: this reads login_enabled and asks whether
    # stdio MCP is allowed, and both answers are meaningless while settings are
    # still the shipped defaults.
    _log_security_posture()


    if load_settings().get("coding_agent_enabled"):
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
                    key = tool_router.register(agent_name, tool.name)
                    print(f"  Registered tool: {key} -> {agent_name}")
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
        for mcp_cfg in await _build_native_mcp_servers():
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
                    key = tool_router.register(mcp_name, tool.name)
                    print(f"  Registered tool: {key} -> {mcp_name}")
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
                    key = tool_router.register(
                        name, tool.name, session_key=agent_key, alias=False
                    )
                    print(f"  Registered external tool: {key} -> {agent_key}")
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
            seed_result = await seed_native_builder()
            if (seed_result["agents_added"] or seed_result["agents_updated"]
                    or seed_result.get("agents_removed")
                    or seed_result["orchestration"] != "unchanged"):
                print(f"Native builder seeded: {seed_result}")
        except Exception as e:
            print(f"Warning: Failed to seed native builder: {e}")

        # --- Sweep zombie orchestration runs (stale "running" from prior server crash) ---
        try:
            from core.scale.heartbeat import sweep_interrupted_runs
            swept = await sweep_interrupted_runs()
            if swept:
                print(f"Swept {len(swept)} zombie orchestration run(s) (marked failed)")
        except Exception as e:
            print(f"Warning: Zombie run sweep failed: {e}")

        # --- Notification hub: observe run events + surface missed pauses ---
        try:
            import core.server as _server_mod
            from core.notifications import hub as _notification_hub
            from core.orchestration.runner import event_observers as _event_observers
            _notification_hub.reload()  # built at import, before the blob store
            _notification_hub.configure(_server_mod)
            if _notification_hub.observe_run_event not in _event_observers:
                _event_observers.append(_notification_hub.observe_run_event)
            await _notification_hub.reconstruct_missed()
        except Exception as e:
            print(f"Warning: Notification hub setup failed: {e}")

        # --- Initialize Telemetry (OpenTelemetry + Prometheus) ---
        try:
            from core.scale.config import get_scale_config as _get_scale_cfg
            _tcfg = _get_scale_cfg()
            if _tcfg.otlp_endpoint:
                from core.scale.telemetry import setup_telemetry
                setup_telemetry("synapse-api", otlp_endpoint=_tcfg.otlp_endpoint)
        except Exception as e:
            print(f"Warning: Telemetry setup failed: {e}")

        # --- Initialize Scale Layer (Redis + Postgres + ARQ) ---
        app.state.redis = None
        app.state.arq_redis = None
        app.state.pg_engine = None
        app.state.pg_session_factory = None
        try:
            from core.scale.config import get_scale_config
            _scale_cfg = get_scale_config()
            if _scale_cfg.scale_mode:
                from arq import create_pool as arq_create_pool
                from arq.connections import RedisSettings as ArqRedisSettings
                from core.scale.db import build_engine, build_session_factory, init_db

                # Redis client for Pub/Sub and Stream reading
                from core.scale.pubsub import get_redis_client
                _redis = get_redis_client(_scale_cfg.redis_url)
                app.state.redis = _redis

                # ARQ Redis pool for enqueueing jobs
                _arq_settings = ArqRedisSettings.from_dsn(
                    _scale_cfg.redis_url.replace("redis+cluster://", "redis://").split(",")[0]
                )
                _arq_redis = await arq_create_pool(_arq_settings)
                app.state.arq_redis = _arq_redis

                # Postgres engine + session factory
                _pg_engine = build_engine(
                    _scale_cfg.postgres_url,
                    pgbouncer_mode=_scale_cfg.pgbouncer_mode,
                )
                await init_db(_pg_engine)
                _pg_session_factory = build_session_factory(_pg_engine)
                app.state.pg_engine = _pg_engine
                app.state.pg_session_factory = _pg_session_factory

                # Background task: reap workers that crash without sending shutdown
                from core.scale.heartbeat import reap_stale_workers
                _reap_task = asyncio.create_task(
                    reap_stale_workers(_pg_session_factory),
                    name="reap_stale_workers",
                )
                app.state.reap_task = _reap_task

                print(f"Scale mode enabled: Redis={_scale_cfg.redis_url[:30]}... Postgres connected.")
            else:
                print("Scale mode disabled (no redis_url configured). Running in standalone mode.")
        except Exception as e:
            print(f"Warning: Failed to initialize scale layer: {e}")
            app.state.redis = None
            app.state.arq_redis = None
            app.state.pg_engine = None
            app.state.pg_session_factory = None

        # --- Initialize Messaging Manager (if enabled) ---
        if load_settings().get("messaging_enabled", False):
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

        # A human step that names a channel asks its question there (F54). The
        # sender reads `messaging_enabled` per send, so it is installed either way.
        try:
            import functools
            from core.messaging.manager import send_human_prompt
            from core.orchestration.human_prompts import set_human_prompt_sender
            set_human_prompt_sender(functools.partial(
                send_human_prompt, manager=getattr(app.state, "messaging_manager", None),
            ))
        except Exception as e:
            print(f"Warning: human-step channel prompts disabled: {e}")

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
        # Scale layer cleanup
        try:
            _reap = getattr(app.state, "reap_task", None)
            if _reap and not _reap.done():
                _reap.cancel()
                try:
                    await _reap
                except (asyncio.CancelledError, Exception):
                    pass
            _arq = getattr(app.state, "arq_redis", None)
            if _arq:
                await _arq.close()
            _redis = getattr(app.state, "redis", None)
            if _redis:
                await _redis.aclose()
            _pg = getattr(app.state, "pg_engine", None)
            if _pg:
                await _pg.dispose()
        except Exception as e:
            print(f"Warning: Scale layer shutdown error: {e}")
        if exit_stack:
            try:
                await exit_stack.aclose()
            except BaseException as e:
                print(f"Warning: Error during shutdown cleanup: {e}")

# One call per process: the engine's runtime modules log through the `core`
# tree (core/log.py). SYNAPSE_LOG_LEVEL=DEBUG turns the step-by-step
# narration back on.
from core.log import configure as _configure_logging
_configure_logging()

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


# Installed here rather than in lifespan: installing is only setting a function
# pointer — no I/O, no event loop, no store — and doing it at app construction
# means the settings a request reads and the settings a request writes are the
# same store even when the app is driven without its lifespan, as tests do.
# The *refresh* still belongs in lifespan, after the legacy import.
from core import settings_runtime as _settings_runtime

_settings_runtime.install_provider()


@app.middleware("http")
async def bind_settings(request, call_next):
    """Bind the request's settings so `load_settings()` can stay synchronous.

    This is not a read per request: `bind()` only touches the store when the
    cached snapshot has aged past SYNAPSE_SETTINGS_TTL. In a single-process
    install the write path invalidates the cache directly, so the TTL never
    fires anything; it exists for deployments where another replica can write
    the row. Binding per request is also what gives an embedded multi-tenant
    host its per-request isolation for free.
    """
    from core import settings_runtime

    token = await settings_runtime.bind()
    try:
        return await call_next(request)
    finally:
        settings_runtime.reset(token)


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
app.include_router(skills_router)
app.include_router(evals_router)
app.include_router(widget_router)
app.include_router(api_v1_router, prefix="/api/v1")
app.include_router(api_v2_router, prefix="/api/v2")
app.include_router(scale_router, prefix="/api")
app.include_router(notifications_router)

if __name__ == "__main__":
    import uvicorn
    _port = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=_port)
