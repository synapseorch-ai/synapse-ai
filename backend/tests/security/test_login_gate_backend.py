"""With login on, the backend itself asks for a session (S9).

The login gate used to live only in the Next proxy, and the proxy skipped it for
any request carrying a `synapse_auth_cache=no_auth_required` cookie — a value the
client sets. Past the gate the proxy attaches the internal token, and the
internal token was all the backend checked. So one forged cookie turned a
login-protected install into an open one: settings read, keys minted, login
switched off, from anywhere that could reach the UI port.

Now, when login is on, every internal route also needs a valid session — from
the `X-Synapse-Session` header the Next route handlers forward, or the
`synapse_session` cookie a proxied request carries. The paths the proxy's gate
exempts stay exempt: login itself, the widget (its own visitor token), and the
versioned API (its own key).
"""
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import internal_auth
from core.internal_auth import InternalTokenMiddleware

TOKEN = "t0ken"


def _app():
    async def ok(_request):
        return JSONResponse({"ok": True})

    paths = ["/api/settings", "/api/settings/api-keys", "/chat", "/api/auth/status",
             "/api/auth/login", "/api/widget/session", "/api/v1/agents", "/api/widgets"]
    inner = Starlette(routes=[Route(p, ok, methods=["GET", "POST"]) for p in paths])
    inner.add_middleware(InternalTokenMiddleware)
    return inner


async def _get(path, *, headers=None, cookies=None):
    transport = httpx.ASGITransport(app=_app(), client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                 cookies=cookies) as client:
        return await client.get(path, headers={"X-Synapse-Internal": TOKEN, **(headers or {})})


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", TOKEN)


@pytest.fixture
def login_on(monkeypatch):
    monkeypatch.setattr(internal_auth, "_login_required", lambda: True)
    monkeypatch.setattr(internal_auth, "_session_user",
                        lambda token: "owner" if token == "good-session" else None)


@pytest.fixture
def login_off(monkeypatch):
    monkeypatch.setattr(internal_auth, "_login_required", lambda: False)


@pytest.mark.parametrize("path", ["/api/settings", "/api/settings/api-keys", "/chat", "/api/widgets"])
async def test_login_on_the_internal_token_alone_is_not_enough(login_on, path):
    r = await _get(path)
    assert r.status_code == 401, path


async def test_a_forged_gate_cookie_opens_nothing(login_on):
    r = await _get("/api/settings", cookies={"synapse_auth_cache": "no_auth_required"})
    assert r.status_code == 401


@pytest.mark.parametrize("session", ["", "bad-session", "eyJhbGciOiJub25lIn0.e30."])
async def test_an_invalid_session_is_refused(login_on, session):
    assert (await _get("/api/settings", headers={"X-Synapse-Session": session})).status_code == 401


async def test_a_session_by_header_or_cookie_is_let_in(login_on):
    assert (await _get("/api/settings", headers={"X-Synapse-Session": "good-session"})).status_code == 200
    assert (await _get("/api/settings", cookies={"synapse_session": "good-session"})).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/auth/status", "/api/auth/login", "/api/widget/session", "/api/v1/agents"]
)
async def test_what_the_gate_exempts_stays_exempt(login_on, path):
    assert (await _get(path)).status_code == 200, path


async def test_login_off_changes_nothing(login_off):
    assert (await _get("/api/settings")).status_code == 200


async def test_the_token_is_still_checked_first(login_on):
    transport = httpx.ASGITransport(app=_app(), client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/settings", headers={"X-Synapse-Session": "good-session"})
    assert r.status_code == 403
