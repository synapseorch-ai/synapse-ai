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
- When login is on, or an access token is configured (login off, D124), an
  internal request ALSO needs a valid session (S9) — see `_needs_session` below.
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
import hmac
import ipaddress
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Addresses treated as local. We check request.client rather than reading
# X-Forwarded-For ourselves — but note what request.client is: uvicorn's default
# `proxy_headers` trusts 127.0.0.1 (the Next proxy) and substitutes the address
# that proxy forwards, taking the rightmost untrusted hop, so a header a remote
# caller sets does not survive it.
_LOOPBACK_NAMES = frozenset({"localhost"})

# Root-mounted routes that are internal despite not living under /api/.
# Matched as exact path or path-prefixed segment so /chat and /chat/stream are
# both covered while an unrelated future route like /chatbot-docs is not.
_INTERNAL_ROOT_PREFIXES = ("/chat",)


def _is_loopback(request: Request) -> bool:
    """Whether the caller is this machine, however its address is spelled.

    Parsed rather than string-compared (F12): a Windows browser reaching WSL2's
    Next proxy is forwarded as `::ffff:127.0.0.1`, an IPv4-mapped IPv6 address,
    and the string check refused every `/api/*` call it made. Anything that does
    not parse is not loopback.
    """
    client = request.client
    if not client or not client.host:
        return False
    if client.host in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


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

#: Platform webhooks: Telegram, Slack, Discord, Teams and WhatsApp post here with
#: no browser and no session, and each is verified by its platform's own check.
#: The rest of `/api/messaging/` (channel settings) stays behind the gate.
_WEBHOOK = re.compile(r"^/api/messaging/[^/]+/webhook(/|$)")

SESSION_HEADER = "X-Synapse-Session"
SESSION_COOKIE = "synapse_session"


def _login_required() -> bool:
    """Login is on and set up — the proxy's own test (`/api/auth/status`)."""
    from core.config import load_settings

    s = load_settings()
    return bool(
        s.get("login_enabled") and s.get("login_username") and s.get("login_password_hash")
    )


def _gate_required() -> bool:
    """A session is needed: login is on, or login is off but an access token
    is configured, so the install is reachable only with its link (D124)."""
    from core.user_auth import access_token

    return _login_required() or bool(access_token())


def session_ok(token: str) -> bool:
    """Whether this session opens the app now.

    A password session is valid while it is ours and unexpired. An access-link
    session (it carries `atf`) also needs login to be off and the token it was
    made from to still be the install's — so turning login on, or rotating the
    link, ends it.
    """
    from core.user_auth import access_fingerprint, access_token, session_claims

    claims = session_claims(token)
    if claims is None:
        return False
    atf = claims.get("atf")
    if atf is None:
        return True
    current = access_token()
    return bool(current) and not _login_required() and hmac.compare_digest(
        str(atf), access_fingerprint(current)
    )


def _needs_session(path: str) -> bool:
    if _WEBHOOK.match(path):
        return False
    return not any(path.startswith(p) for p in _SESSIONLESS_PREFIXES)


def _has_session(request: Request) -> bool:
    token = request.headers.get(SESSION_HEADER) or request.cookies.get(SESSION_COOKIE) or ""
    return session_ok(token)


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
        # With login off and an access token configured, the same: the link
        # is the credential (D124).
        if _needs_session(path) and _gate_required() and not _has_session(request):
            return _LOGIN_REQUIRED

        return await call_next(request)


