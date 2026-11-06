"""A page that did not open says why, never a blank pane (F1425; Naveen's scratch, eb 11-06).

Signing in to x.com opened the pane on a white page: x.com answers this browser 403 with nothing in it, and a site that
can't be reached ends on Chromium's error page, which the headless shell draws white. The session now keeps what went
wrong with its page (`load_error`) and sends it with every frame, for the pane to say above the page: a navigation of
the main frame that failed (with the guard's own reason when it refused it), or a document that answered >= 400 with
nothing drawn. A page that draws something (a site's challenge) is never one: it is the person's to see. A page that
opens afterwards clears it. Apart from the banner, which says who drives.
"""
from __future__ import annotations

import asyncio
import types

from core.browser import gate
from core.browser.host import Session


class _Frame:
    def __init__(self, url="", parent=None):
        self.url, self.parent_frame = url, parent


MAIN = _Frame()


class _Request:
    def __init__(self, url, *, failure=None, frame=MAIN, nav=True, method="GET"):
        self.url, self.failure, self.frame, self._nav, self.method = url, failure, frame, nav, method

    def is_navigation_request(self):
        return self._nav


class _Response:
    def __init__(self, url, status, **kw):
        self.url, self.status, self.request = url, status, _Request(url, **kw)


class _Page:
    def __init__(self, drawn=False):
        self.url, self.drawn, self.events = "about:blank", drawn, []

    def on(self, event, _handler):
        self.events.append(event)

    async def evaluate(self, _js, *_a):
        return not self.drawn  # "nothing is drawn"


def _session(tmp_path, page=None) -> Session:
    s = Session("bs1425", "login:f1425", gate.CO_DRIVE, {}, tmp_path, wheel="person")
    s.page = page or _Page()
    return s


def test_every_page_is_watched_for_it(tmp_path):
    page = _Page()
    _session(tmp_path)._watch(page)
    assert {"requestfailed", "response", "framenavigated", "load"} <= set(page.events), page.events


def test_a_navigation_that_failed_says_why_and_goes_with_the_frame(tmp_path):
    s = _session(tmp_path)
    assert s.load_error is None and s.state()["load_error"] is None
    s._on_request_failed(_Request("https://nosuch.example/", failure="net::ERR_NAME_NOT_RESOLVED"))
    assert s.load_error == {"url": "https://nosuch.example/", "host": "nosuch.example", "kind": "not_found",
                            "code": "ERR_NAME_NOT_RESOLVED"}
    assert s.state()["load_error"] == s.load_error
    viewer: asyncio.Queue = asyncio.Queue()
    s.last_frame, s.viewers = {"jpeg_b64": "x", "url": "chrome-error://chromewebdata/"}, {viewer}
    s._push_state()
    assert viewer.get_nowait()["load_error"]["kind"] == "not_found", "a white page sends no new frame: the state does"


def test_each_failure_is_said_as_what_it_is(tmp_path):
    s = _session(tmp_path)
    for failure, kind in (("net::ERR_CONNECTION_REFUSED", "unreachable"), ("net::ERR_TIMED_OUT", "unreachable"),
                          ("net::ERR_CERT_AUTHORITY_INVALID", "certificate"), ("net::ERR_SSL_PROTOCOL_ERROR", "certificate"),
                          ("net::ERR_TOO_MANY_REDIRECTS", "failed")):
        s._on_request_failed(_Request("https://a.example/", failure=failure))
        assert s.load_error["kind"] == kind and s.load_error["code"] == failure.removeprefix("net::"), failure


async def test_the_guards_refusal_says_the_guards_reason(tmp_path):
    s = _session(tmp_path)
    aborted = []
    route = types.SimpleNamespace(request=_Request("http://10.0.0.7/admin"), continue_=None,
                                  abort=lambda why: aborted.append(why) or _done())
    await s._route(route)
    assert aborted == ["blockedbyclient"], "the guard refuses a private address, as before"
    s._on_request_failed(_Request("http://10.0.0.7/admin", failure="net::ERR_BLOCKED_BY_CLIENT"))
    assert s.load_error["kind"] == "blocked" and s.load_error["host"] == "10.0.0.7"
    assert s.load_error["why"] and s.load_error["why"] == s.log[-1]["target"], "the reason the guard logged"


async def _done():
    return None


def test_what_is_not_a_page_that_did_not_open_is_never_one(tmp_path):
    s = _session(tmp_path)
    s._on_request_failed(_Request("https://a.example/", failure="net::ERR_ABORTED"))  # another navigation took over
    s._on_request_failed(_Request("https://ads.example/f", failure="net::ERR_NAME_NOT_RESOLVED", frame=_Frame(parent=MAIN)))
    s._on_request_failed(_Request("https://a.example/x.js", failure="net::ERR_CONNECTION_REFUSED", nav=False))
    assert s.load_error is None


async def test_a_refusal_with_nothing_drawn_says_its_status_and_a_challenge_is_the_persons_to_see(tmp_path):
    s = _session(tmp_path, _Page(drawn=False))
    s._on_response(_Response("https://x.com/", 403))
    s._on_frame_navigated(_Frame("https://x.com/"))
    s.page.url = "https://x.com/"
    await s._on_load()
    assert s.load_error == {"url": "https://x.com/", "host": "x.com", "kind": "http", "status": 403}

    s = _session(tmp_path, _Page(drawn=True))  # a challenge page: 403, and something on it
    s._on_response(_Response("https://shop.example/", 403))
    s.page.url = "https://shop.example/"
    await s._on_load()
    assert s.load_error is None

    s = _session(tmp_path, _Page(drawn=False))  # an empty page that answered 200 is the site's own
    s._on_response(_Response("https://blank.example/", 200))
    s.page.url = "https://blank.example/"
    await s._on_load()
    assert s.load_error is None
    s._on_response(_Response("https://blank.example/f", 404, frame=_Frame(parent=MAIN)))  # a frame's, not the page's
    await s._on_load()
    assert s.load_error is None


def test_a_page_that_opens_clears_it_and_the_error_page_does_not(tmp_path):
    s = _session(tmp_path)
    s._on_request_failed(_Request("https://nosuch.example/", failure="net::ERR_NAME_NOT_RESOLVED"))
    s._on_frame_navigated(_Frame("chrome-error://chromewebdata/"))
    assert s.load_error is not None, "Chromium's own error page is where it failed"
    s._on_frame_navigated(_Frame("https://a.example/", parent=MAIN))
    assert s.load_error is not None, "a frame inside the page is not the page"
    s._on_frame_navigated(_Frame("https://example.com/"))
    assert s.load_error is None
    assert "banner" in s.state() and s.state()["banner"] is None, "it is never the banner"
