"""
Minimal stub of core.server's server_module interface for use inside worker processes.
Workers don't have a running FastAPI app or interactive MCP session setup,
so this builds a lightweight equivalent that satisfies OrchestrationEngine.

Two halves, because only one of them depends on whose job is running
---------------------------------------------------------------------
``build_shared()`` connects the native tool servers, which are the same for
everybody: `time`, `pdf_parser`, `web_scraper` and the rest advertise the same
tools whoever calls them. It runs once, at worker startup.

``build_for_tenant()`` connects what is *not* the same for everybody — the
Filesystem MCP server, which is rooted at the calling tenant's vault, and that
tenant's own MCP servers. It runs per tenant, behind the bounded pool in
`core/scale/mcp_pool.py`, and the module it returns presents the shared sessions
and its own as one set so nothing downstream has to know about the split.

This used to be a single ``build()`` called once at startup, whose result was
stored on the ARQ context and handed to every job. That meant one set of live
MCP sessions, opened from whatever configuration existed at boot, serving every
tenant — and a Filesystem server rooted at whichever vault `get_tenant()`
resolved to at startup, which is the default tenant's.
"""
import asyncio
import os
import platform
import sys
from pathlib import Path

from core.tool_router import ToolRouter

from core.log import get_logger

_log = get_logger(__name__)

#: How long one remote MCP server may take to hand back a usable transport.
#:
#: Bounds the handshake, which nothing else does — `ClientSession`'s
#: `read_timeout_seconds` starts applying only once a session exists. Thirty
#: seconds is generous for a TLS handshake plus an HTTP round trip and short
#: enough that a fleet worker is not held by a third party. Overridable because
#: a self-hosted install may sit behind a slow corporate proxy.
REMOTE_MCP_CONNECT_TIMEOUT = float(
    os.environ.get("SYNAPSE_MCP_CONNECT_TIMEOUT", "30")
)

_IS_WIN = platform.system() == "Windows"
_NPX_CMD = "npx.cmd" if _IS_WIN else "npx"

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_TOOLS_DIR = _BACKEND_ROOT / "tools"


class WorkerServerModule:
    """
    Satisfies the server_module interface expected by OrchestrationEngine and
    step executors. Only connects to Python-native tools and locally-available
    MCP servers; silently skips anything that can't connect.
    """

    def __init__(self):
        self.agent_sessions: dict = {}     # mcp_server_name -> ClientSession
        self.tool_router = ToolRouter()    # {server}__{tool} -> (session_name, tool_name)
        self._session_tools: dict = {}     # session name -> tools it advertised
        self.memory_store = None           # workers don't maintain long-term memory store
        self.mcp_disabled: list[str] = []  # names of MCP servers that failed to connect
        # A subset of `mcp_disabled`: OAuth servers whose token could not be
        # refreshed, so the only way back is the person authorising again. The
        # cloud's `definition["status"]` has documented `reauth_needed` since the
        # router was written and has never had anything able to set it.
        self.mcp_reauth_needed: list[str] = []
        # One handle per server this module opened, each with its own task and
        # its own AsyncExitStack. Borrowed sessions (folded in from `shared`) are
        # deliberately absent: `close()` closes what this module opened, never
        # what it referenced.
        self._servers: list = []

    @classmethod
    async def build_shared(
        cls,
        disabled_mcp_names: list[str] | None = None,
    ) -> "WorkerServerModule":
        """The tenant-independent native tool servers. Once per process.

        Everything here advertises the same tools to everybody, so one set is
        shared by every tenant the process serves. The Filesystem server is
        excluded: it is rooted at a vault, and a vault belongs to a tenant.
        """
        instance = cls()
        await instance._connect_native(
            _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="shared"),
            set(disabled_mcp_names or []),
        )
        instance._attach_memory_store()
        return instance

    @classmethod
    async def build_for_tenant(
        cls,
        shared: "WorkerServerModule | None" = None,
        disabled_mcp_names: list[str] | None = None,
    ) -> "WorkerServerModule":
        """The current tenant's MCP: the native servers it may use, and its own.

        `shared` is folded into the returned module's sessions and router so
        callers see one set. Its sessions are *referenced*, never owned — this
        module's `close()` only closes what this module opened.
        """
        instance = cls()
        disabled = set(disabled_mcp_names or [])

        # No vault hydration here. This used to call `get_vault().hydrate()` —
        # the tenant's *whole* vault, downloaded on the event loop, at every
        # cold build of a tenant's pool (idle TTL 900 s, per replica) — so that
        # a Filesystem MCP server rooted at the working copy would see a full
        # directory. That server is gone: `tools/vault_fs.py` reads the blob
        # store by key, and so does the `@[path]` reader (core/vault.py). The
        # working copy is `maybe_vault`'s write-through scratch and nothing
        # reads it; `hydrate()` stays on the backend for a caller that needs a
        # directory on purpose.

        if shared is not None:
            instance.agent_sessions.update(shared.agent_sessions)
            instance._session_tools.update(shared._session_tools)
            for key, value in shared.tool_router.items():
                instance.tool_router[key] = value
            instance.mcp_disabled.extend(shared.mcp_disabled)
            instance.memory_store = shared.memory_store

        # A build that does not finish still closes what it opened.
        #
        # Without this, a builder abandoned part-way — `mcp_pool` cancelling it
        # for exceeding its bound is the case that exists — leaves every server
        # already connected holding an open stack and a task parked on
        # `handle.closing`, owned by nobody and freed only by process exit. The
        # sessions are the expensive part, so leaking a set per wedged build is
        # how a fleet runs out of them.
        #
        # `BaseException`, because the case being cleaned up after is
        # `CancelledError`. Best-effort and re-raising: the reason the build
        # stopped is what the caller needs, never a failure from tidying up.
        try:
            # The tenant-scoped native servers (the vault among them, by key).
            await instance._connect_native(
                _get_native_mcp_servers(_TOOLS_DIR, _BACKEND_ROOT, scope="tenant"),
                disabled,
            )
            await instance._connect_user_mcp(disabled)
        except BaseException:
            try:
                await instance.close()
            except BaseException:
                pass
            raise

        if shared is None:
            instance._attach_memory_store()
        return instance

    # ── connection helpers ───────────────────────────────────────────────────

    async def _connect_native(self, native_servers: dict, disabled: set) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        from datetime import timedelta
        from core.config import MCP_SESSION_READ_TIMEOUT

        instance = self

        _SESSION_READ_TIMEOUT = timedelta(seconds=MCP_SESSION_READ_TIMEOUT)

        for server_name, params in native_servers.items():
            if server_name in disabled:
                instance.mcp_disabled.append(server_name)
                continue

            async def connect(stack, params=params):
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()
                return session

            handle = await _start_server(server_name, connect)
            if handle.error is not None:
                _log.debug(
                    f"[worker_server_module] Skipping MCP server '{server_name}': "
                    f"{_reason(handle.error)}",
                )
                instance.mcp_disabled.append(server_name)
                continue

            try:
                # Register tools into the router as {server}__{tool}, with the
                # bare name kept as an alias — see core/tool_router.py. The
                # worker used to key native *and* user MCP on the bare name, so
                # a tenant's own server could shadow a native tool.
                #
                # Cached here rather than on first use: this call has already
                # been made, and every tenant folding in the shared module then
                # inherits the answer instead of re-listing eight servers.
                tools_result = await handle.session.list_tools()
            except Exception as e:
                _log.debug(
                    f"[worker_server_module] Skipping MCP server '{server_name}': "
                    f"connected but would not list tools ({e})",
                )
                handle.closing.set()
                instance.mcp_disabled.append(server_name)
                continue

            instance._servers.append(handle)
            instance.agent_sessions[server_name] = handle.session
            instance._session_tools[server_name] = tools_result.tools
            for tool in tools_result.tools:
                instance.tool_router.register(server_name, tool.name)

    async def _connect_user_mcp(self, disabled: set) -> None:
        """This tenant's own MCP servers.

        In scale mode `stdio_mcp_allowed()` is force-disabled, so in practice
        these are remote (SSE) sessions — no subprocess, which is what makes a
        per-tenant set affordable at fleet scale.
        """
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
        from datetime import timedelta
        from core.config import MCP_SESSION_READ_TIMEOUT

        instance = self

        _SESSION_READ_TIMEOUT = timedelta(seconds=MCP_SESSION_READ_TIMEOUT)

        # Browser Automation (playwright) requires a local browser install — skip in workers.
        _BROWSER_MCP_NAMES = {"Browser Automation", "browser", "playwright"}
        _BROWSER_MCP_PKGS = {"@playwright/mcp", "playwright-mcp"}

        from core.mcp_oauth import can_refresh, refresh_and_persist
        from core.scale.context import report_mcp_status, resolve_mcp_servers
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
            params = (
                _build_remote_mcp_params(cfg)
                if server_type == "remote"
                else _build_stdio_mcp_params(cfg)
            )
            if params is None:
                instance.mcp_disabled.append(server_name)
                continue

            async def connect(stack, params=params, server_type=server_type):
                if server_type == "remote":
                    read, write = await _open_remote(
                        stack, params["url"], params.get("headers", {})
                    )
                else:
                    read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()
                return session

            handle = await _start_server(server_name, connect)

            # One retry, with a freshly minted access token.
            #
            # An OAuth access token lasts about an hour, so a server authorised
            # this morning answers `401` this afternoon — and the 401 does not
            # arrive as a 401. It crashes the MCP client's task group, which
            # cancels the connect, so what we hold here is a `CancelledError`
            # with nothing in it. Inspecting the failure to decide whether to
            # refresh would mean reading a cause that is not there.
            #
            # So the rule is the honest one: the connect failed and this server
            # has refresh material, therefore try once with a new token. A
            # transient failure costs one extra token exchange; an expired token
            # is repaired without anyone being told to click Authorize again.
            renewed = False
            refresh_refused = False
            if handle.error is not None and server_type == "remote" and can_refresh(cfg):
                fresh = await refresh_and_persist(cfg)
                if fresh:
                    renewed = True
                    _log.debug(
                        f"[worker_server_module] Refreshed the token for "
                        f"'{server_name}'; retrying",
                    )
                    params["headers"] = {
                        **params.get("headers", {}),
                        "Authorization": f"Bearer {fresh}",
                    }
                    handle = await _start_server(server_name, connect)
                else:
                    refresh_refused = True

            if handle.error is not None:
                # `reauth_needed` is an instruction to go and click Authorize,
                # so it is worth being exact about rather than treating every
                # failure of an OAuth server as one. It used to be the latter,
                # which would tell someone to re-authorise over a DNS blip.
                #
                # Two cases genuinely mean it, and only these two: the refresh
                # was attempted and refused, or there is no refresh material to
                # attempt with. A server that took a *fresh* token and still
                # failed is not one the person can fix — that is the server
                # being down, and saying otherwise sends them somewhere useless.
                is_oauth = server_type == "remote" and cfg.get("auth") == "oauth"
                needs_auth = is_oauth and not renewed and (
                    refresh_refused or not can_refresh(cfg)
                )
                _log.info(
                    f"[worker_server_module] Skipping user MCP '{server_name}': "
                    f"{_reason(handle.error)}"
                    + ("  (re-authorisation needed)" if needs_auth else ""),
                )
                instance.mcp_disabled.append(server_name)
                if needs_auth:
                    instance.mcp_reauth_needed.append(server_name)
                    await report_mcp_status(server_name, "reauth_needed")
                continue

            # `ext_mcp_` is how the rest of the engine spells "this session
            # belongs to a user-configured MCP server" — core/server.py:563,
            # routes/tools.py, mcp_client.py, and the three sites in
            # react_engine.py that strip it back off to name the server in an
            # error. The worker used its bare name instead, so a user MCP tool
            # was declared to the model as `sometool` here and as
            # `myserver__sometool` on the API server: an agent's stored tools[]
            # array could only ever match in one of the two places, and on a
            # worker it silently matched nothing.
            agent_key = f"ext_mcp_{server_name}"
            try:
                tools_result = await handle.session.list_tools()
            except Exception as e:
                _log.debug(
                    f"[worker_server_module] Skipping user MCP '{server_name}': "
                    f"connected but would not list tools ({e})",
                )
                handle.closing.set()
                instance.mcp_disabled.append(server_name)
                continue

            instance._servers.append(handle)
            instance.agent_sessions[agent_key] = handle.session
            instance._session_tools[agent_key] = tools_result.tools
            for tool in tools_result.tools:
                # alias=False: a tenant's own MCP server must not be able to take
                # over a native tool's bare name for that tenant's runs.
                instance.tool_router.register(
                    server_name, tool.name, session_key=agent_key, alias=False
                )
            # And say so when it works, or a server flagged once stays flagged
            # after the person has already fixed it. `report_mcp_status` skips
            # a write when nothing moved, so the common case costs a read.
            await report_mcp_status(server_name, "connected")
            _log.debug(f"[worker_server_module] Connected user MCP '{server_name}'")

    def _attach_memory_store(self) -> None:
        """Long-term memory, when this deployment can hold one safely.

        **Never in a multi-tenant process, and that is a correctness rule
        rather than a preference.**

        `MemoryStore` is ChromaDB — the docstring here said "Postgres-backed"
        for as long as it existed and was never true — and its index lives at
        `core/runtime_dirs.py::state_dir("chroma_db")`, which resolves through
        `get_tenant()`. `build_shared()` runs once at worker startup, *before
        any tenant is bound*, so the path it computes is the **default**
        tenant's; `build_for_tenant()` then folds that same store into every
        tenant's module. One index, no tenant dimension, every org sharing it.

        There is no per-tenant repair available here either. Building the store
        lazily under a tenant would give each org its own directory on a replica
        whose disk is ephemeral and whose next job belongs to somebody else —
        rebuilding a vector index per pool entry, to hold data that cannot
        survive the task.

        So a process serving many tenants leaves `memory_store` as `None`, which
        is a supported value everywhere it is read: `__init__` sets it, and
        `core/tools.py::build_system_prompt` takes it. An embedder that wants
        memory supplies one whose storage is actually shared and actually
        scoped.

        `is_multi_tenant()` is only true once an embedder has called
        `set_resource_provider()`, so the single-tenant product is untouched and
        keeps the store it has always had.
        """
        from core.tenancy import is_multi_tenant

        if is_multi_tenant():
            return

        try:
            import os
            pg_url = os.getenv("SCALE_POSTGRES_URL", "")
            if pg_url:
                from core.memory import MemoryStore
                self.memory_store = MemoryStore()
        except Exception:
            pass

    async def close(self) -> None:
        """Close what this module opened. Never what it borrowed.

        Each server closes its own stack, in the task that opened it, which is
        anyio's requirement met by construction. This used to be one shared
        stack and the docstring here had to *ask* callers to close it from the
        building task — a constraint no type could express and one wrong caller
        could break. `core/server.py`'s `_filesystem_mcp_manager` carries the
        scar that produced that warning.

        Safe from any task now, and safe to call twice.
        """
        servers, self._servers = self._servers, []
        for handle in servers:
            handle.closing.set()
        for handle in servers:
            if handle.task is None:
                continue
            try:
                await handle.task
            except Exception:
                # An owner task that failed on the way out has nothing left to
                # tell anyone: its session is gone either way.
                pass


def _get_native_mcp_servers(
    tools_dir: Path, backend_root: Path, scope: str = "all"
) -> dict:
    """Return native MCP server configs for the worker process.

    Uses core.tools_registry as the single source of truth for tool filenames.
    WORKER_NATIVE_TOOLS / WORKER_NPX_TOOLS control what runs in workers vs. not.

    `scope` splits them by whether they depend on who is calling:

        "shared"  everything that advertises the same tools to everybody
        "tenant"  the Filesystem server, rooted at the caller's vault
        "all"     both, which is what a single-tenant process wants
    """
    import os
    from pathlib import Path as _Path
    from mcp import StdioServerParameters
    from core.tools_registry import (
        ALL_NATIVE_TOOLS,
        TENANT_SCOPED_TOOLS,
        WORKER_NATIVE_TOOLS,
        WORKER_NPX_TOOLS,
    )

    if scope not in ("shared", "tenant", "all"):
        raise ValueError(f"unknown scope {scope!r}")

    # Tool subprocesses need the backend root on PYTHONPATH so they can do
    # `from core.config import ...` — same as when the main server spawns them.
    tool_env = os.environ.copy()
    existing_pp = tool_env.get("PYTHONPATH", "")
    backend_root_str = str(backend_root)
    tool_env["PYTHONPATH"] = f"{backend_root_str}{os.pathsep}{existing_pp}" if existing_pp else backend_root_str

    servers = {}

    def _python_tool(name: str, env: dict) -> None:
        script = _Path(ALL_NATIVE_TOOLS[name])
        if script.exists():
            servers[name] = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)
        else:
            _log.debug(f"[worker_server_module] Tool script not found, skipping '{name}': {script}")

    if scope in ("shared", "all"):
        # Python-native tools (subset safe for headless worker processes) that
        # advertise the same behaviour to everybody.
        for name in sorted(WORKER_NATIVE_TOOLS - TENANT_SCOPED_TOOLS):
            _python_tool(name, tool_env)

        # npx-based tools available to workers
        for name, args in WORKER_NPX_TOOLS.items():
            servers[name] = StdioServerParameters(command=_NPX_CMD, args=args)

    if scope in ("tenant", "all"):
        # Tools whose subprocess resolves the vault, the store or settings for
        # itself. A ContextVar does not survive a fork+exec, so one of these
        # spawned once and shared reads the *default* tenant's data whoever
        # calls it. Spawned per tenant instead, told who they serve.
        #
        # The arithmetic is worth knowing: this is one extra subprocess per tool
        # per live pool entry. In the shipped single-tenant product that is the
        # same count as before. On a cloud worker `code_vault_search` is
        # excluded and D8 drops `bash` and `vault_sandbox`, which leaves
        # `file_reader` and `sql` — two processes, plus the Filesystem server
        # below.
        from core.tenancy import get_tenant

        tenant_env = dict(tool_env)
        tenant_env["SYNAPSE_TENANT_ID"] = get_tenant()

        # Passed through explicitly rather than left to `os.environ.copy()`
        # above. It would be inherited either way, and that is the problem: a
        # mechanism that works by accident of inheritance is invisible here,
        # where the decision to hand it to a child actually lives. See
        # `core/tool_server.py` for what it names and why it exists.
        resolver = os.getenv("SYNAPSE_DOCUMENT_RESOLVER", "").strip()
        if resolver:
            tenant_env["SYNAPSE_DOCUMENT_RESOLVER"] = resolver

        # The same treatment, and the same reasoning, for the session binder.
        # Without it a child that opens its own store speaks to the database as
        # nobody — which a deployment gating rows on the connection answers with
        # an empty result rather than an error.
        binder = os.getenv("SYNAPSE_SESSION_BINDER", "").strip()
        if binder:
            tenant_env["SYNAPSE_SESSION_BINDER"] = binder
        for name in sorted(WORKER_NATIVE_TOOLS & TENANT_SCOPED_TOOLS):
            _python_tool(name, tenant_env)

        # **The Node Filesystem server used to be spawned here. It is gone, and
        # `vault` above replaces it.**
        #
        # It was rooted at `_vault_root()`, a directory path — which is the
        # assumption that made it wrong for a fleet rather than merely slow. A
        # replica's vault directory is a *working copy*: `core/vault_backend.py`
        # materialises it from the blob store on demand, so the Node process was
        # reading whatever happened to have been hydrated, and reported an empty
        # vault on any pod that had not served that tenant before. Where the blob
        # store is S3 there is no directory to root at in the first place.
        #
        # `tools/vault_fs.py` goes through `core.storage.get_blob_store()`
        # instead, so it reads the vault itself rather than a copy of it, and one
        # implementation serves a local install and an object store alike.
        #
        # It also spawned `npx`, which no scale-mode image has — see
        # `WORKER_NPX_TOOLS` for why installing Node was not the answer.
        #
        # `WORKER_FILESYSTEM_PATHS` is no longer read anywhere. A deployment that
        # still sets it is not broken by this; it simply has no effect, because
        # the vault is now addressed by key rather than by path.

    return servers


def _build_stdio_mcp_params(cfg: dict):
    """Build StdioServerParameters from a saved mcp_servers.json config dict.

    Returns None when this deployment must not spawn user-supplied stdio servers.
    Workers run in scale mode, where ``stdio_mcp_allowed()`` is force-disabled —
    this is a second, independent spawn sink from
    ``MCPClientManager.connect_stdio_server``, so it needs its own gate rather
    than inheriting one.
    """
    import shlex
    from mcp import StdioServerParameters
    from core.mcp_client import stdio_mcp_allowed, check_stdio_command_allowed

    command = cfg.get("command", "")
    if not command:
        return None

    if not stdio_mcp_allowed():
        _log.debug(f"[worker] Refusing to spawn stdio MCP server "
              f"'{cfg.get('name', '?')}': stdio MCP is disabled on this deployment.")
        return None
    try:
        check_stdio_command_allowed(command)
    except ValueError as e:
        _log.debug(f"[worker] Refusing to spawn stdio MCP server "
              f"'{cfg.get('name', '?')}': {e}")
        return None

    args_raw = cfg.get("args", [])
    # Support both list and space-separated string for args
    if isinstance(args_raw, str):
        args_raw = shlex.split(args_raw)
    env = cfg.get("env") or {}
    return StdioServerParameters(command=command, args=list(args_raw), env=env or None)


def _reason(exc: BaseException) -> str:
    """A one-line why, for a server that could not be opened.

    `CancelledError` is the common one and it stringifies to nothing at all —
    which is how "an expired OAuth token" used to reach the log as an empty
    message, or as no message. Named here so the operator sees a cause rather
    than a blank.
    """
    if isinstance(exc, asyncio.CancelledError):
        return (
            "its client cancelled the connect, which usually means an expired "
            "credential or a server that closed the connection"
        )
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class _ServerHandle:
    """One MCP server's session, and the task that owns its lifetime.

    ## Why a task per server

    Every MCP client context manager is backed by an `anyio` task group, and a
    task group that loses a child **cancels its host task on the way out**. When
    all the servers shared one `AsyncExitStack` entered in the module builder's
    task, that host was the builder — so one server answering `401` cancelled the
    task building the whole tenant's module.

    That cost three separate failures before it was understood. `except
    Exception` never caught it (`CancelledError` is a `BaseException`), so ARQ
    recorded the job cancelled and burned its retries. Absorbing the
    cancellation instead was worse: the build finished, the cancellation
    resurfaced at the owner task's next await, and `mcp_pool` was left holding an
    entry with `ready` set, no error and **no module** — every later chat turn
    for that org died instantly on `'NoneType' object has no attribute
    'agent_sessions'`.

    With a task per server the cancellation lands on that server's own task and
    goes no further. The builder is never cancelled, so the pool cannot be
    poisoned, and a failing server can no longer take the servers listed after it
    — which it did, making the outcome depend on the order
    `resolve_mcp_servers()` happened to return.

    ## Why the task also does the closing

    anyio requires a cancel scope to be exited by the task that entered it. The
    stack is opened, used and closed inside `_own_server`, so that holds by
    construction rather than by a comment asking callers to be careful.
    `core/scale/mcp_pool.py::_own` is the same shape one level up, and
    `core/server.py::_filesystem_mcp_manager` carries the scar that produced it.
    """

    __slots__ = ("name", "session", "tools", "error", "ready", "closing", "task")

    def __init__(self, name: str):
        self.name = name
        self.session = None
        self.tools: list = []
        self.error: BaseException | None = None
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.task: asyncio.Task | None = None


async def _own_server(handle: "_ServerHandle", connect) -> None:
    """Open one server, hold it, close it — all in this task.

    `connect` is given a private `AsyncExitStack` and returns the live session.
    Whatever it raises is recorded on the handle rather than propagated: the
    builder decides what to do about a server it could not open, and it cannot
    do that if this task's failure has already cancelled it.
    """
    from contextlib import AsyncExitStack

    stack = AsyncExitStack()
    try:
        try:
            handle.session = await connect(stack)
        except BaseException as exc:  # noqa: BLE001 — handed to the builder
            handle.error = exc
            return
        finally:
            handle.ready.set()

        await handle.closing.wait()
    finally:
        try:
            await stack.aclose()
        except BaseException:  # noqa: BLE001
            # A server that failed on the way in often fails on the way out too.
            # Nothing above can act on it, and raising here would replace a
            # useful error with a teardown one.
            pass


async def _start_server(name: str, connect) -> "_ServerHandle":
    """Spawn a server's owner task and wait for it to report, or time out.

    The timeout is the same bound `_open_remote` applies to a single connect,
    and it is applied here as well because a stdio server can hang in exactly
    the same way — and because `handle.ready` is the only thing the builder
    waits on.
    """
    handle = _ServerHandle(name)
    handle.task = asyncio.create_task(_own_server(handle, connect), name=f"mcp[{name}]")
    try:
        await asyncio.wait_for(handle.ready.wait(), timeout=REMOTE_MCP_CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        handle.error = TimeoutError(
            f"no response within {REMOTE_MCP_CONNECT_TIMEOUT:.0f}s"
        )
        handle.closing.set()
    return handle


async def _open_remote(exit_stack, url: str, headers: dict) -> tuple:
    """Open a remote MCP server, trying both transports. Returns `(read, write)`.

    ## Why this is not just `sse_client`

    It was, and that is a bug with an unusually bad shape. Two transports are in
    common use — streamable HTTP (MCP 2025-03-26+) and the legacy SSE one — and
    `core/mcp_client.py::_open_http_session` has negotiated between them since
    remote servers were added. This path did not: it dialled SSE unconditionally.

    Against a streamable-HTTP endpoint that is not a clean failure. The server
    accepts the GET and simply never sends the `endpoint` event SSE is waiting
    for, so `sse_client` **hangs** — and a hang is not an exception, so the
    `try/except` around the call site catches nothing. One such server wedges
    `WorkerServerModule.build_for_tenant`, which wedges `mcp_pool._own`, which
    leaves every `acquire()` for that tenant waiting on `entry.ready` forever.

    Observed on `https://mcp.deepwiki.com/mcp`, a `/mcp` endpoint seeded by the
    cloud's app directory: unbounded via SSE, and 2.6 seconds and three tools
    via streamable HTTP. The job holding it sat in `arq:in-progress` for forty
    minutes with nothing published and no `chat_sessions` row written, because
    the pool is acquired before either.

    ## Why there is a timeout as well

    The transport fix removes the case we found; the timeout removes the class.
    A remote server is a third party, and "accepts the connection, then goes
    quiet" is a thing third parties do — through a proxy, mid-deploy, or because
    someone pointed a URL at something that is not an MCP server at all. Nothing
    else in this loop is bounded: `ClientSession`'s `read_timeout_seconds`
    covers requests *after* a session exists, not the handshake that creates one.

    A server that exceeds it is skipped like any other failing server, which is
    what the `except` around the caller already does with everything else.
    """
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamablehttp_client

    async def _connect():
        # Streamable HTTP first — it is what current servers speak, and what a
        # `/mcp` endpoint almost always means.
        try:
            read, write, _ = await exit_stack.enter_async_context(
                streamablehttp_client(url, headers=headers or None)
            )
            return read, write
        except BaseException as exc:  # noqa: BLE001 — reported below if SSE also fails
            first = exc

        # Legacy SSE. Only the headers we set explicitly, never httpx's defaults:
        # `accept-encoding` and `user-agent` confuse some SSE servers, which is
        # the same accommodation `core/mcp_client.py` makes.
        try:
            read, write = await exit_stack.enter_async_context(
                sse_client(url, headers=headers or None)
            )
            return read, write
        except BaseException as exc:  # noqa: BLE001
            raise RuntimeError(
                f"both MCP transports failed for {url} — "
                f"streamable HTTP: {type(first).__name__}: {first}; "
                f"SSE: {type(exc).__name__}: {exc}"
            ) from None

    return await asyncio.wait_for(_connect(), timeout=REMOTE_MCP_CONNECT_TIMEOUT)


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
