"""
MCP Client Manager supporting two transport types:

  stdio  — command-line subprocess (existing behaviour)
  remote — HTTP/SSE with optional bearer token or native OAuth 2.0 PKCE flow

OAuth flow (remote, no token):
  1. add_server() spawns an asyncio background task that calls start_oauth_connect()
  2. OAuthClientProvider discovers the server's auth metadata and calls redirect_handler()
  3. redirect_handler() parses the `state` from the auth URL, registers it in
     mcp_oauth_state, then resolves the auth_url_future so add_server() can
     return the URL to the API layer immediately.
  4. The background coroutine's callback_handler() blocks on an asyncio.Event.
  5. When the user completes OAuth, GET /api/mcp/oauth/callback?code=…&state=…
     is hit → mcp_oauth_state.complete_callback() sets the event.
  6. callback_handler() returns (code, state), token exchange completes, MCP
     session is established and persisted in self.sessions.

Tokens are stored on disk (FileTokenStorage) so OAuth servers auto-reconnect
on backend restart without re-authenticating (the provider handles refresh).
"""

import asyncio
import json
import os
import secrets
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import AnyUrl

from mcp import ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from core.config import DATA_DIR
import core.mcp_oauth_state as oauth_state

# ── Constants ──────────────────────────────────────────────────────────────────

_SESSION_READ_TIMEOUT = timedelta(seconds=60)

MCP_SERVERS_FILE = os.path.join(DATA_DIR, "mcp_servers.json")
MCP_TOKENS_DIR   = os.path.join(DATA_DIR, "mcp_tokens")

# Redirect URI registered with OAuth servers.
# Reads SYNAPSE_BACKEND_PORT from env so it matches the running port.
_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
OAUTH_CALLBACK_URL = f"http://localhost:{_BACKEND_PORT}/api/mcp/oauth/callback"


# ── Token Storage ──────────────────────────────────────────────────────────────

class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens and client registration to disk per server."""

    def __init__(self, server_name: str):
        safe = server_name.replace("/", "_").replace(" ", "_")
        self._tok  = os.path.join(MCP_TOKENS_DIR, f"{safe}.json")
        self._cli  = os.path.join(MCP_TOKENS_DIR, f"{safe}_client.json")

    def _read(self, path: str) -> Optional[dict]:
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None

    def _write(self, path: str, data: dict):
        os.makedirs(MCP_TOKENS_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    async def get_tokens(self) -> Optional[OAuthToken]:
        d = self._read(self._tok)
        return OAuthToken(**d) if d else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write(self._tok, tokens.model_dump(mode="json"))

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        d = self._read(self._cli)
        return OAuthClientInformationFull(**d) if d else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._write(self._cli, info.model_dump(mode="json"))

    def delete_all(self):
        for p in (self._tok, self._cli):
            if os.path.exists(p):
                os.remove(p)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_oauth_provider(
    name: str,
    url: str,
    redirect_handler=None,
    callback_handler=None,
) -> OAuthClientProvider:
    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="Synapse AI",
            redirect_uris=[AnyUrl(OAUTH_CALLBACK_URL)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=FileTokenStorage(name),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


async def _open_http_session(
    exit_stack: AsyncExitStack,
    url: str,
    http_client: httpx.AsyncClient,
) -> tuple:
    """
    Try streamable HTTP (MCP 2025-03-26+) first, fall back to SSE (legacy).
    Returns (read, write).

    Both standards are in common use:
    - streamable_http_client: GitHub Copilot, Vercel, Jira, Zapier
    - sse_client:             Zerodha Kite, older mcp-remote-based servers
    """
    err_http: Optional[BaseException] = None

    # ── Try Streamable HTTP ────────────────────────────────────────────────────
    try:
        read, write, _ = await exit_stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        return read, write
    except BaseException as e:
        err_http = e
        print(f"[MCP] Streamable HTTP failed for {url}: {type(e).__name__}: {e}. Trying SSE…")

    # ── SSE fallback ───────────────────────────────────────────────────────────
    # Pass only the headers we explicitly set (auth bearer etc.), NOT httpx
    # defaults like accept-encoding or user-agent which can confuse SSE servers.
    explicit_headers: Dict[str, str] = {}
    if http_client.headers.get("authorization"):
        explicit_headers["authorization"] = http_client.headers["authorization"]

    try:
        read, write = await exit_stack.enter_async_context(
            sse_client(url, headers=explicit_headers or None)
        )
        return read, write
    except BaseException as e:
        raise RuntimeError(
            f"Both transports failed for {url}.\n"
            f"  Streamable HTTP: {type(err_http).__name__}: {err_http}\n"
            f"  SSE:             {type(e).__name__}: {e}"
        ) from None


# ── Manager ────────────────────────────────────────────────────────────────────

class MCPClientManager:

    def __init__(self, exit_stack: AsyncExitStack):
        self.exit_stack = exit_stack
        self.sessions: Dict[str, ClientSession] = {}
        self.servers_config: List[Dict[str, Any]] = self._load_servers()

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load_servers(self) -> List[Dict[str, Any]]:
        if not os.path.exists(MCP_SERVERS_FILE):
            return []
        try:
            with open(MCP_SERVERS_FILE) as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading MCP servers config: {e}")
            return []

    def save_servers(self):
        os.makedirs(os.path.dirname(MCP_SERVERS_FILE), exist_ok=True)
        with open(MCP_SERVERS_FILE, "w") as f:
            json.dump(self.servers_config, f, indent=4)

    def _set_status(self, name: str, status: str):
        for s in self.servers_config:
            if s["name"] == name:
                s["status"] = status
                break
        self.save_servers()

    async def _auto_register(self, name: str):
        """Register a newly connected session into the global agent_sessions and tool_router.
        Uses a lazy import of core.server to avoid circular imports.
        Called from background coroutines where the route-level _register_session() helper
        is not available (e.g. after OAuth flow completes asynchronously)."""
        try:
            import core.server as _server
            session = self.sessions.get(name)
            if not session:
                return
            agent_key = f"ext_mcp_{name}"
            _server.agent_sessions[agent_key] = session
            tools = await session.list_tools()
            for tool in tools.tools:
                _server.tool_router[f"{name}__{tool.name}"] = (agent_key, tool.name)
            print(f"[MCP] Registered {len(tools.tools)} tools for '{name}': "
                  f"{[t.name for t in tools.tools]}")
        except Exception as e:
            print(f"[MCP] Tool registration failed for '{name}': {e}")

    # ── Stdio connection ───────────────────────────────────────────────────────

    async def connect_stdio_server(self, config: Dict) -> Optional[ClientSession]:
        name    = config["name"]
        command = config.get("command", "")
        args    = config.get("args", [])
        env_vars = config.get("env", {})

        if not command:
            print(f"Skipping '{name}': no command")
            return None

        env = os.environ.copy()
        env.update(env_vars)

        print(f"Connecting to stdio MCP server '{name}' ({command} {args})...")
        inner_stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=command, args=args, env=env)
            read, write = await inner_stack.enter_async_context(stdio_client(params))
            session = await inner_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
            )
            await session.initialize()
            await self.exit_stack.enter_async_context(inner_stack)
            self.sessions[name] = session
            print(f"Connected to stdio MCP server '{name}'.")
            return session
        except BaseException as e:
            print(f"Failed stdio connect '{name}': {e}")
            try:
                await inner_stack.aclose()
            except BaseException:
                pass
            return None

    # ── Remote connection: bearer token ───────────────────────────────────────

    async def connect_remote_server(self, config: Dict) -> Optional[ClientSession]:
        """Connect to a remote MCP server with an optional pre-auth bearer token."""
        name  = config["name"]
        url   = config["url"]
        token = config.get("token", "")

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        print(f"Connecting to remote MCP server '{name}' ({url})...")
        inner_stack = AsyncExitStack()
        try:
            http_client = await inner_stack.enter_async_context(
                httpx.AsyncClient(headers=headers, follow_redirects=True)
            )
            read, write = await _open_http_session(inner_stack, url, http_client)
            session = await inner_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
            )
            await session.initialize()
            await self.exit_stack.enter_async_context(inner_stack)
            self.sessions[name] = session
            print(f"Connected to remote MCP server '{name}'.")
            return session
        except BaseException as e:
            print(f"Failed remote connect '{name}': {e}")
            try:
                await inner_stack.aclose()
            except BaseException:
                pass
            return None

    # ── Remote connection: OAuth (background task) ─────────────────────────────

    async def start_oauth_connect(self, config: Dict, auth_url_future: "asyncio.Future[str]"):
        """
        Background coroutine for the OAuth flow.
        Resolves auth_url_future as soon as the auth URL is known so the API
        route can return it to the frontend immediately.
        """
        name = config["name"]
        url  = config["url"]
        event = asyncio.Event()
        state_key_box: List[str] = []  # mutable container so closure can write to it

        async def redirect_handler(auth_url: str) -> None:
            # Parse the `state` that OAuthClientProvider generated
            params = parse_qs(urlparse(auth_url).query)
            sk = params.get("state", [secrets.token_urlsafe(16)])[0]
            state_key_box.append(sk)
            oauth_state.register(sk, name)
            # Also store the event so complete_callback can set it
            entry = oauth_state.get(sk)
            if entry:
                entry["event"] = event   # replace the one created by register()
            # Signal the API route that we have the URL
            if not auth_url_future.done():
                auth_url_future.set_result(auth_url)

        async def callback_handler() -> tuple[str, Optional[str]]:
            await event.wait()   # blocks until /api/mcp/oauth/callback is hit
            sk = state_key_box[0] if state_key_box else None
            entry = oauth_state.pop(sk) if sk else None
            return (entry or {}).get("code", ""), sk

        oauth_provider = _make_oauth_provider(
            name, url,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

        try:
            http_client = await self.exit_stack.enter_async_context(
                httpx.AsyncClient(auth=oauth_provider, follow_redirects=True)
            )
            read, write = await _open_http_session(self.exit_stack, url, http_client)
            session = await self.exit_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
            )
            await session.initialize()
            self.sessions[name] = session
            self._set_status(name, "connected")
            await self._auto_register(name)    # ← register tools into agent_sessions
            print(f"OAuth complete — connected to '{name}'.")
        except Exception as e:
            print(f"OAuth connection failed for '{name}': {e}")
            self._set_status(name, "disconnected")
            if not auth_url_future.done():
                auth_url_future.set_exception(e)

    # ── Remote reconnect: use cached tokens (startup / manual retry) ───────────

    async def _connect_remote_cached(self, config: Dict) -> Optional[ClientSession]:
        """
        Reconnect a remote server on startup without prompting the user.

        Strategy (in order):
        1. Cached OAuth tokens exist → try OAuthClientProvider (handles token refresh).
        2. No cached tokens → try a plain no-auth direct connection.
           This handles servers like Zerodha where authentication is lazy (required
           only when a tool is called, not at connection time).
        3. Both fail → return None, server shows as Disconnected.
        """
        name = config["name"]
        url  = config["url"]

        storage = FileTokenStorage(name)
        has_tokens = bool(await storage.get_tokens())

        if has_tokens:
            # ── OAuth path: try cached tokens, allow silent refresh ────────────
            async def noop_redirect(auth_url: str) -> None:
                print(f"[MCP] Token refresh failed for '{name}' — re-auth needed.")

            async def noop_callback() -> tuple[str, Optional[str]]:
                raise RuntimeError("Interactive OAuth not available at startup")

            oauth_provider = _make_oauth_provider(
                name, url,
                redirect_handler=noop_redirect,
                callback_handler=noop_callback,
            )
            inner_stack = AsyncExitStack()
            try:
                http_client = await inner_stack.enter_async_context(
                    httpx.AsyncClient(auth=oauth_provider, follow_redirects=True)
                )
                read, write = await _open_http_session(inner_stack, url, http_client)
                session = await inner_stack.enter_async_context(
                    ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
                )
                await session.initialize()
                await self.exit_stack.enter_async_context(inner_stack)
                self.sessions[name] = session
                print(f"[MCP] Reconnected '{name}' with cached OAuth tokens.")
                return session
            except BaseException as e:
                print(f"[MCP] Cached OAuth reconnect failed for '{name}': {e}. Falling back to direct connect.")
                try:
                    await inner_stack.aclose()
                except BaseException:
                    pass

        # ── Direct path: no token, no OAuth — server may not need auth ───────
        print(f"[MCP] Attempting direct (no-auth) connection to '{name}' ({url})...")
        inner_stack = AsyncExitStack()
        try:
            http_client = await inner_stack.enter_async_context(
                httpx.AsyncClient(follow_redirects=True)
            )
            read, write = await _open_http_session(inner_stack, url, http_client)
            session = await inner_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
            )
            await session.initialize()
            await self.exit_stack.enter_async_context(inner_stack)
            self.sessions[name] = session
            print(f"[MCP] Connected '{name}' via direct (no-auth) connection.")
            return session
        except BaseException as e:
            print(f"[MCP] Direct connect also failed for '{name}': {e}")
            try:
                await inner_stack.aclose()
            except BaseException:
                pass
            return None

    # ── connect_all (startup) ──────────────────────────────────────────────────

    async def connect_all(self):
        for config in self.servers_config:
            name = config.get("name")
            if not name or name in self.sessions:
                continue

            server_type = config.get("server_type", "stdio")
            if server_type == "stdio":
                session = await self.connect_stdio_server(config)
            elif config.get("token"):
                session = await self.connect_remote_server(config)
            else:
                session = await self._connect_remote_cached(config)

            if session:
                self._set_status(name, "connected")
                await self._auto_register(name)   # ← register on startup
        return self.sessions

    # ── add_server ─────────────────────────────────────────────────────────────

    async def add_server(
        self,
        name: str,
        label: str = "",
        server_type: str = "stdio",
        command: str = "",
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        url: str = "",
        token: str = "",
    ) -> Dict[str, Any]:
        import shutil

        for s in self.servers_config:
            if s["name"] == name:
                raise ValueError(f"Server '{name}' already exists.")

        new_config: Dict[str, Any] = {"name": name, "label": label or name, "server_type": server_type, "status": "disconnected"}

        if server_type == "stdio":
            if not command:
                raise ValueError("Command is required for stdio servers.")
            if shutil.which(command) is None:
                hints = {"uvx": "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh",
                         "npx": "Install Node.js/npm."}
                raise ValueError(f"'{command}' not found in PATH. {hints.get(command, 'Please install it.')}")
            new_config.update({"command": command, "args": args or [], "env": env or {}})
        else:
            if not url:
                raise ValueError("URL is required for remote servers.")
            new_config.update({"url": url, "token": token})

        # Always save first
        self.servers_config.append(new_config)
        self.save_servers()

        if server_type == "stdio":
            session = await self.connect_stdio_server(new_config)
            if session:
                self._set_status(name, "connected")
                new_config["status"] = "connected"
                await self._auto_register(name)
            return {"config": new_config, "connected": bool(session), "status": new_config["status"]}

        # Remote
        if token:
            session = await self.connect_remote_server(new_config)
            if session:
                self._set_status(name, "connected")
                new_config["status"] = "connected"
            return {"config": new_config, "connected": bool(session), "status": new_config["status"]}

        # Remote + OAuth
        loop = asyncio.get_event_loop()
        auth_url_future: asyncio.Future = loop.create_future()
        asyncio.create_task(self.start_oauth_connect(new_config, auth_url_future))

        try:
            # Wait up to 15 s for OAuthClientProvider to hand us the auth URL.
            # Use asyncio.shield so the background task keeps running on timeout.
            auth_url = await asyncio.wait_for(asyncio.shield(auth_url_future), timeout=15.0)
            return {"config": new_config, "connected": False, "status": "oauth_pending", "auth_url": auth_url}
        except asyncio.TimeoutError:
            return {"config": new_config, "connected": False, "status": "disconnected", "auth_url": None}

    # ── reconnect_server (manual retry) ────────────────────────────────────────

    async def reconnect_server(self, name: str) -> bool:
        config = self.get_server_config(name)
        if not config:
            raise ValueError(f"Server '{name}' not found.")

        server_type = config.get("server_type", "stdio")
        if server_type == "stdio":
            session = await self.connect_stdio_server(config)
        elif config.get("token"):
            session = await self.connect_remote_server(config)
        else:
            session = await self._connect_remote_cached(config)

        if session:
            self._set_status(name, "connected")
            await self._auto_register(name)   # ← register on manual retry
            return True
        return False

    # ── remove_server ──────────────────────────────────────────────────────────

    async def remove_server(self, name: str) -> bool:
        self.servers_config = [s for s in self.servers_config if s["name"] != name]
        self.save_servers()
        self.sessions.pop(name, None)
        FileTokenStorage(name).delete_all()
        return True

    # ── helpers ────────────────────────────────────────────────────────────────

    def get_server_config(self, name: str) -> Optional[Dict]:
        for s in self.servers_config:
            if s["name"] == name:
                return s
        return None
