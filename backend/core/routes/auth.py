"""
Authentication routes: Google OAuth + Synapse login gate.
"""
import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel
from services.google import get_auth_url, finish_auth
from core.config import load_settings
from core.user_auth import verify_password, create_session_token

router = APIRouter()

# Read ports from env so they always match the running servers.
_BACKEND_PORT = int(os.getenv("SYNAPSE_BACKEND_PORT", "8765"))
_FRONTEND_PORT = int(os.getenv("SYNAPSE_FRONTEND_PORT", "3000"))

# This must match exactly what's registered in your Google Cloud Console
# OAuth 2.0 Client → Authorized redirect URIs.
# Add "http://localhost:<SYNAPSE_BACKEND_PORT>/auth/callback" to your OAuth app.
REDIRECT_URI = f"http://localhost:{_BACKEND_PORT}/auth/callback"
_FRONTEND_BASE = f"http://localhost:{_FRONTEND_PORT}"


@router.get("/auth/login")
async def login():
    try:
        auth_url = get_auth_url(redirect_uri=REDIRECT_URI)
        return RedirectResponse(auth_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/api/auth/status")
async def auth_status():
    """Returns whether login is enabled and fully configured."""
    s = load_settings()
    login_enabled = s.get("login_enabled", False)
    login_configured = bool(
        login_enabled
        and s.get("login_username")
        and s.get("login_password_hash")
    )
    return {"login_enabled": login_enabled, "login_configured": login_configured}


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
async def callback(code: str, state: str = None):
    try:
        finish_auth(code=code, redirect_uri=REDIRECT_URI)
        # Redirect back to the frontend with a success flag so the UI can refresh
        return RedirectResponse(f"{_FRONTEND_BASE}?google_auth=success")
    except Exception as e:
        return RedirectResponse(f"{_FRONTEND_BASE}?google_auth=error&reason={str(e)}")
