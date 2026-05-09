"""
Internal Token Middleware
-------------------------
Protects all /api/* routes from direct external access.

Only the Next.js frontend knows the SYNAPSE_INTERNAL_TOKEN and injects it
as an X-Synapse-Internal header on every proxied request. External callers
that try to hit /api/settings, /api/agents, etc. directly will get 403.

Rules:
- /api/v1/*          → SKIP (uses API key auth instead)
- /docs, /openapi.json, /redoc  → SKIP (FastAPI docs)
- /chat*, /auth/*    → SKIP (direct backend routes, not under /api/)
- /api/*             → REQUIRE X-Synapse-Internal header
- If SYNAPSE_INTERNAL_TOKEN is not set → permissive (backward compatible)
"""
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class InternalTokenMiddleware(BaseHTTPMiddleware):
    """Block direct access to internal /api/* routes without the internal token."""

    def __init__(self, app):
        super().__init__(app)
        self.token = os.getenv("SYNAPSE_INTERNAL_TOKEN", "")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # If no token configured, be permissive (local dev / backward compat)
        if not self.token:
            return await call_next(request)

        # Skip: V1 API routes (they use API key auth)
        if path.startswith("/api/v1/") or path == "/api/v1":
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
