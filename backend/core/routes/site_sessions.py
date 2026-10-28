"""The Sessions library (BRW1, D328, F1138/F1139; DESIGN §v5.1): the sites an install keeps itself signed in to for
its agents, and Remove.

Only what a session is (its site, owner and dates, and where it is picked) leaves here; the session itself never
does. Keeping one opens the browser's pane for the person alone, on that site (`POST /api/site-sessions/new`); the
person signs in and keeps it (`POST /api/browser/{sid}/finish-session`). Sharing is cloud's: here one person uses the
install, so PATCH answers 404. `/api/website-logins` answers as before for one release (BRW2's F1079 screen).
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core import site_sessions
from core.browser import client as browser_client

router = APIRouter()


def person() -> str:
    """Who keeps a session here: the install's one sign-in, or "you" without one."""
    return site_sessions.install_person()


def _site_of(text: str) -> str:
    """A site from what the person typed: a host or an address, reduced to its registrable domain."""
    from core.sites import registrable_domain
    raw = (text or "").strip().lower()
    host = urlparse(raw if "://" in raw else f"//{raw}").hostname or ""
    if not host or "." not in host or "@" in raw:
        raise HTTPException(status_code=422, detail=f"'{text}' is not a site: give one such as portal.example.com.")
    return registrable_domain(host)


@router.get("/api/site-sessions")
async def list_site_sessions():
    from core.browser.picks import uses_of
    rows = await site_sessions.list_sessions()
    me, uses = person(), await uses_of([r["id"] for r in rows])
    # Every session is listed (the library shows each exists); `usable` says whether the person may pick it: theirs,
    # or shared with them (F1167). Here sharing is none, so usable is mine.
    return {"sessions": [{**r, "owner_name": r["owner"], "shared_with": [], "mine": r["owner"] == me,
                          "usable": r["owner"] == me, "uses": uses[r["id"]]} for r in rows]}


class NewSession(BaseModel):
    site: str


@router.post("/api/site-sessions/new")
async def new_site_session(body: NewSession):
    """"Keep me signed in to a site": a browser that is the person's alone, on that site."""
    site = _site_of(body.site)
    return {"browser_session_id": await browser_client.tools.open_login_session([site])}


@router.patch("/api/site-sessions/{session_id}")
async def share_site_session(session_id: str):
    raise HTTPException(status_code=404, detail="Sharing a session is for workspaces with several people.")


@router.delete("/api/site-sessions/{session_id}")
async def remove_site_session(session_id: str):
    if not await site_sessions.forget(session_id):
        raise HTTPException(status_code=404, detail="That session is gone already.")
    return {"removed": True}


# ── a chat's picks (F1140) ─────────────────────────────────────────────────

class Picks(BaseModel):
    ids: list[str] = []


@router.get("/api/chat/{chat_id}/browser-sessions")
async def get_chat_picks(chat_id: str):
    from core.browser.picks import chat_picks
    return {"ids": await chat_picks(chat_id)}


@router.put("/api/chat/{chat_id}/browser-sessions")
async def put_chat_picks(chat_id: str, body: Picks):
    """The chat's sessions, applied when its browser next opens. Two for one site: 422 `{detail, site, ids}`."""
    from core.browser.picks import PickRefused, set_chat_picks
    try:
        return {"ids": await set_chat_picks(chat_id, body.ids)}
    except PickRefused as refused:
        return JSONResponse(status_code=422, content=refused.body())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


# ── for one release: BRW2's Website logins screen (F1079) ───────────────────

@router.get("/api/website-logins")
async def list_logins():
    return {"logins": [{"id": r["id"], "owner": r["owner"], "sites": [r["site"]], "created_at": r["created_at"],
                        "expires_at": r["expires_at"]} for r in await site_sessions.list_sessions() if not r["expired"]]}


@router.delete("/api/website-logins/{login_id}")
async def forget_login(login_id: str):
    if not await site_sessions.forget(login_id):
        raise HTTPException(status_code=404, detail="That login is gone already.")
    return {"forgotten": True}
