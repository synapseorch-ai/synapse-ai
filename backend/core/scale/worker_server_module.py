"""
Minimal stub of core.server's server_module interface for use inside worker processes.
Workers don't have a running FastAPI app or interactive MCP session setup,
so this builds a lightweight equivalent that satisfies OrchestrationEngine.
"""
import asyncio
import platform
import sys
from pathlib import Path

_IS_WIN = platform.system() == "Windows"
_NPX_CMD = "npx.cmd" if _IS_WIN else "npx"


class WorkerServerModule:
    """
    Satisfies the server_module interface expected by OrchestrationEngine and
    step executors. Only connects to Python-native tools and locally-available
    MCP servers; silently skips anything that can't connect.
    """

    def __init__(self):
        self.agent_sessions: dict = {}     # mcp_server_name -> ClientSession
        self.tool_router: dict = {}        # tool_name -> session
        self.memory_store = None           # workers don't maintain long-term memory store
        self.mcp_disabled: list[str] = []  # names of MCP servers that failed to connect
        self._exit_stack = None

    @classmethod
    async def build(
        cls,
        disabled_mcp_names: list[str] | None = None,
    ) -> "WorkerServerModule":
        """
        Connect to available tools and MCP servers.
        MCP servers listed in disabled_mcp_names are skipped without attempting.
        Any MCP server that raises a connection error is added to mcp_disabled.
        """
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from datetime import timedelta

        instance = cls()
        disabled = set(disabled_mcp_names or [])
        exit_stack = AsyncExitStack()
        instance._exit_stack = exit_stack

        _SESSION_READ_TIMEOUT = timedelta(seconds=60)

        # --- Python-native MCP tools (always available on workers) ---
        TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
        BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

        native_servers = _get_native_mcp_servers(TOOLS_DIR, BACKEND_ROOT)

        for server_name, params in native_servers.items():
            if server_name in disabled:
                instance.mcp_disabled.append(server_name)
                continue
            try:
                read, write = await exit_stack.enter_async_context(
                    stdio_client(params)
                )
                session = await exit_stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()
                instance.agent_sessions[server_name] = session
                # Register tools into router: tool_name -> (server_name, actual_tool_name)
                # matches the interface expected by react_engine.py
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    instance.tool_router[tool.name] = (server_name, tool.name)
            except Exception as e:
                print(
                    f"[worker_server_module] Skipping MCP server '{server_name}': {e}",
                    flush=True,
                )
                instance.mcp_disabled.append(server_name)

        # --- User-configured MCP servers (from Postgres or mcp_servers.json) ---
        # Browser Automation (playwright) requires a local browser install — skip in workers.
        _BROWSER_MCP_NAMES = {"Browser Automation", "browser", "playwright"}
        _BROWSER_MCP_PKGS = {"@playwright/mcp", "playwright-mcp"}

        from core.scale.context import resolve_mcp_servers
        user_mcp_configs = await resolve_mcp_servers()
        for cfg in user_mcp_configs:
            server_name = cfg.get("name", "")
            if not server_name or server_name in disabled:
                continue
            if not cfg.get("enabled", True):
                continue
            # Skip native servers already connected above
            if server_name in instance.agent_sessions:
                continue
            # Skip browser/playwright MCP — requires a local browser install
            if server_name in _BROWSER_MCP_NAMES:
                instance.mcp_disabled.append(server_name)
                continue
            args_str = " ".join(str(a) for a in cfg.get("args", []))
            if any(pkg in args_str for pkg in _BROWSER_MCP_PKGS):
                instance.mcp_disabled.append(server_name)
                continue
            server_type = cfg.get("server_type", "stdio")
            try:
                if server_type == "remote":
                    params = _build_remote_mcp_params(cfg)
                    if params is None:
                        instance.mcp_disabled.append(server_name)
                        continue
                    from mcp.client.sse import sse_client
                    read, write = await exit_stack.enter_async_context(
                        sse_client(params["url"], headers=params.get("headers", {}))
                    )
                else:
                    params = _build_stdio_mcp_params(cfg)
                    if params is None:
                        instance.mcp_disabled.append(server_name)
                        continue
                    read, write = await exit_stack.enter_async_context(
                        stdio_client(params)
                    )
                session = await exit_stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()
                instance.agent_sessions[server_name] = session
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    instance.tool_router[tool.name] = (server_name, tool.name)
                print(f"[worker_server_module] Connected user MCP '{server_name}'", flush=True)
            except Exception as e:
                print(
                    f"[worker_server_module] Skipping user MCP '{server_name}': {e}",
                    flush=True,
                )
                instance.mcp_disabled.append(server_name)

        # Memory store: try Postgres-backed if SCALE_POSTGRES_URL is set
        try:
            import os
            pg_url = os.getenv("SCALE_POSTGRES_URL", "")
            if pg_url:
                from core.memory import MemoryStore
                instance.memory_store = MemoryStore()
        except Exception:
            pass

        return instance

    async def close(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass


def _get_native_mcp_servers(tools_dir: Path, backend_root: Path) -> dict:
    """Return native MCP server configs for the worker process.

    Uses core.tools_registry as the single source of truth for tool filenames.
    WORKER_NATIVE_TOOLS / WORKER_NPX_TOOLS control what runs in workers vs. not.
    """
    import os
    from pathlib import Path as _Path
    from mcp import StdioServerParameters
    from core.tools_registry import ALL_NATIVE_TOOLS, WORKER_NATIVE_TOOLS, WORKER_NPX_TOOLS

    # Tool subprocesses need the backend root on PYTHONPATH so they can do
    # `from core.config import ...` — same as when the main server spawns them.
    tool_env = os.environ.copy()
    existing_pp = tool_env.get("PYTHONPATH", "")
    backend_root_str = str(backend_root)
    tool_env["PYTHONPATH"] = f"{backend_root_str}{os.pathsep}{existing_pp}" if existing_pp else backend_root_str

    servers = {}

    # Python-native tools (subset safe for headless worker processes)
    for name in WORKER_NATIVE_TOOLS:
        script = _Path(ALL_NATIVE_TOOLS[name])
        if script.exists():
            servers[name] = StdioServerParameters(command=sys.executable, args=[str(script)], env=tool_env)
        else:
            print(f"[worker_server_module] Tool script not found, skipping '{name}': {script}", flush=True)

    # npx-based tools available to workers
    for name, args in WORKER_NPX_TOOLS.items():
        servers[name] = StdioServerParameters(command=_NPX_CMD, args=args)

    # Filesystem MCP (Node.js) — point to SYNAPSE_DATA_DIR
    data_dir = os.getenv("SYNAPSE_DATA_DIR", str(backend_root / "data"))
    worker_fs_paths = os.getenv("WORKER_FILESYSTEM_PATHS", data_dir)
    filesystem_paths = [p.strip() for p in worker_fs_paths.split(",") if p.strip()]
    if filesystem_paths:
        servers["filesystem"] = StdioServerParameters(
            command=_NPX_CMD,
            args=["-y", "@modelcontextprotocol/server-filesystem"] + filesystem_paths,
        )

    return servers


def _build_stdio_mcp_params(cfg: dict):
    """Build StdioServerParameters from a saved mcp_servers.json config dict."""
    import shlex
    from mcp import StdioServerParameters

    command = cfg.get("command", "")
    if not command:
        return None
    args_raw = cfg.get("args", [])
    # Support both list and space-separated string for args
    if isinstance(args_raw, str):
        args_raw = shlex.split(args_raw)
    env = cfg.get("env") or {}
    return StdioServerParameters(command=command, args=list(args_raw), env=env or None)


def _build_remote_mcp_params(cfg: dict) -> dict | None:
    """Build connection params dict for an SSE/HTTP MCP server."""
    url = cfg.get("url", "")
    if not url:
        return None
    # Token was stripped during sync; if present (JSON fallback path), include it
    token = cfg.get("token", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return {"url": url, "headers": headers}
