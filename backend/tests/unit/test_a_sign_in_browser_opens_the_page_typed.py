"""A sign-in browser opens on the page the person typed, on the host they named, over https (F1514, eb's QA B5;
Naveen's pick 11-10).

"Keep me signed in" to the-internet.herokuapp.com/login opened the site's home: the dialog sent only the host, and
the route opened `https://{host}` (F1425 held it there: "nothing else of what they typed reaches its address"). A path
adds no reach (the person can type any address in that browser, and Record a task's Start at keeps its path), so the
dialog now sends `start`, the address as typed, beside `site`. The browser opens `start` only on the very host of
`site`: over https, with its path and query, never a login (`user:pass@`), a port, another host or another scheme.
Anything else is the host's front page, as before. The site kept is still the registrable domain.
"""
from __future__ import annotations

import pytest
from core.browser import client as browser_client
from test_browser_component import _FakeHost


@pytest.fixture
def fake(monkeypatch):
    tools = browser_client.BrowserTools(_FakeHost())
    monkeypatch.setattr(browser_client, "tools", tools)
    return tools


def _went(fake) -> list[str]:
    return [c[2]["url"] for c in fake.host.calls if c[:2] == ("VIEW", "/input") and c[2].get("type") == "nav"]


async def _open(client, fake, site: str, start: str | None = None) -> str:
    body = {"site": site, **({"start": start} if start is not None else {})}
    resp = await client.post("/api/site-sessions/new", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["browser_session_id"]


async def test_it_opens_on_the_page_typed(client, fake):
    sid = await _open(client, fake, "portal.example.com", "https://Portal.Example.com/login?next=/a")
    assert _went(fake)[-1] == "https://portal.example.com/login?next=/a"
    assert (await fake.store.get_owner(await fake.owner_of(sid)))["sessions"] == ["example.com"]


@pytest.mark.parametrize("start, url", [
    ("http://portal.example.com/login", "https://portal.example.com/login"),       # over https
    ("portal.example.com/login", "https://portal.example.com/login"),              # typed without a scheme
    ("https://portal.example.com/login#top", "https://portal.example.com/login"),  # no fragment
    ("https://portal.example.com/", "https://portal.example.com/"),
])
async def test_the_page_is_its_own_over_https(client, fake, start, url):
    await _open(client, fake, "portal.example.com", start)
    assert _went(fake)[-1] == url


@pytest.mark.parametrize("start", [
    "https://evil.example.net/login",                 # another host
    "https://www.example.com/login",                  # another host on the same site
    "https://user:pw@portal.example.com/login",       # a login in the address
    "https://portal.example.com:8443/login",          # a port
    "javascript:alert(1)",
    "ftp://portal.example.com/login",
    "",
])
async def test_anything_else_is_the_hosts_front_page(client, fake, start):
    await _open(client, fake, "portal.example.com", start)
    assert _went(fake)[-1] == "https://portal.example.com"


async def test_without_a_start_it_is_the_hosts_front_page_as_before(client, fake):
    await _open(client, fake, "x.com")
    assert _went(fake) == ["https://x.com"]


#: The same host written another way (eb 11-10): it counts as that host every time, or the front page opens; another
#: host never does. Case is the same host; a trailing dot, or an IDN in its other form, is the front page.
@pytest.mark.parametrize("site, start, url", [
    ("portal.example.com", "https://PORTAL.Example.COM/login", "https://portal.example.com/login"),
    ("portal.example.com", "https://portal.example.com./login", "https://portal.example.com"),
    ("bücher.example", "https://bücher.example/login", "https://bücher.example/login"),
    ("bücher.example", "https://BÜCHER.example/login", "https://bücher.example/login"),
    ("bücher.example", "https://xn--bcher-kva.example/login", "https://bücher.example"),
    ("xn--bcher-kva.example", "https://bücher.example/login", "https://xn--bcher-kva.example"),
    ("portal.example.com", "https://portal.example.com\\@evil.example.net/login", "https://portal.example.com"),
])
async def test_the_same_host_written_another_way_is_that_host_or_the_front_page(client, fake, site, start, url):
    from urllib.parse import urlparse

    await _open(client, fake, site, start)
    assert _went(fake)[-1] == url
    assert urlparse(_went(fake)[-1]).hostname == site, "never another host"
