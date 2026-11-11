"""
Authentication routes: Google OAuth + Synapse login gate.
"""
import os
from urllib.parse import urlencode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from services.google import get_auth_url, finish_auth
from core.config import load_settings
from core.internal_auth import signed_in
from core.user_auth import verify_password, create_session_token, verify_session_token

router = APIRouter()

# Read ports from env so they always match the running servers.
_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
_FRONTEND_PORT = int(os.getenv("SYNAPSE_FRONTEND_PORT", "3000"))

# This must match exactly what's registered in your Google Cloud Console
# OAuth 2.0 Client → Authorized redirect URIs.
# Add "http://localhost:<SYNAPSE_BACKEND_PORT>/auth/callback" to your OAuth app.
REDIRECT_URI = f"http://localhost:{_BACKEND_PORT}/auth/callback"
_FRONTEND_BASE = f"http://localhost:{_FRONTEND_PORT}"


def _back_to_app(result: str, reason: str | None = None) -> RedirectResponse:
    """The app, told how the Google sign-in went; the reason encoded, so its text cannot add to the address (F1535)."""
    query = {"google_auth": result, **({"reason": reason} if reason else {})}
    return RedirectResponse(f"{_FRONTEND_BASE}?{urlencode(query)}")


@router.get("/auth/login")
async def login(request: Request):
    # A browser navigates here, so the internal-token middleware does not gate it, and the frontend's proxy lets /auth/
    # past its login gate. Starting a sign-in takes a session when the install has a gate (F1535): a stranger on the
    # network, or a page in someone else's browser, cannot start one or push out the person's. The app's own
    # Settings → Integrations link comes through the frontend, which passes the person's session cookie on.
    if not signed_in(request):
        return _back_to_app("error", "Sign in to Synapse first, then connect Google from Settings → Integrations.")
    try:
        auth_url = await get_auth_url(redirect_uri=REDIRECT_URI)
        return RedirectResponse(auth_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/api/auth/status")
async def auth_status(request: Request):
    """Returns whether login is enabled/configured and whether THIS request's
    session token is valid.

    The proxy (frontend/src/proxy.ts) forwards the `synapse_session` cookie via
    the `X-Synapse-Session` header. The backend is the single owner of
    SYNAPSE_JWT_SECRET, so `authenticated` is the authoritative gate when the
    proxy's local JWT fast-path can't verify (e.g. the frontend process has a
    different/empty secret). `verify_session_token` never raises, so a missing
    backend secret yields `authenticated: false` rather than a 500.
    """
    s = load_settings()
    login_enabled = s.get("login_enabled", False)
    login_configured = bool(
        login_enabled
        and s.get("login_username")
        and s.get("login_password_hash")
    )
    from core.internal_auth import session_ok
    from core.user_auth import access_token

    session = request.headers.get("X-Synapse-Session", "") or request.cookies.get("synapse_session", "")
    return {
        "login_enabled": login_enabled,
        "login_configured": login_configured,
        # Login off, but this install is reachable only with its access link
        # (D124): the gate asks for the link instead of a password.
        "access_required": bool(access_token()) and not login_configured,
        # The middleware's own rule, so the gate and the backend never disagree
        # about who is signed in (an access-link session dies with its token).
        "authenticated": session_ok(session),
    }


class AccessRequest(BaseModel):
    token: str


@router.post("/api/auth/access")
async def access_link(body: AccessRequest):
    """Swap the install's access token for a session (login off, D124).

    409 with login on: a password always wins, so the link is no way around it.
    404 when no access token is configured — nothing to present.
    """
    from core.internal_auth import _login_required
    from core.user_auth import access_token, access_token_matches, create_access_session

    if _login_required():
        raise HTTPException(status_code=409, detail="Login is on. Sign in with your username and password.")
    if not access_token():
        raise HTTPException(status_code=404, detail="This install has no access link.")
    if not access_token_matches(body.token.strip()):
        raise HTTPException(status_code=401, detail="That is not this install's access link.")
    return {"success": True, "token": create_access_session()}


@router.post("/api/auth/login")
async def user_login(body: LoginRequest):
    """Validate username/password and return a signed JWT on success."""
    s = load_settings()
    if not s.get("login_enabled"):
        return {"success": True, "token": None}
    stored_username = s.get("login_username", "")
    stored_hash = s.get("login_password_hash", "")
    if not (stored_username and stored_hash):
        raise HTTPException(status_code=500, detail="Login is enabled but credentials are not configured")
    if body.username != stored_username or not verify_password(body.password, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"success": True, "token": create_session_token(body.username)}


@router.post("/api/auth/logout")
async def user_logout():
    """Stateless logout — cookie cleared by the Next.js route handler."""
    return {"success": True}


@router.get("/auth/callback")
async def callback(code: str = "", state: str | None = None, error: str | None = None):
    """Google sends the person back here. Only the callback of a sign-in started here, by its `state`, is kept
    (F1535); anything else is refused in words and keeps nothing. No session is asked for: Google sends the browser
    to the backend's own address, which may not carry the app's cookie."""
    if error:  # the person said no on Google's screen, or Google refused: nothing to exchange
        return _back_to_app("error", f"Google did not connect: {error}.")
    try:
        await finish_auth(code=code, state=state)
        # Redirect back to the frontend with a success flag so the UI can refresh
        return _back_to_app("success")
    except Exception as e:
        return _back_to_app("error", str(e))
