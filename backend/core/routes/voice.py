"""The voice channel's routes (VOX1a): Twilio's webhooks and the call's WebSocket, plus the app's own.

Twilio's, under ``/api/messaging/twilio_voice/webhook/{route_token}/…``. The path is the messaging webhooks'
shape on purpose: the login gate and the frontend proxy already let it through (`internal_auth._WEBHOOK`,
`proxy.ts`), and the route token is what names and admits the channel (V6). They answer TwiML.

* ``POST incoming``, ``POST end``, ``POST dialed``, ``POST recorded``, ``POST status``
* ``WS ws?t=`` — a WebSocket is not HTTP, so no middleware sees it: the one-time token is its whole admission,
  and it binds its own settings.

The app's (behind its login, like any route): the bell's answer to a call's parked approval, the numbers a key
can see (the connect flow's step two), and a number's recent calls.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response
from pydantic import BaseModel

from core.messaging.voice import PLATFORM, service
from core.messaging.voice.relay import TextRelay

log = logging.getLogger("voice.routes")

router = APIRouter()
WEBHOOK = "/api/messaging/" + PLATFORM + "/webhook/{route_token}"
#: Per address, as the messaging webhooks' (V28): a minute's window.
PER_IP = ("voice", 600, 60)


def _xml(markup: str) -> Response:
    return Response(content=markup, media_type="application/xml")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else ""


async def _admit(request: Request, route_token: str) -> tuple[dict, dict]:
    """The channel and the form, or a refusal (logged without the body)."""
    from core.chat_widgets import limits

    if not await limits.check_ip(getattr(request.app.state, "redis", None), _client_ip(request), PER_IP):
        log.warning("voice webhook rate-limited")
        raise HTTPException(status_code=429, detail="Too many requests")
    channel = await service.channel_for_route(route_token)
    if channel is None:
        log.warning("voice webhook for no channel")
        raise HTTPException(status_code=404, detail="Not found")
    form = {k: str(v) for k, v in (await request.form()).items()}
    signing = (channel.get("voice") or {}).get("signing")
    secret = (channel.get("credentials") or {}).get("signing_secret")
    if signing and secret:
        # Signed for the URL Twilio was given: this server's public base and the path it asked for.
        from core.messaging.voice import hooks
        from core.messaging.voice.connect import provider_for
        url = hooks.current().public_base() + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not provider_for({}).verify_webhook({**signing, "secret": secret}, url=url, params=form,
                                                headers=request.headers):
            log.warning("voice webhook with a bad or missing signature")
            raise HTTPException(status_code=403, detail="Bad signature")
    return channel, form


@router.post(WEBHOOK + "/incoming", include_in_schema=False)
async def voice_incoming(route_token: str, request: Request):
    channel, form = await _admit(request, route_token)
    return _xml(await service.incoming(channel, form))


@router.post(WEBHOOK + "/end", include_in_schema=False)
async def voice_end(route_token: str, request: Request):
    channel, form = await _admit(request, route_token)
    return _xml(await service.end(channel, form))


@router.post(WEBHOOK + "/dialed", include_in_schema=False)
async def voice_dialed(route_token: str, request: Request):
    channel, form = await _admit(request, route_token)
    return _xml(await service.dialed(channel, form))


@router.post(WEBHOOK + "/recorded", include_in_schema=False)
async def voice_recorded(route_token: str, request: Request):
    channel, form = await _admit(request, route_token)
    return _xml(await service.recorded(channel, form))


@router.post(WEBHOOK + "/status", include_in_schema=False)
async def voice_status(route_token: str, request: Request):
    channel, form = await _admit(request, route_token)
    await service.status(channel, form)
    return Response(status_code=204)


@router.websocket(WEBHOOK + "/ws")
async def voice_socket(websocket: WebSocket, route_token: str):
    from core import settings_runtime

    token = websocket.query_params.get("t", "")
    channel = await service.channel_for_route(route_token)
    if channel is None:
        log.warning("voice socket for no channel")
        await websocket.close(code=1008)
        return
    settings_token = await settings_runtime.bind()
    try:
        transport = TextRelay(websocket.receive_text, websocket.send_text)
        try:
            call = await service.socket_call(channel, token, transport)
        except service.SocketRefused as exc:
            log.warning("voice socket refused: %s", exc)
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await call.run()
    finally:
        settings_runtime.reset(settings_token)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — already closed by either side
            pass


# ── The app's own ───────────────────────────────────────────────────────────


class ApprovalAnswer(BaseModel):
    approve: bool
    reason: str = ""
    approval_id: str | None = None


@router.post("/api/messaging/" + PLATFORM + "/approvals/{session_id}", include_in_schema=False)
async def answer_call_approval(session_id: str, body: ApprovalAnswer):
    """The bell's Approve / Refuse for a tool call a phone call parked on (V16); the turn carries on behind."""
    from core import api_approvals, chat_turns
    from core.routes.chat import TURN_RUNNING

    if not session_id.startswith("voice_"):
        raise HTTPException(status_code=404, detail="Not a call's question")
    try:
        await api_approvals.answer(session_id, approve=body.approve, reason=body.reason,
                                   approval_id=body.approval_id)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return {"status": "allowed" if body.approve else "refused", "session_id": session_id}


class NumbersRequest(BaseModel):
    account_sid: str
    api_key_sid: str
    api_key_secret: str = ""
    channel_id: str | None = None  # an existing number's: a blank secret keeps the stored one


@router.post("/api/messaging/" + PLATFORM + "/numbers", include_in_schema=False)
async def list_numbers(body: NumbersRequest):
    """The numbers a Twilio key can see, and where each points now (V30's step two). Checks the key."""
    from core.messaging import store as channel_store
    from core.messaging.voice.connect import provider_for
    from core.messaging.voice.provider import CallRefused

    secret = body.api_key_secret
    if not secret and body.channel_id:
        stored = await channel_store.get_channel(body.channel_id) or {}
        secret = (stored.get("credentials") or {}).get("api_key_secret") or ""
    provider = provider_for({"account_sid": body.account_sid, "api_key_sid": body.api_key_sid,
                             "api_key_secret": secret})
    try:
        numbers = await provider.list_numbers()
    except CallRefused as exc:
        raise HTTPException(status_code=400, detail=exc.reason)
    return {"numbers": [{"sid": n.sid, "number": n.number, "voice_url": n.voice_url,
                         "voice_application_sid": n.voice_application_sid} for n in numbers]}


@router.get("/api/messaging/" + PLATFORM + "/channels/{channel_id}/calls", include_in_schema=False)
async def recent_calls(channel_id: str, limit: int = 50):
    """A number's recent calls, newest first (V31)."""
    from core.messaging.voice import hooks
    return {"calls": await hooks.current().list_calls(channel_id, limit=max(1, min(limit, 200)))}
