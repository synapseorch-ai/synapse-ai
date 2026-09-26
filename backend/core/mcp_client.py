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

Tokens are stored per tenant in the database (StoreTokenStorage) so OAuth servers auto-reconnect
on backend restart without re-authenticating (the provider handles refresh).
"""

import asyncio
import json
import os
import secrets
import time
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

from core.config import MCP_SESSION_READ_TIMEOUT, load_settings
import core.mcp_oauth_state as oauth_state

from core.log import get_logger

_log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_SESSION_READ_TIMEOUT = timedelta(seconds=MCP_SESSION_READ_TIMEOUT)


# ── stdio MCP registration policy (security) ────────────────────────────────────
# stdio MCP servers launch arbitrary local OS processes (npx/uvx/python -c/…),
# which is the whole point of the transport but also an RCE-shaped capability.
# These helpers gate every path that can spawn one. See GHSA-3j67-x3j8-r32x.

def stdio_mcp_allowed() -> bool:
    """Whether stdio MCP servers may be registered/spawned on this deployment.

    Disabled in scale mode (multi-tenant / network-exposed) and toggleable via
    the ``allow_stdio_mcp`` setting. Fails open only if settings are unreadable,
    to preserve local behaviour when config is unavailable.

    "Unreadable" deliberately includes *not yet read*. ``allow_stdio_mcp``
    defaults to True, so a settings provider that has nothing bound returns a
    dict saying "allowed" — indistinguishable, at this call site, from the user
    having chosen it. That is not a default worth failing open on.
    """
    from core import settings_runtime

    if not settings_runtime.is_loaded():
        return False
    try:
        s = load_settings()
    except Exception:
        return True
    if s.get("scale_mode_enabled"):
        return False
    return bool(s.get("allow_stdio_mcp", True))


def check_stdio_command_allowed(command: str) -> None:
    """Enforce the optional ``mcp_command_allowlist`` (empty list = allow any)."""
    try:
        allowlist = load_settings().get("mcp_command_allowlist") or []
    except Exception:
        allowlist = []
    if allowlist and os.path.basename(command) not in allowlist:
        raise ValueError(f"Command '{command}' is not in the allowed MCP command list.")

# Redirect URI registered with OAuth servers.
# Reads SYNAPSE_BACKEND_PORT from env so it matches the running port.
_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
OAUTH_CALLBACK_URL = f"http://localhost:{_BACKEND_PORT}/api/mcp/oauth/callback"

#: Collection holding MCP OAuth material, one document per server.
#:
#: The database rather than the blob store, matching where the Google OAuth
#: credentials live: an access token, a refresh token and a dynamic client
#: registration are tenant *secrets*, and in a hosted deployment the database
#: is what carries KMS envelope encryption and row-level security. The blob
#: store is for tenant content. They were files in a shared folder with no
#: tenant dimension at all, so two tenants with a server called `github` had
#: one credential between them.
#:
#: A slot in `collections` rather than a table of its own, per the rule in
#: core/store/models.py: nothing queries these, they are only ever fetched by
#: the name of the server they belong to.
_TOKENS_COLLECTION = "mcp_tokens"

#: Renew this many seconds before the access token actually dies.
#:
#: A token that expires during the handshake fails the connect just as surely as
#: one that expired an hour ago, and the round trip to renew costs far less than
#: the re-authorisation prompt that failure produces. Sixty seconds also covers
#: ordinary clock drift between this host and the provider.
_EXPIRY_SKEW_SECONDS = 60

#: Where an authorization server publishes its metadata. RFC 8414 first, then
#: the OpenID Connect location that some providers serve instead.
_METADATA_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
)


async def _discover_token_endpoint(server_url: str) -> Optional[str]:
    """Find a server's token endpoint, for one authorised before we stored it.

    Only reached when nothing is on file. A server authorised from now on has
    its endpoint recorded at authorise time, from the metadata the SDK already
    discovered — which is the value to trust, because it was fetched when the
    person actually granted access rather than at some later moment of the
    server's choosing.

    Failure is None and a fall-through to the existing behaviour, never an
    exception: not being able to renew is worse than today only if it also
    breaks the connect that would have worked.
    """
    parsed = urlparse(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for path in _METADATA_PATHS:
                try:
                    response = await client.get(origin + path)
                except Exception:
                    continue
                if response.status_code != 200:
                    continue
                try:
                    endpoint = response.json().get("token_endpoint")
                except ValueError:
                    continue
                if endpoint:
                    return str(endpoint)
    except Exception:
        return None
    return None


# ── Token Storage ──────────────────────────────────────────────────────────────

class StoreTokenStorage(TokenStorage):
    """Persists OAuth tokens and client registration per server, per tenant.

    The four ``TokenStorage`` methods are ``async`` because the MCP SDK
    requires it — which is what makes this cheap, since the store is async too.
    """

    def __init__(self, server_name: str):
        self._key = _safe_server_name(server_name)

    async def _document(self) -> dict:
        from core.store import collections

        for item in await collections.load(_TOKENS_COLLECTION):
            if item.get("id") == self._key:
                return item
        return {}

    async def _merge(self, **fields) -> None:
        from core.store import collections

        items = await collections.load(_TOKENS_COLLECTION)
        for item in items:
            if item.get("id") == self._key:
                item.update(fields)
                break
        else:
            items.append({"id": self._key, **fields})
        await collections.save(_TOKENS_COLLECTION, items)

    async def get_tokens(self) -> Optional[OAuthToken]:
        d = (await self._document()).get("tokens")
        return OAuthToken(**d) if d else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Store the tokens, and an **absolute** expiry beside them.

        The absolute part is the whole point. `OAuthToken` carries `expires_in`,
        which is a duration from the moment it was issued — so a token stored at
        09:00 with `expires_in=3600` is dead at 10:00, and recomputing
        `now + expires_in` when it is loaded at 14:00 would call it fresh until
        15:00. The SDK's own `update_token_expiry()` does exactly that, in
        memory, at exchange time; nothing writes the result down.

        That omission is what made every restart ask the person to authorise
        again. `OAuthClientProvider._initialize()` restores the tokens and
        **not** `token_expiry_time`, and `is_token_valid()` reads
        `not self.token_expiry_time or ...` — so a `None` expiry counts as
        *valid*, the proactive refresh is skipped, the expired token goes out,
        and the `401` that comes back triggers a full re-authorisation rather
        than a refresh. The refresh token sat there unused the entire time.
        """
        import time

        fields: dict = {"tokens": tokens.model_dump(mode="json")}
        if tokens.expires_in:
            fields["expires_at"] = time.time() + float(tokens.expires_in)
        await self._merge(**fields)

    async def get_expires_at(self) -> Optional[float]:
        """When the stored access token dies, in absolute epoch seconds.

        None means unknown — a token stored before this was recorded. Treated as
        "assume it needs refreshing" by the caller rather than "assume it is
        fine", because the second is the assumption that produced the bug.
        """
        v = (await self._document()).get("expires_at")
        return float(v) if v is not None else None

    async def get_token_endpoint(self) -> Optional[str]:
        return (await self._document()).get("token_endpoint")

    async def set_token_endpoint(self, endpoint: str) -> None:
        """Remember where a refresh is sent, recorded when the person authorised.

        Stored rather than re-derived on each refresh for the reason
        `core/mcp_oauth.py` gives: a server that later advertises a *different*
        token endpoint would otherwise be handed this tenant's refresh token on
        its own say-so.

        It also has to be stored because the SDK cannot supply it cold. Its
        `_refresh_token()` reads `context.oauth_metadata`, which is populated by
        discovery during an interactive flow and is `None` in a fresh process —
        so it falls back to `urljoin(server_url, "/token")`. For Vercel the real
        endpoint is `https://vercel.com/api/login/oauth/token`, nothing like
        `https://mcp.vercel.com/token`, and the same is true of most providers.
        """
        await self._merge(token_endpoint=endpoint)

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        d = (await self._document()).get("client")
        return OAuthClientInformationFull(**d) if d else None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        await self._merge(client=info.model_dump(mode="json"))

    async def delete_all(self) -> None:
        from core.store import collections

        items = [
            i for i in await collections.load(_TOKENS_COLLECTION)
            if i.get("id") != self._key
        ]
        await collections.save(_TOKENS_COLLECTION, items)


def _safe_server_name(server_name: str) -> str:
    """A server name as a storage key.

    Stricter than the old filename sanitiser, which replaced only ``/`` and
    spaces — a server called ``../x`` produced a traversal-shaped path when
    these were files. Keeping every unexpected character out costs nothing and
    means a badly-named server is never a question about path handling.
    """
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in server_name).strip(".") or "_"


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
            client_name="SynapseOrch",
            redirect_uris=[AnyUrl(OAUTH_CALLBACK_URL)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        ),
        storage=StoreTokenStorage(name),
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
        _log.debug(f"[MCP] Streamable HTTP failed for {url}: {type(e).__name__}: {e}. Trying SSE…")

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


def _http_opener(url: str, http_client: httpx.AsyncClient):
    """An opener for `MCPClientManager._hold`: this client, the transport, the session."""
    async def opener(stack: AsyncExitStack) -> ClientSession:
        client = await stack.enter_async_context(http_client)
        read, write = await _open_http_session(stack, url, client)
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
        )
        await session.initialize()
        return session
    return opener


# ── Manager ────────────────────────────────────────────────────────────────────

class MCPClientManager:

    def __init__(self, exit_stack: AsyncExitStack):
        self.exit_stack = exit_stack
        self.sessions: Dict[str, ClientSession] = {}
        # Populated by `load()`, not here: __init__ cannot await, and the
        # registrations live in the store now. The one construction site awaits
        # connect_all() on the next line, which loads first.
        self.servers_config: List[Dict[str, Any]] = []
        # name → (the task that holds the session open, the event that closes it)
        self._owners: Dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
        # Closed with the app: a callback, not an entered context, so pushing
        # it from here touches no task's cancel scopes.
        exit_stack.push_async_callback(self.close_all)

    # ── Session ownership (F200) ───────────────────────────────────────────────

    async def _hold(self, name: str, opener) -> ClientSession:
        """Open a session in a task of its own, which holds it until it is closed.

        `opener(stack)` enters the transport and the `ClientSession` on `stack`
        and initializes. A transport is an anyio task group, which must be
        entered and left on one task. Opened in the caller's task — a request's,
        for Add server and Retry — it was entered inside the request's middleware
        scopes and parked on the lifespan's exit stack; when the request
        returned, Starlette's middleware left its own scope while the
        transport's was still open ("Attempted to exit a cancel scope that isn't
        the current tasks's current cancel scope") and the transport's tasks
        went with it. The server showed Connected and its tools never appeared
        (F200). Only a server connected at startup, in the lifespan's task, worked.

        A session opened again under the same name (a Retry) replaces the old
        one, which is closed rather than left running.
        """
        opened: asyncio.Future = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def own() -> None:
            try:
                async with AsyncExitStack() as stack:
                    session = await opener(stack)
                    opened.set_result(session)
                    await stop.wait()
            except Exception as exc:  # noqa: BLE001 — reported to the opener, or logged
                if not opened.done():
                    opened.set_exception(exc)
                else:
                    _log.info(f"[MCP] Session '{name}' closed with an error: {exc!r}")
            except BaseException as exc:
                if not opened.done():
                    opened.set_exception(exc)
                raise

        task = asyncio.create_task(own(), name=f"mcp-session:{name}")
        try:
            session = await opened
        except BaseException:
            await asyncio.gather(task, return_exceptions=True)
            raise
        await self._close(name)
        self._owners[name] = (task, stop)
        return session

    async def _close(self, name: str) -> None:
        """Close the session held under `name`, on the task that opened it."""
        owner = self._owners.pop(name, None)
        if owner is None:
            return
        task, stop = owner
        stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except Exception:  # noqa: BLE001 — a close that failed is still closed
            pass

    async def close_all(self) -> None:
        for name in list(self._owners):
            await self._close(name)

    # ── Persistence ────────────────────────────────────────────────────────────

    async def load(self) -> List[Dict[str, Any]]:
        """Read this tenant's MCP server registrations into memory."""
        from core.store.resources import load_mcp_servers
        try:
            self.servers_config = await load_mcp_servers()
        except Exception as e:
            _log.info(f"Error loading MCP servers config: {e}")
            self.servers_config = []
        return self.servers_config

    async def save_servers(self):
        from core.store.resources import replace_mcp_servers
        await replace_mcp_servers(self.servers_config)

    async def _set_status(self, name: str, status: str):
        for s in self.servers_config:
            if s["name"] == name:
                s["status"] = status
                break
        await self.save_servers()

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
                _server.tool_router.register(
                    name, tool.name, session_key=agent_key, alias=False
                )
            _log.debug(f"[MCP] Registered {len(tools.tools)} tools for '{name}': "
                  f"{[t.name for t in tools.tools]}")
        except Exception as e:
            _log.debug(f"[MCP] Tool registration failed for '{name}': {e}")

    # ── Stdio connection ───────────────────────────────────────────────────────

    async def connect_stdio_server(self, config: Dict) -> Optional[ClientSession]:
        name    = config["name"]
        command = config.get("command", "")
        args    = config.get("args", [])
        env_vars = config.get("env", {})

        if not command:
            _log.info(f"Skipping '{name}': no command")
            return None

        # Gate at the sink, not only at the callers. Spawning a stdio server is
        # arbitrary local command execution, so the check belongs on the code
        # path that actually spawns — otherwise any new (or overlooked) call site
        # silently bypasses it. POST /api/import did exactly that: it wrote a
        # bundle's MCP configs to disk and called this method directly, skipping
        # the add_server/reconnect_server/connect_all gates entirely.
        if not stdio_mcp_allowed():
            _log.debug(f"[MCP] Refusing to spawn stdio server '{name}': "
                  f"stdio MCP servers are disabled on this deployment.")
            await self._set_status(name, "disconnected")
            return None
        try:
            check_stdio_command_allowed(command)
        except ValueError as e:
            _log.debug(f"[MCP] Refusing to spawn stdio server '{name}': {e}")
            await self._set_status(name, "disconnected")
            return None

        env = os.environ.copy()
        env.update(env_vars)

        _log.info(f"Connecting to stdio MCP server '{name}' ({command} {args})...")

        async def opener(stack: AsyncExitStack) -> ClientSession:
            params = StdioServerParameters(command=command, args=args, env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=_SESSION_READ_TIMEOUT)
            )
            await session.initialize()
            return session

        try:
            session = await self._hold(name, opener)
        except Exception as e:
            _log.info(f"Failed stdio connect '{name}': {e}")
            return None
        self.sessions[name] = session
        _log.info(f"Connected to stdio MCP server '{name}'.")
        return session

    # ── Remote connection: bearer token ───────────────────────────────────────

    async def connect_remote_server(self, config: Dict) -> Optional[ClientSession]:
        """Connect to a remote MCP server with an optional pre-auth bearer token."""
        name  = config["name"]
        url   = config["url"]
        token = config.get("token", "")

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        _log.info(f"Connecting to remote MCP server '{name}' ({url})...")
        try:
            session = await self._hold(name, _http_opener(url, httpx.AsyncClient(headers=headers, follow_redirects=True)))
        except Exception as e:
            _log.info(f"Failed remote connect '{name}': {e}")
            return None
        self.sessions[name] = session
        _log.info(f"Connected to remote MCP server '{name}'.")
        return session

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
            session = await self._hold(
                name, _http_opener(url, httpx.AsyncClient(auth=oauth_provider, follow_redirects=True)))
            self.sessions[name] = session

            # Record where a refresh goes, now, while the provider still holds
            # the metadata it discovered during this flow. It is thrown away
            # with the process otherwise, and a refresh in a later process has
            # no way to find it again — which is what `_discover_token_endpoint`
            # exists to paper over for servers authorised before this line.
            try:
                metadata = getattr(oauth_provider.context, "oauth_metadata", None)
                endpoint = getattr(metadata, "token_endpoint", None)
                if endpoint:
                    await StoreTokenStorage(name).set_token_endpoint(str(endpoint))
            except Exception as e:
                _log.debug(f"[MCP] Could not record the token endpoint for '{name}': {e}")

            await self._set_status(name, "connected")
            await self._auto_register(name)    # ← register tools into agent_sessions
            _log.info(f"OAuth complete — connected to '{name}'.")
        except Exception as e:
            _log.info(f"OAuth connection failed for '{name}': {e}")
            await self._set_status(name, "disconnected")
            if not auth_url_future.done():
                auth_url_future.set_exception(e)

    # ── Silent renewal, before a reconnect ────────────────────────────────────

    async def _renew_if_expired(self, name: str, url: str) -> bool:
        """Refresh this server's access token if it has expired. True if renewed.

        **Why this exists rather than leaning on the SDK.** `OAuthClientProvider`
        has a refresh path and it never runs: `_initialize()` restores the tokens
        but not `token_expiry_time`, `is_token_valid()` treats a `None` expiry as
        valid, so the proactive refresh is skipped and the dead token is sent.
        The `401` that comes back does not trigger a refresh either — it starts a
        *full* re-authorisation, straight into the `noop_callback` that
        `_connect_remote_cached` deliberately installs, which raises. The server
        then falls through to a no-auth connect, which an authenticated server
        refuses, and the person is asked to click Authorize again. Every restart.

        Even with the expiry restored the SDK could not finish the job cold: its
        `_refresh_token()` takes the endpoint from `context.oauth_metadata`,
        which discovery fills in during an interactive flow and which is `None`
        in a fresh process, so it would POST to `urljoin(server_url, "/token")`.
        Those lines are marked `# pragma: no cover` upstream, which is a fair
        warning about how much weight to put on them.

        So the renewal is explicit and reuses `core/mcp_oauth.py` — the same
        exchange the worker performs, in one place, with the failure visible in
        a log rather than inferred from a re-auth prompt.
        """
        from core import mcp_oauth

        storage = StoreTokenStorage(name)
        tokens = await storage.get_tokens()
        if not tokens or not tokens.refresh_token:
            return False

        # An unknown expiry means "stored before this was recorded", and is
        # treated as expired. Assuming the other way is the bug this fixes; the
        # cost of being wrong here is one refresh nobody needed.
        expires_at = await storage.get_expires_at()
        if expires_at is not None and time.time() < expires_at - _EXPIRY_SKEW_SECONDS:
            return False

        client_info = await storage.get_client_info()
        if not client_info or not client_info.client_id:
            return False

        endpoint = await storage.get_token_endpoint()
        if not endpoint:
            endpoint = await _discover_token_endpoint(url)
            if endpoint:
                await storage.set_token_endpoint(endpoint)
        if not endpoint:
            _log.debug(f"[MCP] No token endpoint known for '{name}' — cannot refresh.")
            return False

        cfg = {
            "name": name,
            "refresh_token": tokens.refresh_token,
            "token_endpoint": endpoint,
            "client_id": client_info.client_id,
            "client_secret": client_info.client_secret or "",
        }
        fresh = await mcp_oauth.refresh_access_token(cfg)
        if not fresh:
            return False

        # `set_tokens` writes the new absolute expiry, and a rotated refresh
        # token is carried across — several providers issue a new one every time
        # and invalidate the old, so keeping only the access token works exactly
        # once and fails forever after.
        renewed = OAuthToken(**fresh)
        if not renewed.refresh_token:
            renewed.refresh_token = tokens.refresh_token
        await storage.set_tokens(renewed)
        _log.debug(f"[MCP] Renewed the access token for '{name}'.")
        return True

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

        storage = StoreTokenStorage(name)
        has_tokens = bool(await storage.get_tokens())

        if has_tokens:
            # Renew first, so the provider below is handed a token that works.
            # Once storage holds a live token the SDK's own path is correct
            # again — it is only the *expired* case it cannot get right.
            try:
                await self._renew_if_expired(name, url)
            except Exception as e:
                # A renewal that blew up must not cost us the connect attempt
                # the old token might still satisfy.
                _log.debug(f"[MCP] Token renewal for '{name}' failed: {e}")

            # ── OAuth path: try cached tokens, allow silent refresh ────────────
            async def noop_redirect(auth_url: str) -> None:
                _log.debug(f"[MCP] Token refresh failed for '{name}' — re-auth needed.")

            async def noop_callback() -> tuple[str, Optional[str]]:
                raise RuntimeError("Interactive OAuth not available at startup")

            oauth_provider = _make_oauth_provider(
                name, url,
                redirect_handler=noop_redirect,
                callback_handler=noop_callback,
            )
            try:
                session = await self._hold(
                    name, _http_opener(url, httpx.AsyncClient(auth=oauth_provider, follow_redirects=True)))
                self.sessions[name] = session
                _log.debug(f"[MCP] Reconnected '{name}' with cached OAuth tokens.")
                return session
            except Exception as e:
                _log.debug(f"[MCP] Cached OAuth reconnect failed for '{name}': {e}. Falling back to direct connect.")

        # ── Direct path: no token, no OAuth — server may not need auth ───────
        _log.debug(f"[MCP] Attempting direct (no-auth) connection to '{name}' ({url})...")
        try:
            session = await self._hold(name, _http_opener(url, httpx.AsyncClient(follow_redirects=True)))
        except Exception as e:
            _log.debug(f"[MCP] Direct connect also failed for '{name}': {e}")
            return None
        self.sessions[name] = session
        _log.debug(f"[MCP] Connected '{name}' via direct (no-auth) connection.")
        return session

    # ── connect_all (startup) ──────────────────────────────────────────────────

    async def connect_all(self):
        await self.load()

        for config in self.servers_config:
            name = config.get("name")
            if not name or name in self.sessions:
                continue

            server_type = config.get("server_type", "stdio")
            if server_type == "stdio":
                # Do not re-spawn persisted stdio servers when the capability is
                # disabled — otherwise a saved (possibly malicious) config would
                # execute on every boot. See GHSA-3j67-x3j8-r32x.
                if not stdio_mcp_allowed():
                    _log.debug(f"[MCP] Skipping stdio server '{name}': stdio MCP disabled on this deployment.")
                    await self._set_status(name, "disabled")
                    continue
                session = await self.connect_stdio_server(config)
            elif config.get("token"):
                session = await self.connect_remote_server(config)
            else:
                session = await self._connect_remote_cached(config)

            if session:
                await self._set_status(name, "connected")
                await self._auto_register(name)   # ← register on startup
            else:
                # Remote servers without a bearer token use OAuth — failure likely means re-auth needed.
                # Stdio and bearer-token servers just go disconnected.
                if server_type == "remote" and not config.get("token"):
                    await self._set_status(name, "reauth_needed")
                else:
                    await self._set_status(name, "disconnected")
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
            if not stdio_mcp_allowed():
                raise PermissionError("stdio MCP server registration is disabled on this deployment.")
            if not command:
                raise ValueError("Command is required for stdio servers.")
            check_stdio_command_allowed(command)
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
        await self.save_servers()

        if server_type == "stdio":
            session = await self.connect_stdio_server(new_config)
            if session:
                await self._set_status(name, "connected")
                new_config["status"] = "connected"
                await self._auto_register(name)
            return {"config": new_config, "connected": bool(session), "status": new_config["status"]}

        # Remote
        if token:
            session = await self.connect_remote_server(new_config)
            if session:
                await self._set_status(name, "connected")
                new_config["status"] = "connected"
                await self._auto_register(name)
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
            if not stdio_mcp_allowed():
                raise PermissionError("stdio MCP servers are disabled on this deployment.")
            session = await self.connect_stdio_server(config)
        elif config.get("token"):
            session = await self.connect_remote_server(config)
        else:
            session = await self._connect_remote_cached(config)

        if session:
            await self._set_status(name, "connected")
            await self._auto_register(name)   # ← register on manual retry
            return True
        return False

    # ── remove_server ──────────────────────────────────────────────────────────

    async def remove_server(self, name: str) -> bool:
        self.servers_config = [s for s in self.servers_config if s["name"] != name]
        await self.save_servers()
        self.sessions.pop(name, None)
        await self._close(name)
        await StoreTokenStorage(name).delete_all()
        return True

    # ── helpers ────────────────────────────────────────────────────────────────

    def get_server_config(self, name: str) -> Optional[Dict]:
        for s in self.servers_config:
            if s["name"] == name:
                return s
        return None
