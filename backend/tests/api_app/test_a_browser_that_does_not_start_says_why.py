"""A browser that does not start is said in words, never a bare 500 (F1533, BRW5; eb 11-11).

On a fresh Windows install, "Keep me signed in to a site" and "Record a task" answered `500 Internal Server Error`
(text/plain), and both dialogs could only say "The browser could not be opened. Try again." The browser host had
stopped before it was ready; nothing caught that on the way to the route. Now it is a `BrowserUnavailable`, and the
app answers 503 with the host's own words in `detail`, which both dialogs show (`refusal()`, sessions.ts).
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from core.browser import client as browser_client
from core.browser import gate

DRIVER_GONE = "Exception: Connection.init: Connection closed while reading from the driver"
FAILING_HOST = f"import sys; sys.stderr.write({DRIVER_GONE!r} + '\\n'); sys.exit(1)"


@pytest.fixture
def no_host_starts(monkeypatch):
    """The real `BrowserTools` and `_Host`, with the host process one that stops before it is ready."""
    real = asyncio.create_subprocess_exec
    started: list[asyncio.subprocess.Process] = []

    async def fake(program, *args, **kwargs):
        proc = await real(sys.executable, "-c", FAILING_HOST, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    monkeypatch.setattr(browser_client, "tools", browser_client.BrowserTools(browser_client._Host()))

    async def any_site(host):
        return None

    monkeypatch.setattr(gate, "refused_site", any_site)
    yield started
    for proc in started:
        if proc.returncode is None:
            proc.kill()


async def test_keep_me_signed_in_says_why_the_browser_did_not_start(client, no_host_starts):
    resp = await client.post("/api/site-sessions/new", json={"site": "example.com"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert DRIVER_GONE in detail and "Is Chromium installed" not in detail
    assert no_host_starts, "the host was never started"


async def test_record_a_task_says_why_the_browser_did_not_start(client, no_host_starts):
    resp = await client.post("/api/recordings/new", json={})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert DRIVER_GONE in detail and "Is Chromium installed" not in detail
