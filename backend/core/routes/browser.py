"""The person's view of a browser session (BRW1, D328).

`frames` relays the component's screencast to the pane; `stop` ends the agent's
turn or run and closes its browser. The component itself listens on 127.0.0.1
only; this is the one door to it from the app, behind the internal token like
every `/api/*` route. A session is reachable here only while its chat or run
holds it, and its id is never guessable.
"""
from __future__ import annotations

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

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
