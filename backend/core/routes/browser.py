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
from fastapi import APIRouter, HTTPException, Request
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
                   "take": "/wheel/take", "hand-back": "/wheel/hand-back"}


async def _relay(session_id: str, action: str, request: Request):
    _owner(session_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status, answer = await browser_client.tools.viewer(session_id, _VIEWER_ACTIONS[action], body)
    if status >= 400:
        raise HTTPException(status_code=status, detail=answer.get("error") or "Refused.")
    return answer


@router.post("/api/browser/{session_id}/input")
async def person_input(session_id: str, request: Request):
    return await _relay(session_id, "input", request)


@router.post("/api/browser/{session_id}/release")
async def release(session_id: str, request: Request):
    return await _relay(session_id, "release", request)


@router.post("/api/browser/{session_id}/allow-site")
async def allow_site(session_id: str, request: Request):
    return await _relay(session_id, "allow-site", request)


@router.post("/api/browser/{session_id}/wheel/take")
async def take(session_id: str, request: Request):
    return await _relay(session_id, "take", request)


@router.post("/api/browser/{session_id}/wheel/hand-back")
async def hand_back(session_id: str, request: Request):
    return await _relay(session_id, "hand-back", request)


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
