"""A site the browser's guard will never open is refused before any browser starts (F1517, eb's QA B15's door check;
Naveen's pick 11-10).

"Keep me signed in" to 127.0.0.1.nip.io opened a browser (on cloud a private VM and its credits) whose only page was
the guard's refusal, and Keep was offered there (F1501 made Keep wait). The browser's own guard now answers at the
door: `gate.refused_site`, the same `check_host` and the same words as F1505's, before anything opens. Only a host
it will never open is refused: a private or blocked address. A name that can't be found now, or a fault in the
guard ("could not be checked"), opens the browser as before, where the page says what happened.
"""
from __future__ import annotations

import pytest
from core import egress_guard
from core.browser import client as browser_client
from core.browser import gate
from test_browser_component import _FakeHost


@pytest.fixture
def fake(monkeypatch):
    tools = browser_client.BrowserTools(_FakeHost())
    monkeypatch.setattr(browser_client, "tools", tools)
    return tools


def _guard(monkeypatch, raised: Exception | None):
    async def check_host(_host):
        if raised is not None:
            raise raised

    monkeypatch.setattr(gate, "check_host", check_host)


def _opened(fake) -> bool:
    return any(c[1] == "/session" for c in fake.host.calls)


async def test_a_private_address_is_refused_before_any_browser_starts(client, fake, monkeypatch):
    _guard(monkeypatch, egress_guard.BlockedAddress("intranet.example resolves to a blocked range: private",
                                                    "intranet.example"))
    resp = await client.post("/api/site-sessions/new", json={"site": "intranet.example"})
    assert resp.status_code == 422, resp.text
    assert "intranet.example is not a public address" in resp.json()["detail"], resp.text
    assert not _opened(fake), "no browser starts for a site it can never open"
    old = await client.post("/api/browser/login-sessions", json={"sites": ["intranet.example"]})
    assert old.status_code == 422 and not _opened(fake), "BRW2's old route the same"


@pytest.mark.parametrize("raised", [
    egress_guard.UnresolvedHost("could not resolve nosuch.example"),   # could not be found: maybe later
    RuntimeError("the resolver broke"),                                 # could not be checked: as at open today
    None,                                                               # a public site
])
async def test_anything_else_opens_the_browser_as_before(client, fake, monkeypatch, raised):
    _guard(monkeypatch, raised)
    resp = await client.post("/api/site-sessions/new", json={"site": "nosuch.example"})
    assert resp.status_code == 200, resp.text
    assert _opened(fake)


async def test_the_door_says_the_guards_own_words(monkeypatch):
    _guard(monkeypatch, egress_guard.BlockedAddress("x", "10.0.0.5"))
    assert await gate.refused_site("10.0.0.5") == "10.0.0.5 is not a public address"
    _guard(monkeypatch, egress_guard.UnresolvedHost("x"))
    assert await gate.refused_site("nosuch.example") is None
    _guard(monkeypatch, RuntimeError("x"))
    assert await gate.refused_site("nosuch.example") is None
