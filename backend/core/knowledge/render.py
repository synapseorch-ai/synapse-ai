"""Knowledge's headless render of a page whose words only JavaScript writes (F1632 c).

A crawl's plain fetch of such a page reads an empty root and a skip link, which F1660 keeps out of a base with words
that say it may need JavaScript. Where Chromium is installed, that page is rendered once and its rendered words read:

* **As SynapseBot.** The render sends Knowledge's own user agent (`fetch.USER_AGENT`), never a browser's, and takes
  no browser check: a page that answered 403 or a challenge is never rendered (the crawl stops before this), so a
  render is never a way past a site's protection.
* **Behind the egress guard, twice.** The address is checked before Chromium is asked to open it, every request the
  page makes is checked on the browser's own network layer, and the browser's network is the egress proxy (or the
  host's own proxy where the environment names one, as the scraper's is), so it dials only the address the guard
  checked. An address the guard refuses is never opened.
* **A few pages a crawl** (`MAX_RENDERS_PER_CRAWL`), each within `RENDER_TIMEOUT_MS`: a render is a browser's memory
  and seconds, never the whole of a big site.
* **None, never an error,** when Chromium is not installed or the render fails: the page keeps F1660's words.

`render` is the seam the crawl calls; `set_renderer` puts another in its place (a test's stand-in).
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from core.log import get_logger

_log = get_logger(__name__)

#: The most pages one crawl renders.
MAX_RENDERS_PER_CRAWL = 20
#: One render's deadline: the page's load, then a short wait for its network to settle.
RENDER_TIMEOUT_MS = 20_000
SETTLE_MS = 3_000


@dataclass(frozen=True)
class Rendered:
    """A rendered page: its DOM as HTML, and the address it ended on (a script can send it elsewhere)."""

    html: str
    url: str


Renderer = Callable[..., Awaitable["Rendered | None"]]
_renderer: Renderer | None = None
_browser = None
_playwright = None
_browser_loop: asyncio.AbstractEventLoop | None = None
_lock: asyncio.Lock | None = None


def set_renderer(renderer: Renderer | None) -> None:
    """Render with `renderer` (`url, *, user_agent -> Rendered | None`), or with headless Chromium again when None."""
    global _renderer
    _renderer = renderer


async def render(url: str, *, user_agent: str) -> Rendered | None:
    """The page at `url` as Chromium renders it for `user_agent`, or None (see the module)."""
    return await (_renderer or render_page)(url, user_agent=user_agent)


def _browsers_folder() -> Path:
    if folder := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return Path(folder)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def _chromium_installed() -> bool:
    folder = _browsers_folder()
    try:
        return folder.is_dir() and any(p.name.startswith("chromium") for p in folder.iterdir())
    except OSError:
        return False


def _environment_proxy() -> str | None:
    """The host's own proxy, where the environment names one (cloud's), as the scraper's browser takes it (F580)."""
    import urllib.request

    proxies = urllib.request.getproxies_environment()
    return proxies.get("https") or proxies.get("http")


async def _browser_for_this_loop():
    """One headless Chromium for this event loop, started on first use, on the egress proxy."""
    global _browser, _playwright, _browser_loop, _lock
    from playwright.async_api import async_playwright

    from core import egress_proxy

    loop = asyncio.get_running_loop()
    if _lock is None or _browser_loop is not loop:
        _lock = asyncio.Lock()
    async with _lock:
        if _browser is not None and _browser_loop is loop and _browser.is_connected():
            return _browser
        await _close_quietly()
        proxy = _environment_proxy() or (await egress_proxy.shared()).url
        _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.launch(headless=True, proxy={"server": proxy},
                                                         args=list(egress_proxy.CHROMIUM_ARGS))
        except BaseException:
            await _playwright.stop()
            _playwright = None
            raise
        _browser_loop = loop
        return _browser


async def _guard(route) -> None:
    """Every request the page makes, checked as the scraper's are: what the guard refuses is aborted, never fails open."""
    from core.egress_guard import check_host

    allowed = False
    try:
        parsed = urlparse(route.request.url)
        if parsed.scheme in {"http", "https"}:
            await check_host(parsed.hostname or "")
            allowed = True
        elif parsed.scheme in {"data", "blob", "about"}:
            allowed = True
    except Exception:  # noqa: BLE001 — a refusal, a failed lookup or a guard fault: aborted
        allowed = False
    try:
        await (route.continue_() if allowed else route.abort("blockedbyclient"))
    except Exception:  # noqa: BLE001 — the page or the request is already gone
        pass


async def render_page(url: str, *, user_agent: str) -> Rendered | None:
    """Headless Chromium's render of `url` for `user_agent`, or None where it cannot or must not be rendered."""
    from core.egress_guard import BlockedEgress, check_url

    if not _chromium_installed():
        return None
    try:
        await check_url(url)
    except BlockedEgress:
        return None
    try:
        browser = await _browser_for_this_loop()
    except Exception as exc:  # noqa: BLE001 — no browser here: the page keeps F1660's words
        _log.warning("knowledge could not start Chromium to render a page: %s", exc)
        return None
    context = None
    try:
        context = await browser.new_context(user_agent=user_agent, service_workers="block")
        await context.route("**/*", _guard)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
        except Exception:  # noqa: BLE001 — a page that keeps a connection open is read as it stands
            pass
        return Rendered(html=await page.content(), url=page.url)
    except Exception as exc:  # noqa: BLE001 — a render that fails is a page left as the plain fetch read it
        _log.info("knowledge's render of %s failed: %s", url, exc)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass


async def _close_quietly() -> None:
    global _browser, _playwright, _browser_loop
    browser, pw = _browser, _playwright
    _browser = _playwright = _browser_loop = None
    for closing in ((browser.close() if browser is not None else None), (pw.stop() if pw is not None else None)):
        if closing is not None:
            try:
                await closing
            except Exception:  # noqa: BLE001 — already gone
                pass


async def close() -> None:
    """Stop the browser this process started for renders, if any."""
    await _close_quietly()
