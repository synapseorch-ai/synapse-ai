"""A page behind a sign-in is named as one, never read as the page asked for (F1632).

Asked for a private page, a site answers 401 or sends the reader to its sign-in page. The scraper handed that sign-in
page back as the page's content, so a run summarised a login form as someone's settings. A sign-in wall is now a
failure that says so. The near misses stay content: a sign-in page asked for by its own address, a page whose name
merely contains "login", and an ordinary redirect to another page.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core import egress_guard, web_refusals
from tools import web_scraper


@pytest.mark.parametrize("asked,landed,status", [
    ("https://github.com/settings/profile", "https://github.com/login?return_to=%2Fsettings%2Fprofile", 200),
    ("https://a.test/account", "https://a.test/users/sign_in", 200),
    ("https://a.test/report", "https://auth.a.test/signin?continue=https%3A%2F%2Fa.test%2Freport", 200),
    ("https://a.test/inbox", "https://a.test/session/new?next=/inbox", 200),
    ("https://a.test/x", "https://a.test/x", 401),
])
def test_a_sign_in_wall_is_named(asked, landed, status):
    said = web_refusals.sign_in_wall(asked, landed, status)
    assert said and said.startswith("Needs a sign-in:"), said


@pytest.mark.parametrize("asked,landed,status", [
    ("https://a.test/login", "https://a.test/login", 200),  # the sign-in page asked for by its own address
    ("https://a.test/login", "https://a.test/login?next=%2F", 200),
    ("https://a.test/blog/how-to-login-faster", "https://a.test/blog/how-to-login-faster", 200),
    ("https://a.test/old", "https://a.test/new", 200),  # an ordinary move
    ("https://a.test/sessions-guide", "https://a.test/sessions-guide", 200),
    ("https://a.test/x", "https://a.test/x", 403),  # a refusal, but no sign-in: the bot-check words say it
])
def test_a_page_that_is_no_sign_in_wall_is_left_alone(asked, landed, status):
    assert web_refusals.sign_in_wall(asked, landed, status) is None


# ── The scraper, for real ────────────────────────────────────────────────────────────────────────────────────────

def _chromium() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


@pytest.fixture
async def site(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    egress_guard._resolve_cache.clear()
    monkeypatch.setattr(web_scraper, "_crawler", None)
    login = b'<html><body><h1>Sign in to Harbor & Pine</h1><form><input type="password"></form></body></html>'
    pages = {"/account": (302, b"", "/login?return_to=%2Faccount"),
             "/login": (200, login, None),
             "/login?return_to=%2Faccount": (200, login, None)}

    async def serve(reader, writer):
        try:
            path = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split(" ", 2)[1]
            status, body, location = pages[path]
            moved = f"Location: {location}\r\n" if location else ""
            writer.write(f"HTTP/1.1 {status} X\r\nContent-Type: text/html\r\n{moved}Connection: close\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, KeyError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    yield f"http://localhost:{server.sockets[0].getsockname()[1]}"
    server.close()
    await web_scraper.close_crawler()


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_the_scraper_refuses_a_page_that_lands_on_a_sign_in(site):
    out = json.loads((await web_scraper._handle_scrape_url({"url": site + "/account"}))[0].text)
    assert out["success"] is False and "Needs a sign-in:" in out["error"], out
    assert "Harbor & Pine" not in json.dumps(out)  # the login form is never handed back as the page


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_the_scraper_reads_a_sign_in_page_asked_for_by_its_own_address(site):
    out = json.loads((await web_scraper._handle_scrape_url({"url": site + "/login"}))[0].text)
    assert out["success"] is True and "Sign in to Harbor" in out.get("markdown", ""), out
