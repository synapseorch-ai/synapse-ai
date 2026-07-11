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

        # No token configured → permissive ONLY for loopback callers. A remote
        # client hitting a token-less backend directly is rejected, so the
        # internal /api/* surface is never exposed unauthenticated over the
        # network even in a misconfigured/bare-metal deployment.
        if not self.token:
            if _is_loopback(request):
                return await call_next(request)
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden: internal token not configured"},
            )

        # Skip: external versioned API routes (v1, v2, ...) — they use API key
        # auth (require_api_key), not the internal frontend token. Match any
        # /api/v<N> prefix so future versions are exempt automatically.
        if re.match(r"^/api/v\d+(/|$)", path):
            return await call_next(request)

        # Skip: MCP OAuth callback — called by external OAuth providers, not frontend
        if path == "/api/mcp/oauth/callback":
            return await call_next(request)

        # Skip: FastAPI docs
        if path in ("/docs", "/openapi.json", "/redoc"):
            return await call_next(request)

        # Skip: non-API routes (chat, auth, health, websocket, etc.)
        if not path.startswith("/api/"):
            return await call_next(request)

        # This is an /api/* route — require internal token
        provided = request.headers.get("X-Synapse-Internal", "")
        if provided != self.token:
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden"},
            )

        return await call_next(request)
