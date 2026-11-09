"""A blocked page's reason is its own, never the attempt's before (F1504, eb's QA B14).

eb opened 169.254.169.254 and then localhost in a sign-in browser on prod, and the second page said "localhost was not
opened: this browser blocked it (169.254.169.254 is not a public address)". `_route` refused a page with
`await route.abort(...)` and only then kept the guard's reason; Playwright fires `requestfailed` while that abort is
awaited, so a failed page was said with the reason kept for the page before it, and the first with none. The reason is
now kept with its page's address before the abort, and a failed page takes only its own.
"""
from __future__ import annotations

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


def _session(tmp_path) -> Session:
    s = Session("bs1504", "login:f1504", gate.CO_DRIVE, {}, tmp_path, wheel="person")
    s.page = types.SimpleNamespace(url="about:blank")
    return s


async def _refused(s: Session, request: _Request) -> None:
    """The guard refuses `request`; as Playwright does, its `requestfailed` fires while the abort is awaited."""
    async def abort(_why):
        s._on_request_failed(_Request(request.url, failure="net::ERR_BLOCKED_BY_CLIENT", frame=request.frame,
                                      nav=request._nav))

    await s._route(types.SimpleNamespace(request=request, continue_=None, abort=abort))


async def test_each_blocked_page_says_its_own_reason(tmp_path):
    s = _session(tmp_path)
    await _refused(s, _Request("http://169.254.169.254/latest/meta-data"))
    assert s.load_error["kind"] == "blocked" and s.load_error["host"] == "169.254.169.254"
    assert s.load_error.get("why") == "169.254.169.254 is not a public address", s.load_error

    await _refused(s, _Request("http://127.0.0.1:8000/"))
    assert s.load_error["host"] == "127.0.0.1"
    assert s.load_error.get("why") == "127.0.0.1 is not a public address", s.load_error


async def test_a_page_never_takes_another_requests_reason(tmp_path):
    s = _session(tmp_path)
    await _refused(s, _Request("http://10.0.0.7/admin"))
    # A request the page made, refused for its own reason, is not the page's.
    await _refused(s, _Request("http://192.168.1.9/pixel.gif", frame=_Frame(parent=MAIN), nav=False))
    assert s.load_error.get("why") == "10.0.0.7 is not a public address", s.load_error
    # A page blocked by anything else (Chromium's own list) is said without one.
    s._on_request_failed(_Request("https://elsewhere.example/", failure="net::ERR_BLOCKED_BY_CLIENT"))
    assert s.load_error["host"] == "elsewhere.example" and "why" not in s.load_error, s.load_error
