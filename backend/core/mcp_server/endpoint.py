"""The MCP server over streamable HTTP, inside the backend (D125).

Stateless, JSON responses: every POST carries its own bearer and nothing is kept
between them, so there is no session to lose across a restart or between
replicas. The shape is cloud's google-mcp (`api/google_mcp/main.py`).

The route is served by a plain ASGI wrapper rather than a `Mount`: a mount
redirects `/…/mcp` to `/…/mcp/` with a 307, which MCP clients do not follow for
a POST. The wrapper sits outside every other middleware, so:

* the key is checked here, always — loopback included. The path is under
  `/api/v1`, which `InternalTokenMiddleware` leaves to the API key, the same as
  every v1 route;
* CORS does not apply, so a browser page on another origin cannot call it.
"""
from __future__ import annotations

import contextlib
import json
from typing import AsyncIterator

from starlette.requests import Request

from core.mcp_server.server import (
    Authorize,
    Bind,
    Permits,
    ServerModule,
    authorize_api_key,
    bind_settings,
    build_server,
    permit_all,
    this_process,
)

#: Where the endpoint is served, on both products (D124): `{origin}/api/v1/mcp`.
#: A self-host's UI origin reaches it through the existing `/api/v1` rewrite;
#: cloud's public edge serves `/api/v1/*` and `/api/v2/*` only.
MCP_PATH = "/api/v1/mcp"


async def _send_json(send, status: int, body: dict, headers: list[tuple[bytes, bytes]] = ()) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()), *headers],
    })
    await send({"type": "http.response.body", "body": payload})


class McpEndpoint:
    def __init__(self, authorize: Authorize = authorize_api_key, bind: Bind = bind_settings,
                 server_module: ServerModule = this_process, permits: Permits = permit_all) -> None:
        self._authorize = authorize
        self.server = build_server(authorize, bind, server_module=server_module, permits=permits)
        self._manager = None

    @property
    def running(self) -> bool:
        return self._manager is not None

    @contextlib.asynccontextmanager
    async def serve(self) -> AsyncIterator[None]:
        """Run the session manager for as long as the app runs.

        A manager's `run()` can be entered once, so each lifespan builds its own.
        """
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

        manager = StreamableHTTPSessionManager(
            app=self.server, event_store=None, json_response=True, stateless=True,
        )
        async with manager.run():
            self._manager = manager
            try:
                yield
            finally:
                self._manager = None

    async def __call__(self, scope, receive, send) -> None:
        if self._manager is None:
            await _send_json(send, 503, {"detail": "The MCP endpoint is starting."})
            return
        if await self._authorize(Request(scope)) is None:
            await _send_json(
                send, 401, {"detail": "A valid Synapse API key is required (Authorization: Bearer sk-syn-…)."},
                [(b"www-authenticate", b"Bearer")],
            )
            return
        if scope.get("method") != "POST":
            # Stateless: no server-sent stream (GET) and no session to end (DELETE).
            # The SDK held a GET open with no headers for ever; the spec's answer is
            # 405, which clients read as "no stream" — Claude Code waited on it and
            # never got as far as listing the tools.
            await _send_json(send, 405, {"detail": "This MCP endpoint takes POST only."},
                             [(b"allow", b"POST")])
            return
        await self._manager.handle_request(scope, receive, send)


class McpDispatch:
    """ASGI middleware: `MCP_PATH` goes to the endpoint, everything else to the app."""

    def __init__(self, app, endpoint: McpEndpoint | None = None, path: str = MCP_PATH) -> None:
        self.app = app
        self.endpoint = endpoint or default_endpoint()
        self.path = path.rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == self.path:
            await self.endpoint(scope, receive, send)
            return
        await self.app(scope, receive, send)


_default: McpEndpoint | None = None


def default_endpoint() -> McpEndpoint:
    """The backend's own endpoint, served by `core.server` and started by its lifespan."""
    global _default
    if _default is None:
        _default = McpEndpoint()
    return _default
