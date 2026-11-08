"""The scraper's browser is closed when its server stops, with the method its browser has (F1016).

DEP2 saw it in rc4.1's roll (2026-10-24): every worker's web_scraper subprocess died on shutdown with
`AttributeError: 'AsyncWebCrawler' object has no attribute 'stop'` — crawl4ai 0.9's is `close()` — so the headless
browser was left to the process's death rather than closed. The shutdown closes it now, never raises (a failure
there must not be the process's last word), and every method the scraper calls on its browser is one its browser
class (`_Browser`, Playwright driven directly since F1157) has.
"""
import contextlib
import re
from pathlib import Path

import pytest

from tools import web_scraper


class _Crawler:
    """The browser's lifecycle and nothing more: `start()` and `close()`, no `stop()`."""

    def __init__(self, config=None, fail_close=False):
        self.started = self.closed = 0
        self.fail_close = fail_close

    async def start(self):
        self.started += 1

    async def close(self):
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("browser already gone")


@pytest.fixture
def server(monkeypatch):
    """`main()` with a stdio server that ends at once: what a shutdown runs."""

    @contextlib.asynccontextmanager
    async def stdio():
        yield None, None

    async def run(*_a, **_k):
        return None

    monkeypatch.setattr(web_scraper, "stdio_server", stdio)
    monkeypatch.setattr(web_scraper.app, "run", run)
    monkeypatch.setattr(web_scraper, "_crawler", None)


async def test_a_shutdown_closes_the_browser_it_started(server, monkeypatch):
    monkeypatch.setattr(web_scraper, "_Browser", _Crawler)
    crawler = await web_scraper.get_crawler()
    await web_scraper.main()
    assert crawler.started == 1 and crawler.closed == 1
    assert web_scraper._crawler is None


async def test_a_shutdown_with_no_browser_does_nothing(server):
    await web_scraper.main()


async def test_a_browser_that_will_not_close_does_not_crash_the_shutdown(server, monkeypatch, capsys):
    monkeypatch.setattr(web_scraper, "_crawler", _Crawler(fail_close=True))
    await web_scraper.main()
    assert "did not close" in capsys.readouterr().err


async def test_after_a_close_the_next_call_starts_a_new_browser(server, monkeypatch):
    monkeypatch.setattr(web_scraper, "_Browser", _Crawler)
    first = await web_scraper.get_crawler()
    await web_scraper.close_crawler()
    second = await web_scraper.get_crawler()
    assert second is not first and second.started == 1


def test_every_crawler_method_the_scraper_calls_is_one_its_browser_has():
    """The class: a method the browser renamed or never had (`stop`) fails here, not in a worker's shutdown."""
    source = Path(web_scraper.__file__).read_text()
    called = set(re.findall(r"\b_?crawler\.([a-z_]+)", source))
    assert called, "the scan found the scraper's crawler calls"
    missing = sorted(name for name in called if not hasattr(web_scraper._Browser, name))
    assert not missing, f"web_scraper calls browser methods _Browser does not have: {missing}"
    assert hasattr(web_scraper._Browser, "kill_session"), "what `_end_session` calls through `crawler_strategy`"
