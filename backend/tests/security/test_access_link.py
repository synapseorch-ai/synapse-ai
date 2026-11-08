"""Login off is not "open to the network": the access link (D124, after S9).

`synapse start` and the Docker entrypoint generate SYNAPSE_ACCESS_TOKEN and
hand out `http://host:3000/?token=…`. Presenting the token mints a session, and
the backend's session check (S9) applies whenever a token is configured — so an
install with login off is reachable by whoever holds the link, not by whoever
can reach the port. A password always wins: with login on, the link grants
nothing and its sessions stop working. Rotating the token signs everyone out.
"""
import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from core import internal_auth, user_auth
from core.internal_auth import InternalTokenMiddleware

TOKEN = "internal-t0ken"
ACCESS = "the-access-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", TOKEN)
    monkeypatch.setenv("SYNAPSE_JWT_SECRET", "jwt-secret-for-the-access-link-tests")
    monkeypatch.setenv("SYNAPSE_ACCESS_TOKEN", ACCESS)
    monkeypatch.setattr(internal_auth, "_login_required", lambda: False)


def _app():
    from core.routes.auth import router as auth_router

    app = FastAPI()
    for path in ("/api/settings", "/api/messaging/channels",
                 "/api/messaging/telegram/webhook/c1", "/api/messaging/whatsapp/webhook/c1"):
        app.add_api_route(path, lambda: JSONResponse({"ok": True}), methods=["GET", "POST"])
    app.include_router(auth_router)
    app.add_middleware(InternalTokenMiddleware)
    return app


async def _call(method, path, *, session=None, json=None):
    headers = {"X-Synapse-Internal": TOKEN}
    if session:
        headers["X-Synapse-Session"] = session
    transport = httpx.ASGITransport(app=_app(), client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        return await c.request(method, path, headers=headers, json=json)


async def _exchange(token=ACCESS):
    return await _call("POST", "/api/auth/access", json={"token": token})


async def test_login_off_with_a_token_is_not_open():
    assert (await _call("GET", "/api/settings")).status_code == 401


async def test_the_link_mints_a_session_that_opens_the_app():
    r = await _exchange()
    assert r.status_code == 200
    session = r.json()["token"]
    assert (await _call("GET", "/api/settings", session=session)).status_code == 200


async def test_a_wrong_token_mints_nothing():
    assert (await _exchange("not-the-token")).status_code == 401


async def test_rotating_the_token_signs_everyone_out(monkeypatch):
    session = (await _exchange()).json()["token"]
    monkeypatch.setenv("SYNAPSE_ACCESS_TOKEN", "a-brand-new-access-token-9876543210")
    assert (await _call("GET", "/api/settings", session=session)).status_code == 401


async def test_a_password_wins_over_the_link(monkeypatch):
    session = (await _exchange()).json()["token"]
    monkeypatch.setattr(internal_auth, "_login_required", lambda: True)
    assert (await _exchange()).status_code == 409
    assert (await _call("GET", "/api/settings", session=session)).status_code == 401
    password_session = user_auth.create_session_token("owner")
    assert (await _call("GET", "/api/settings", session=password_session)).status_code == 200


async def test_status_says_the_link_is_needed():
    status = (await _call("GET", "/api/auth/status")).json()
    assert status["access_required"] is True and status["authenticated"] is False
    session = (await _exchange()).json()["token"]
    assert (await _call("GET", "/api/auth/status", session=session)).json()["authenticated"] is True


@pytest.mark.parametrize("path", ["/api/messaging/telegram/webhook/c1", "/api/messaging/whatsapp/webhook/c1"])
async def test_platform_webhooks_need_no_session(path):
    """They come from Telegram or WhatsApp, not a browser, and verify themselves."""
    assert (await _call("POST", path)).status_code == 200


async def test_the_rest_of_messaging_still_needs_one():
    assert (await _call("GET", "/api/messaging/channels")).status_code == 401


async def test_no_token_configured_leaves_login_off_as_it_was(monkeypatch):
    monkeypatch.delenv("SYNAPSE_ACCESS_TOKEN")
    assert (await _call("GET", "/api/settings")).status_code == 200
    assert (await _exchange()).status_code == 404
