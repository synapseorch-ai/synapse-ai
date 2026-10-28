"""The scraper's browser state belongs to whoever called it (F1067, F1110).

One scraper process serves every chat and run of a backend, and on a worker every tenant of its task. Its one
browser kept one cookie jar for every call with the same settings, and a named session (`scrape_with_session`) was
a name any caller could reuse, logged-in state and all. Its page cache was keyed by URL alone, so a page fetched
with one caller's headers could be read back by another.

Now the engine says who a call is for (MCP request `_meta`, `core.tool_scope`, sent to Synapse's own servers only),
and the scraper gives each tenant + chat or run its own browser context; a call that does not say gets one of its
own, closed when it ends. A call with no named session leaves nothing behind in the browser. The cache is used only
by a call that sends no headers and runs no script.

Its tags say what it does, too: `scrape_with_session` logs in and fills forms and `screenshot_url` writes a file, so
neither is "read-only" or "safe to repeat", and a Tool step does not repeat one that may have gone through.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from core import egress_guard, tool_scope
from tools import web_scraper

PUBLIC = "https://93.184.216.34/"


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch):
    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


# ── The engine says who a call is for ────────────────────────────────────────────────────────────────────────────

def test_a_native_servers_call_carries_the_tenant_and_the_owner():
    meta = tool_scope.call_meta("web_scraper", "run-1")
    assert meta == {tool_scope.SCOPE_KEY: {"tenant": "default", "owner": "run-1"}}


@pytest.mark.parametrize("server", ["ext_mcp_somebody", "Google Workspace", "Browser Automation", "bash", "nope"])
def test_no_one_elses_server_is_told(server):
    assert tool_scope.call_meta(server, "run-1") is None


def test_only_servers_that_are_ours_are_told():
    from core.tools_registry import ALL_NATIVE_TOOLS

    assert tool_scope.SCOPED_SERVERS <= set(ALL_NATIVE_TOOLS)


def test_a_call_to_any_other_server_is_made_as_before():
    assert tool_scope.call_kwargs("ext_mcp_somebody", "run-1") == {}
    assert tool_scope.call_kwargs("web_scraper", None) == {}
    assert tool_scope.call_kwargs("web_scraper", "run-1") == {"meta": tool_scope.call_meta("web_scraper", "run-1")}


def test_a_call_with_no_owner_says_nothing():
    assert tool_scope.call_meta("web_scraper", None) is None
    assert tool_scope.call_meta("web_scraper", "") is None


def test_the_agent_loop_and_a_tool_step_send_it():
    import inspect

    from core import react_engine
    from core.orchestration import steps

    loop = inspect.getsource(react_engine)
    assert "session.call_tool(actual_tool_name, tool_args, read_timeout_seconds=_timeout,\n" in loop
    # Through `call_kwargs_for` since F1118: it returns `call_kwargs`'s own answer for every
    # server but the SQL tool's (tests/security/test_sql_handover.py).
    assert "_call_kwargs = await call_kwargs_for(agent_name, run_id or session_id, tool_args)" in loop
    assert "**_call_kwargs)" in loop
    step = inspect.getsource(steps)
    assert "**(await call_kwargs_for(agent_name, run_id, tool_args))," in step


# ── The scraper keys what it keeps by it ─────────────────────────────────────────────────────────────────────────

class Recorder:
    """A crawler that records the config of every run and every session it was asked to end."""

    def __init__(self):
        self.configs = []
        self.killed = []
        self.crawler_strategy = SimpleNamespace(kill_session=self._kill)

    async def _kill(self, session_id):
        self.killed.append(session_id)

    async def arun(self, url, config):
        self.configs.append(config)
        return SimpleNamespace(success=True, markdown="words " * 20, links={}, media={}, error_message=None,
                               screenshot=None, extracted_content=None)


@pytest.fixture
def recorder(monkeypatch):
    crawler = Recorder()

    async def get_crawler():
        return crawler

    monkeypatch.setattr(web_scraper, "get_crawler", get_crawler)
    monkeypatch.setattr(web_scraper, "_browser_proxy", "http://127.0.0.1:1")
    return crawler


async def _through_mcp(name, args, meta=None):
    """The real MCP request path: the scraper's server, a client, and `_meta` on the call."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(web_scraper.app) as client:
        result = await client.call_tool(name, args, meta=meta)
    return json.loads(result.content[0].text)


def _label(config):
    return config.proxy_config.username


async def test_two_owners_get_two_contexts_and_one_owner_keeps_one(recorder):
    a = tool_scope.call_meta("web_scraper", "chat-a")
    b = tool_scope.call_meta("web_scraper", "chat-b")
    await _through_mcp("scrape_url", {"url": PUBLIC}, a)
    await _through_mcp("scrape_url", {"url": PUBLIC}, a)
    await _through_mcp("scrape_url", {"url": PUBLIC}, b)
    first, again, other = (_label(c) for c in recorder.configs)
    assert first == again
    assert first != other


async def test_a_named_session_is_the_owners_and_lasts_beyond_the_call(recorder):
    a = tool_scope.call_meta("web_scraper", "chat-a")
    b = tool_scope.call_meta("web_scraper", "chat-b")
    steps = {"session_id": "linkedin", "steps": [{"url": PUBLIC}]}
    await _through_mcp("scrape_with_session", steps, a)
    await _through_mcp("scrape_with_session", steps, b)
    mine, theirs = (c.session_id for c in recorder.configs)
    assert mine != theirs and mine.endswith(":linkedin") and theirs.endswith(":linkedin")
    assert mine not in recorder.killed and theirs not in recorder.killed


async def test_a_call_with_no_named_session_leaves_nothing_behind(recorder):
    await _through_mcp("scrape_url", {"url": PUBLIC}, tool_scope.call_meta("web_scraper", "chat-a"))
    (config,) = recorder.configs
    assert config.session_id and recorder.killed == [config.session_id]


async def test_a_caller_that_says_nothing_shares_with_no_one(recorder):
    steps = {"session_id": "linkedin", "steps": [{"url": PUBLIC}, {"url": PUBLIC}]}
    await _through_mcp("scrape_with_session", steps)
    await _through_mcp("scrape_with_session", steps)
    one, same_call, next_call, next_same = recorder.configs
    # One call's steps share their session; the next call does not get it, and nothing outlives a call.
    assert one.session_id == same_call.session_id and next_call.session_id == next_same.session_id
    assert next_call.session_id != one.session_id and _label(next_call) != _label(one)
    assert set(recorder.killed) == {one.session_id, next_call.session_id}


async def test_a_meta_from_a_caller_with_no_tenant_or_owner_is_no_scope(recorder):
    await _through_mcp("scrape_url", {"url": PUBLIC}, {tool_scope.SCOPE_KEY: {"tenant": "", "owner": ""}})
    await _through_mcp("scrape_url", {"url": PUBLIC}, {tool_scope.SCOPE_KEY: {"tenant": "", "owner": ""}})
    assert _label(recorder.configs[0]) != _label(recorder.configs[1])


# ── The cache (F1110) ────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args,cached", [
    ({"cache": True}, True),
    ({"cache": True, "js_code": "document.querySelector('a').click()"}, False),
    ({}, False),
])
async def test_the_cache_is_used_only_by_a_plain_read(recorder, args, cached):
    CacheMode = web_scraper.CacheMode

    await web_scraper._handle_scrape_url({"url": PUBLIC, **args})
    assert (recorder.configs[0].cache_mode == CacheMode.ENABLED) is cached


@pytest.mark.parametrize("headers,js_code,cached", [
    (None, None, True), ({"Authorization": "Bearer x"}, None, False), (None, "1", False), ({}, None, True),
])
def test_headers_or_a_script_keep_a_read_out_of_the_cache(headers, js_code, cached):
    CacheMode = web_scraper.CacheMode

    assert (web_scraper._cache_mode(True, headers, js_code) == CacheMode.ENABLED) is cached
    assert web_scraper._cache_mode(False, headers, js_code) == CacheMode.BYPASS


@pytest.mark.parametrize("wait_for", [
    ".results", "css:.results", "js:document.title.length > 0", "() => true", "function () { return true }",
])
async def test_a_read_that_waits_stays_out_of_the_cache_too(recorder, wait_for):
    """A wait may run a script, and which forms do is the wait's business: a read that waits is never a plain read,
    so no form, today's or a later one, reaches the cache every caller shares."""
    await web_scraper._handle_scrape_url({"url": PUBLIC, "cache": True, "wait_for": wait_for})
    assert recorder.configs[0].cache_mode == web_scraper.CacheMode.BYPASS


# ── Honest tags ──────────────────────────────────────────────────────────────────────────────────────────────────

async def _listed():
    return {t.name: t for t in await web_scraper.list_tools()}


@pytest.mark.parametrize("name", ["scrape_with_session", "screenshot_url"])
async def test_the_tools_that_change_things_say_so(name):
    from core.tools import tool_hints

    hints = tool_hints((await _listed())[name])
    assert hints.declared and not hints.read_only and not hints.idempotent and not hints.destructive
    assert hints.open_world
    # What a Tool step reads before it repeats a call that may have gone through (TAG1, F737).
    assert not (not hints.declared or hints.read_only or hints.idempotent)


@pytest.mark.parametrize("name", ["scrape_url", "scrape_structured", "crawl_multiple", "extract_links",
                                  "search_page"])
async def test_the_reading_tools_keep_their_tags(name):
    annotations = (await _listed())[name].annotations
    assert annotations.readOnlyHint is True and annotations.idempotentHint is True
    assert annotations.openWorldHint is True


# ── For real: one browser, two owners, a site that sets a cookie ─────────────────────────────────────────────────

def _chromium() -> bool:
    import os
    from pathlib import Path

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return root.is_dir() and any(p.name.startswith("chromium") for p in root.iterdir())


@pytest.fixture
async def real_scraper(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    monkeypatch.setattr(web_scraper, "_crawler", None)
    yield web_scraper
    await web_scraper.close_crawler()


@pytest.mark.skipif(not _chromium(), reason="no Playwright Chromium on this machine")
async def test_one_owners_cookies_never_reach_another_owner(real_scraper):
    async def serve(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            path = head.split(" ", 2)[1]
            cookie = next((line.split(":", 1)[1].strip() for line in head.split("\r\n")
                           if line.lower().startswith("cookie:")), "none")
            extra = "Set-Cookie: who=owner-a; Path=/\r\n" if path == "/login" else ""
            body = f"<html><body><p>cookie is [{cookie}] {'words ' * 20}</p></body></html>".encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n{extra}Connection: close\r\n"
                         f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    site = await asyncio.start_server(serve, "127.0.0.1", 0)
    base = f"http://localhost:{site.sockets[0].getsockname()[1]}"
    a = tool_scope.call_meta("web_scraper", "chat-a")
    b = tool_scope.call_meta("web_scraper", "chat-b")
    try:
        login = {"session_id": "site", "steps": [{"url": f"{base}/login"}, {"url": f"{base}/me"}]}
        mine = await _through_mcp("scrape_with_session", login, a)
        assert "who=owner-a" in mine["results"][1]["markdown"], mine  # the owner's own session keeps its login
        again = await _through_mcp("scrape_with_session", {"session_id": "site", "steps": [{"url": f"{base}/me"}]}, a)
        assert "who=owner-a" in again["results"][0]["markdown"], again

        theirs = await _through_mcp("scrape_with_session", {"session_id": "site", "steps": [{"url": f"{base}/me"}]}, b)
        assert "who=owner-a" not in theirs["results"][0]["markdown"], theirs
        read = await _through_mcp("scrape_url", {"url": f"{base}/me"}, b)
        assert "who=owner-a" not in read["markdown"], read
        anyone = await _through_mcp("scrape_url", {"url": f"{base}/me"})
        assert "who=owner-a" not in anyone["markdown"], anyone
    finally:
        site.close()
