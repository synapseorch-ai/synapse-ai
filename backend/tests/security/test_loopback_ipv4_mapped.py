"""A token-less backend admits a local browser however its address is spelled
(F12, D104).

The launchers run uvicorn with its default `proxy_headers`, which trusts
127.0.0.1 — the Next proxy — and takes the client address from the
`X-Forwarded-For` it adds. A Windows browser reaching WSL2's Next arrives there
as `::ffff:127.0.0.1`, which `_is_loopback` compared as a string against
`{127.0.0.1, ::1, localhost}` and refused: every `/api/*` call 403'd. The check
parses the address instead. It stays exactly as strict about anything that is
not loopback, IPv4-mapped or not.
"""
import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from core.internal_auth import InternalTokenMiddleware


def _app():
    async def ok(_request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/api/settings", ok)])
    inner.add_middleware(InternalTokenMiddleware)
    # What `uvicorn.run(...)` wraps every app in by default.
    return ProxyHeadersMiddleware(inner, trusted_hosts="127.0.0.1")


async def _get(forwarded_for):
    transport = httpx.ASGITransport(app=_app(), client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/api/settings", headers={"X-Forwarded-For": forwarded_for})


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("SYNAPSE_INTERNAL_TOKEN", raising=False)


@pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "127.0.0.1", "::1"])
async def test_a_local_browser_is_admitted(address):
    assert (await _get(address)).status_code == 200


@pytest.mark.parametrize("address", ["::ffff:203.0.113.9", "203.0.113.9", "10.0.0.5", "not-an-address"])
async def test_anything_else_is_still_refused(address):
    assert (await _get(address)).status_code == 403
