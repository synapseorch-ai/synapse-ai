"""
Internal Token Middleware
-------------------------
Protects all /api/* routes from direct external access.

Only the Next.js frontend knows the SYNAPSE_INTERNAL_TOKEN and injects it
as an X-Synapse-Internal header on every proxied request. External callers
that try to hit /api/settings, /api/agents, etc. directly will get 403.

Rules:
- /api/v1/*, /api/v2/*, ... → SKIP (external versioned API; uses API key auth instead)
- /docs, /openapi.json, /redoc  → SKIP (FastAPI docs)
- /chat*, /auth/*    → SKIP (direct backend routes, not under /api/)
- /api/*             → REQUIRE X-Synapse-Internal header
- If SYNAPSE_INTERNAL_TOKEN is not set → LOOPBACK-ONLY: allow requests whose
  direct peer is 127.0.0.1/::1 (local dev, same-container proxy) and 403 any
  remote caller. This closes the unauthenticated-RCE hole on network-exposed
  token-less deployments (GHSA-3j67-x3j8-r32x). Docker images auto-generate a
  token (docker/entrypoint.sh) so they never rely on this fallback.
"""
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Direct-peer addresses treated as local. We deliberately check request.client
# (the immediate TCP peer), NOT X-Forwarded-For, which is attacker-spoofable.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(request: Request) -> bool:
    client = request.client
    return bool(client) and client.host in _LOOPBACK_HOSTS


class InternalTokenMiddleware(BaseHTTPMiddleware):
    """Block direct access to internal /api/* routes without the internal token."""

    def __init__(self, app):
        super().__init__(app)
        self.token = os.getenv("SYNAPSE_INTERNAL_TOKEN", "")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # ── Skips run FIRST, independent of the internal token ──────────────────
        # These routes are either externally authenticated (versioned API → API
        # key) or intentionally public, so they must stay reachable even when the
        # token is unset/loopback-gated. This lets external clients hit the open
        # /api/v1|v2 API through the frontend proxy (or a directly-published
        # backend port) without the internal frontend token.

        # External versioned API (v1, v2, ...) — uses require_api_key, not the
        # internal token. Match any /api/v<N> prefix so future versions are exempt.
        if re.match(r"^/api/v\d+(/|$)", path):
            return await call_next(request)

        # MCP OAuth callback — called by external OAuth providers, not the frontend.
        if path == "/api/mcp/oauth/callback":
            return await call_next(request)

        # FastAPI docs.
        if path in ("/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # Non-API routes (chat, auth, health, websocket, etc.).
        if not path.startswith("/api/"):
            return await call_next(request)

        # ── Internal /api/* surface — the sensitive routes (settings, agents,
        # mcp/servers, …) that rely on the internal token for protection ────────
        if not self.token:
            # No token configured → permissive ONLY for loopback callers. A remote
            # client hitting a token-less backend directly is rejected, so the
            # internal surface is never exposed unauthenticated over the network
            # even in a misconfigured/bare-metal deployment (GHSA-3j67-x3j8-r32x).
            if _is_loopback(request):
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: internal token not configured"},
            )

        # Token configured → require the matching header.
        provided = request.headers.get("X-Synapse-Internal", "")
        if provided != self.token:
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden"},
            )

        return await call_next(request)
