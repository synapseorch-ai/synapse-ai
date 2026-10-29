"""The person's view of a browser session (BRW1, D328).

`frames` relays the component's screencast to the pane, `shot` serves an action's
screenshot, and `stop` ends the agent's turn or run and closes its browser. Kept
copies of the screenshots arrive with v2's filmstrip. The component listens on 127.0.0.1
only; this is the one door to it from the app, behind the internal token like
every `/api/*` route. A session is reachable here only while its chat or run
holds it, and its id is never guessable.
"""
from __future__ import annotations

import json
import logging

import aiohttp
from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.browser import client as browser_client

logger = logging.getLogger(__name__)

router = APIRouter()


async def _owner(session_id: str) -> str:
    owner = await browser_client.tools.owner_of(session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="That browser is closed.")
    return owner


@router.get("/api/browser/{session_id}/frames")
async def frames(session_id: str, request: Request):
    owner = await _owner(session_id)
    host = browser_client.tools.host

    async def relay():
        timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
        pending = b""
        async with aiohttp.ClientSession(headers=host._headers(), timeout=timeout) as http:
            async with http.get(host.viewer_url("/frames"), params={"session_id": session_id}) as resp:
                if resp.status != 200:
                    return
                async for chunk in resp.content.iter_any():
                    if await request.is_disconnected():
                        break
                    yield chunk
                    pending = await _keep_log_lines(owner, pending + chunk)

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _keep_log_lines(owner: str, pending: bytes) -> bytes:
    """The person's and the guard's log lines, kept with the run or chat as they pass to the pane (F1125): you
    drive only while watching, so every line of yours goes by here. Returns what is left of an unfinished event."""
    *events, rest = pending.split(b"\n\n")
    lines = []
    for event in events:
        if event.startswith(b'data: {"log"'):
            try:
                lines.append(json.loads(event[6:])["log"])
            except (ValueError, KeyError):
                pass
    if lines:
        await browser_client.tools.keep_lines(owner, lines)
    return rest if len(rest) < 4_000_000 else b""


@router.get("/api/browser/{session_id}/shot/{ref}")
async def shot(session_id: str, ref: str):
    """The screenshot taken after one agent action, while its browser is open."""
    await _owner(session_id)
    host = browser_client.tools.host
    async with aiohttp.ClientSession(headers=host._headers(), timeout=aiohttp.ClientTimeout(total=30)) as http:
        async with http.get(f"http://127.0.0.1:{host.control}/shot",
                            params={"session_id": session_id, "ref": ref}) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=404, detail="That screenshot is gone.")
            body = await resp.read()
    return Response(content=body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


#: The person's actions on their view, relayed to the component (V2-CONTRACT.md).
_VIEWER_ACTIONS = {"input": "/input", "release": "/release", "allow-site": "/allow-site",
                   "take": "/wheel/take", "hand-back": "/wheel/hand-back", "dialog": "/dialog", "select": "/select"}


async def _relay(session_id: str, action: str, request: Request):
    owner = await _owner(session_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from core.browser.gate import CARD_WORDS, looks_like_card
    if action == "hand-back" and looks_like_card(str(body.get("note") or "")):
        raise HTTPException(status_code=422, detail=CARD_WORDS)  # the note reaches the agent (F1100)
    status, answer = await browser_client.tools.viewer(session_id, _VIEWER_ACTIONS[action], body)
    if status >= 400:
        raise HTTPException(status_code=status, detail=answer.get("error") or "Refused.")
    said = {"hand-back": ("I handed the browser back. " + str(body.get("note") or "")).strip(),
            "release": "It only loaded data: carry on.",
            "allow-site": f"You may type on {body.get('host') or 'that site'}. Carry on."}.get(action)
    if said:
        resumed, kept = await _resume(owner, said, str(body.get("note") or ""), request)
        if resumed:
            answer = {**answer, "resumed": resumed}
        if kept:
            answer = {**answer, "kept": kept}
    return answer


async def _resume(owner: str, said: str, note: str, request: Request) -> tuple[dict | None, dict | None]:
    """A chat or run parked on the person's turn carries on (F1099): a run resumes its step; a chat
    gets the person's word as its next message. A turn that was never parked (Take over mid-turn)
    needs nothing: the agent's call is waiting for the wheel."""
    parked = await browser_client.tools.take_parked(owner)
    if not parked:
        return None, None
    kept = None
    if parked.get("kind") in ("session_expired", "login_expired") and parked.get("site_session_id"):
        # The person signed in again in their turn: it is kept into the same session, with a fresh expiry.
        try:
            kept = await browser_client.tools.keep_session(parked["session_id"],
                                                           site_session_id=parked["site_session_id"],
                                                           site=parked.get("site") or "", owner=_person())
        except Exception:
            kept = None
    kind, _, ident = owner.partition(":")
    if kind == "run":
        from core.orchestration.answers import answer_paused_run
        await answer_paused_run(request.app.state, ident,
                                {"browser_turn": "answered", "note": note or said})
        return {"kind": "run", "run_id": ident}, kept
    if kind == "chat":
        from core.models import ChatRequest
        from core.routes.chat import _start
        agent_key = parked.get("agent_key") or "default"
        follow = ChatRequest(message=said, session_id=ident, agent_id=None if agent_key == "default" else agent_key)
        follow._app_chat = True
        # The kept turn it answers says so, for a reopened chat (F1183).
        from core.store import sessions as _sessions
        try:
            await _sessions.mark_browser_turn(ident, parked.get("session_id") or "", answered=True)
        except Exception as exc:  # the follow-up turn still goes on; only the reopened card's wording is at stake
            logger.warning("browser turn %s not marked handed back: %s", parked.get("session_id"), exc)
        _start(follow, expand_mentions=False)
        return {"kind": "chat", "session_id": ident}, kept
    return None, kept


@router.post("/api/browser/{session_id}/input")
async def person_input(session_id: str, request: Request):
    return await _relay(session_id, "input", request)


@router.post("/api/browser/{session_id}/release")
async def release(session_id: str, request: Request):
    return await _relay(session_id, "release", request)


@router.post("/api/browser/{session_id}/allow-site")
async def allow_site(session_id: str, request: Request):
    return await _relay(session_id, "allow-site", request)


@router.post("/api/browser/{session_id}/dialog")
async def answer_dialog(session_id: str, request: Request):
    return await _relay(session_id, "dialog", request)


@router.post("/api/browser/{session_id}/select")
async def choose(session_id: str, request: Request):
    return await _relay(session_id, "select", request)


@router.post("/api/browser/{session_id}/file")
async def give_file(session_id: str, file: UploadFile):
    """The person's own file, for the page's file picker while they drive (V2-CONTRACT.md)."""
    await _owner(session_id)
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files go up to 25 MB.")
    import base64
    status, answer = await browser_client.tools.viewer(session_id, "/file", {
        "name": file.filename or "file", "b64": base64.b64encode(data).decode()})
    if status >= 400:
        raise HTTPException(status_code=status, detail=answer.get("error") or "Refused.")
    return answer


@router.get("/api/browser/kept/{kind}/{ident}/log")
async def kept_log(kind: str, ident: str):
    """The record of who pressed and sent what in a run's or chat's browsers, kept with it (F1125)."""
    import asyncio
    from core.browser import kept
    entries = await asyncio.to_thread(kept.read_log, kind, ident)
    if entries is None:
        raise HTTPException(status_code=404, detail="No such run or chat.")
    return {"entries": entries}


@router.get("/api/browser/kept/{kind}/{ident}/{n}")
async def kept_shot(kind: str, ident: str, n: str):
    """A screenshot a run or chat kept after its browser closed, for the filmstrip (F1103)."""
    import asyncio
    from core.browser import kept
    data = await asyncio.to_thread(kept.read, kind, ident, n)
    if data is None:
        raise HTTPException(status_code=404, detail="That screenshot is gone.")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/api/browser/{session_id}/wheel/take")
async def take(session_id: str, request: Request):
    return await _relay(session_id, "take", request)


@router.post("/api/browser/{session_id}/wheel/hand-back")
async def hand_back(session_id: str, request: Request):
    return await _relay(session_id, "hand-back", request)


def _person() -> str:
    """Who keeps a session: the install's one sign-in, or "you" without one."""
    from core.routes.site_sessions import person
    return person()


class FinishSession(BaseModel):
    keep: bool = True


@router.post("/api/browser/{session_id}/finish-session")
async def finish_session(session_id: str, body: FinishSession):
    """Keep what the person signed in to in a "Keep me signed in" browser (F1138): the kept row, or 422 when nothing
    on the site was signed in, the browser staying open for another try."""
    await _owner(session_id)
    done = await browser_client.tools.finish_session(session_id, keep=body.keep, owner=_person())
    if done is None:
        raise HTTPException(status_code=409, detail="This browser is not for keeping a session.")
    site, row = done
    if body.keep and row is None:
        return JSONResponse(status_code=422, content={"detail": f"Nothing on {site} is signed in yet.", "site": site})
    return row or {}


# ── for one release: BRW2's Website logins screen (F1079) ───────────────────

class LoginSession(BaseModel):
    sites: list[str] = []


@router.post("/api/browser/login-sessions")
async def open_login_session(body: LoginSession):
    from core.routes.site_sessions import _site_of
    if not body.sites:
        raise HTTPException(status_code=422, detail="Name the site to log in to.")
    return {"session_id": await browser_client.tools.open_login_session([_site_of(body.sites[0].removeprefix("*."))])}


@router.post("/api/browser/{session_id}/finish-login")
async def finish_login(session_id: str, body: FinishSession):
    await _owner(session_id)
    done = await browser_client.tools.finish_session(session_id, keep=body.keep, owner=_person())
    if done is None:
        raise HTTPException(status_code=409, detail="This browser is not a login session.")
    return {"login_ref": (done[1] or {}).get("id")}


@router.post("/api/browser/{session_id}/stop")
async def stop(session_id: str):
    owner = await _owner(session_id)
    kind, _, ident = owner.partition(":")
    stopped = False
    if kind == "chat":
        from core import chat_turns
        stopped = bool(await chat_turns.stop(ident))
    elif kind == "run":
        from core.orchestration.cancel import cancel_run
        await cancel_run(ident, reason="The person stopped the browser.")
        stopped = True
    await browser_client.tools.close_owner(owner)
    return {"stopped": stopped, "closed": True}
