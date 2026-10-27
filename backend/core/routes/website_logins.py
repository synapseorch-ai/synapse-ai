"""Settings → Website logins (BRW2, D330, F1079): the sessions kept for runs, and Forget.

Only what a login is (its sites, owner and dates) leaves here; the session itself never does. Logging in to a site
opens the browser's pane for the person alone (BRW1's `POST /api/browser/login-sessions`).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from core import website_logins

router = APIRouter()


@router.get("/api/website-logins")
async def list_logins():
    return {"logins": await website_logins.list_logins()}


@router.delete("/api/website-logins/{login_id}")
async def forget_login(login_id: str):
    if not await website_logins.forget(login_id):
        raise HTTPException(status_code=404, detail="That login is gone already.")
    return {"forgotten": True}
