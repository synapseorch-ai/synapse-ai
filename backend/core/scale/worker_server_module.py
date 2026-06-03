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
    """Return the same native MCP server configs as the main server.py lifespan."""
    import os
    from core.config import DATA_DIR
    from mcp import StdioServerParameters

    servers = {}

    # Time server
    time_script = tools_dir / "time_server.py"
    if time_script.exists():
        servers["time"] = StdioServerParameters(
            command=sys.executable,
            args=[str(time_script)],
        )

    # SQL server
    sql_script = tools_dir / "sql_server.py"
    if sql_script.exists():
        servers["sql"] = StdioServerParameters(
            command=sys.executable,
            args=[str(sql_script)],
        )

    # Personal details server
    personal_script = tools_dir / "personal_details_server.py"
    if personal_script.exists():
        servers["personal_details"] = StdioServerParameters(
            command=sys.executable,
            args=[str(personal_script)],
        )

    # Collect data server
    collect_script = tools_dir / "collect_data_server.py"
    if collect_script.exists():
        servers["collect_data"] = StdioServerParameters(
            command=sys.executable,
            args=[str(collect_script)],
        )

    # PDF parser
    pdf_script = tools_dir / "pdf_parser_server.py"
    if pdf_script.exists():
        servers["pdf_parser"] = StdioServerParameters(
            command=sys.executable,
            args=[str(pdf_script)],
        )

    # XLSX parser
    xlsx_script = tools_dir / "xlsx_parser_server.py"
    if xlsx_script.exists():
        servers["xlsx_parser"] = StdioServerParameters(
            command=sys.executable,
            args=[str(xlsx_script)],
        )

    # Vault sandbox
    vault_script = tools_dir / "vault_sandbox_server.py"
    if vault_script.exists():
        servers["vault_sandbox"] = StdioServerParameters(
            command=sys.executable,
            args=[str(vault_script)],
        )

    # Web scraper
    web_scraper_script = tools_dir / "web_scraper_server.py"
    if web_scraper_script.exists():
        servers["web_scraper"] = StdioServerParameters(
            command=sys.executable,
            args=[str(web_scraper_script)],
        )

    # Bash server
    bash_script = tools_dir / "bash_server.py"
    if bash_script.exists():
        servers["bash"] = StdioServerParameters(
            command=sys.executable,
            args=[str(bash_script)],
        )

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
