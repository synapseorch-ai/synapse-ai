"""A phone number's webhooks reach the backend without the internal token (VOX1a), and nothing else does.

Twilio cannot pass through the Next proxy for a call (its `/api` is a route handler, which cannot upgrade a
WebSocket), so the voice routes are reached directly and admit by their own credentials: the route token, the
number's SharedKey signature, the one-time socket token. The exemption must be exactly those five webhook
paths: the voice channel's own app routes (approvals, numbers, calls) and everything else keep the token.
"""
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.internal_auth import InternalTokenMiddleware

OPEN = [f"/api/messaging/twilio_voice/webhook/rt0ken/{leaf}" for leaf in ("incoming", "end", "dialed", "recorded",
                                                                           "status")]
CLOSED = [
    "/api/messaging/twilio_voice/approvals/voice_chn_1_CA1",
    "/api/messaging/twilio_voice/numbers",
    "/api/messaging/twilio_voice/channels/chn_1/calls",
    "/api/messaging/twilio_voice/webhook/rt0ken/incoming/extra",
    "/api/messaging/twilio_voice/webhook/rt0ken/settings",
    "/api/messaging/telegram/webhook/rt0ken/incoming",
    "/api/messaging/channels",
    "/api/settings",
]


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", "t0ken")


async def _post(path):
    async def ok(_request):
        return JSONResponse({"ok": True})
    app = Starlette(routes=[Route(p, ok, methods=["POST"]) for p in OPEN + CLOSED])
    app.add_middleware(InternalTokenMiddleware)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(path)


@pytest.mark.parametrize("path", OPEN)
async def test_a_numbers_webhook_needs_no_internal_token(path):
    assert (await _post(path)).status_code == 200


@pytest.mark.parametrize("path", CLOSED)
async def test_everything_else_still_does(path):
    assert (await _post(path)).status_code == 403
