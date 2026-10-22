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
- /auth/*            → SKIP (OAuth redirects; browser-navigated, cannot carry a header)
- /api/*, /chat*     → REQUIRE X-Synapse-Internal header
- When login is on, an internal request ALSO needs a valid session (S9) — see
  `_needs_session` below.
- If SYNAPSE_INTERNAL_TOKEN is not set → LOOPBACK-ONLY: allow requests whose
  direct peer is 127.0.0.1/::1 (local dev, same-container proxy) and 403 any
  remote caller. This closes the unauthenticated-RCE hole on network-exposed
  token-less deployments (GHSA-3j67-x3j8-r32x). Docker images auto-generate a
  token (docker/entrypoint.sh) so they never rely on this fallback.

Why /chat is gated even though it is not under /api/:
  POST /chat and /chat/stream (core/routes/chat.py) run the full ReAct loop with
  the complete tool surface — bash, execute_python, SQL, every MCP server. They
  are mounted at the root, so the "not /api/ → skip" rule let any caller who
  could reach the port drive them with no credentials at all.
  The public, externally-authenticated chat API is /api/v1/chat (require_api_key)
  — a different route. Root /chat is reached only by the Next.js route handler
  (frontend/src/app/api/chat/**), which already sends X-Synapse-Internal via
  backendHeaders(), so gating it is transparent to the UI.
"""
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Direct-peer addresses treated as local. We deliberately check request.client
# (the immediate TCP peer), NOT X-Forwarded-For, which is attacker-spoofable.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Root-mounted routes that are internal despite not living under /api/.
# Matched as exact path or path-prefixed segment so /chat and /chat/stream are
# both covered while an unrelated future route like /chatbot-docs is not.
_INTERNAL_ROOT_PREFIXES = ("/chat",)


def _is_loopback(request: Request) -> bool:
    client = request.client
    return bool(client) and client.host in _LOOPBACK_HOSTS


# ── The login gate's second layer (S9) ──────────────────────────────────────
#
# The Next proxy is the login gate, and it used to be the only one: past it the
# proxy attaches the internal token, and the token was all this middleware
# checked. The proxy skipped its gate for a `synapse_auth_cache=no_auth_required`
# cookie the client could set itself, so one forged cookie opened a
# login-protected install — settings, keys, even switching login off. The proxy
# no longer trusts that cookie, and this makes sure no proxy mistake can open the
# internal surface again: with login on, the caller must also show a session.

#: Internal paths the proxy's gate lets through without a session, mirrored:
#: login itself, and the widget (each call carries its own visitor token).
#: `/api/v<N>` (an API key) and the OAuth callback are skipped before this runs.
_SESSIONLESS_PREFIXES = ("/api/auth/", "/api/widget/")

SESSION_HEADER = "X-Synapse-Session"
SESSION_COOKIE = "synapse_session"


def _login_required() -> bool:
    """Login is on and set up — the proxy's own test (`/api/auth/status`)."""
    from core.config import load_settings

    s = load_settings()
    return bool(
        s.get("login_enabled") and s.get("login_username") and s.get("login_password_hash")
    )


def _session_user(token: str) -> str | None:
    from core.user_auth import verify_session_token

    return verify_session_token(token) if token else None


def _needs_session(path: str) -> bool:
    return not any(path.startswith(p) for p in _SESSIONLESS_PREFIXES)


def _has_session(request: Request) -> bool:
    token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE) or ""
    return _session_user(token) is not None


_LOGIN_REQUIRED = JSONResponse(status_code=401, content={"detail": "Login required"})


def _is_internal_root_route(path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _INTERNAL_ROOT_PREFIXES
    )


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

        # Non-API routes (auth redirects, health, websocket, etc.) — EXCEPT the
        # root-mounted internal routes, which are gated below. /chat drives the
        # full agent tool surface and must never be reachable unauthenticated.
        if not path.startswith("/api/") and not _is_internal_root_route(path):
            return await call_next(request)

        # ── Internal surface — /api/* plus the root-mounted internal routes.
        # The sensitive routes (settings, agents, mcp/servers, chat, …) that rely
        # on the internal token for protection ──────────────────────────────────
        if not self.token:
            # No token configured → permissive ONLY for loopback callers. A remote
            # client hitting a token-less backend directly is rejected, so the
            # internal surface is never exposed unauthenticated over the network
            # even in a misconfigured/bare-metal deployment (GHSA-3j67-x3j8-r32x).
            if not _is_loopback(request):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden: internal token not configured"},
                )
        else:
            # Token configured → require the matching header.
            provided = request.headers.get("X-Synapse-Internal", "")
            if provided != self.token:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Forbidden"},
                )

        # With login on, the token (or loopback) proves the request came
        # through our proxy; it does not prove a signed-in person sent it (S9).
        if _needs_session(path) and _login_required() and not _has_session(request):
            return _LOGIN_REQUIRED

        return await call_next(request)


