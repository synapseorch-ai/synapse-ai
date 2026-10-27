"""The person's view of a browser session (BRW1, D328).

`frames` relays the component's screencast to the pane, `shot` serves an action's
screenshot, and `stop` ends the agent's turn or run and closes its browser. Kept
copies of the screenshots arrive with v2's filmstrip. The component listens on 127.0.0.1
only; this is the one door to it from the app, behind the internal token like
every `/api/*` route. A session is reachable here only while its chat or run
holds it, and its id is never guessable.
"""
from __future__ import annotations

import aiohttp
from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel
from fastapi.responses import Response, StreamingResponse

from core.browser import client as browser_client

router = APIRouter()


def _owner(session_id: str) -> str:
    owner = browser_client.tools.owner_of(session_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="That browser is closed.")
    return owner


@router.get("/api/browser/{session_id}/frames")
async def frames(session_id: str, request: Request):
    _owner(session_id)
    host = browser_client.tools.host

    async def relay():
        timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
        async with aiohttp.ClientSession(headers=host._headers(), timeout=timeout) as http:
            async with http.get(host.viewer_url("/frames"), params={"session_id": session_id}) as resp:
                if resp.status != 200:
                    return
                async for chunk in resp.content.iter_any():
                    if await request.is_disconnected():
                        break
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/browser/{session_id}/shot/{ref}")
async def shot(session_id: str, ref: str):
    """The screenshot taken after one agent action, while its browser is open."""
    _owner(session_id)
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
    owner = _owner(session_id)
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
        resumed = await _resume(owner, said, str(body.get("note") or ""), request)
        if resumed:
            answer = {**answer, "resumed": resumed}
    return answer


async def _resume(owner: str, said: str, note: str, request: Request) -> dict | None:
    """A chat or run parked on the person's turn carries on (F1099): a run resumes its step; a chat
    gets the person's word as its next message. A turn that was never parked (Take over mid-turn)
    needs nothing: the agent's call is waiting for the wheel."""
    parked = browser_client.tools.take_parked(owner)
    if not parked:
        return None
    if parked.get("kind") == "login_expired" and parked.get("login_ref"):
        # The person logged in again in their turn: that login is kept under the same ref.
        try:
            await browser_client.tools.keep_login(parked["session_id"], login_ref=parked["login_ref"],
                                                  sites=parked.get("sites") or [], owner=_person())
        except Exception:
            pass
    kind, _, ident = owner.partition(":")
    if kind == "run":
        from core.orchestration.answers import answer_paused_run
        await answer_paused_run(request.app.state, ident,
                                {"browser_turn": "answered", "note": note or said})
        return {"kind": "run", "run_id": ident}
    if kind == "chat":
        from core.models import ChatRequest
        from core.routes.chat import _start
        agent_key = parked.get("agent_key") or "default"
        follow = ChatRequest(message=said, session_id=ident, agent_id=None if agent_key == "default" else agent_key)
        follow._app_chat = True
        _start(follow, expand_mentions=False)
        return {"kind": "chat", "session_id": ident}
    return None


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
    _owner(session_id)
    data = await file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Files go up to 25 MB.")
    import base64
    status, answer = await browser_client.tools.viewer(session_id, "/file", {
        "name": file.filename or "file", "b64": base64.b64encode(data).decode()})
    if status >= 400:
        raise HTTPException(status_code=status, detail=answer.get("error") or "Refused.")
    return answer


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
    """Who a kept login belongs to: the install's one sign-in, or "you" without one."""
    from core.config import load_settings
    return load_settings().get("login_username") or "you"


class LoginSession(BaseModel):
    sites: list[str] = []


@router.post("/api/browser/login-sessions")
async def open_login_session(body: LoginSession):
    """"Log in to a site" (Website logins): a browser that is the person's alone."""
    from core.models import AgentBrowser
    try:
        sites = AgentBrowser(sites=body.sites).sites
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {"session_id": await browser_client.tools.open_login_session(sites)}


class FinishLogin(BaseModel):
    keep: bool = True


@router.post("/api/browser/{session_id}/finish-login")
async def finish_login(session_id: str, body: FinishLogin):
    owner = _owner(session_id)
    if not owner.startswith("login:"):
        raise HTTPException(status_code=409, detail="This browser is not a login session.")
    ref = await browser_client.tools.finish_login(session_id, keep=body.keep, owner=_person())
    return {"login_ref": ref}


@router.post("/api/browser/{session_id}/stop")
async def stop(session_id: str):
    owner = _owner(session_id)
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
