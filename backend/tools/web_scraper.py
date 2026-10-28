import asyncio
import base64
import dataclasses
import enum
import hashlib
import json
import os
import re
import sys
import time
import uuid
import weakref
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@contextmanager
def _stderr_stdout():
    """Redirect stdout→stderr for the duration of a page read.

    Nothing the browser runs may write to sys.stdout: it is the MCP stdio JSON-RPC stream. The MCP framework writes
    its JSON-RPC responses only after the tool handler returns, so temporarily swapping sys.stdout here is safe.
    """
    _real = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = _real

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from html_to_markdown import ConversionOptions, convert as _html_markdown
from playwright.async_api import Error as PlaywrightError, async_playwright
from playwright_stealth import Stealth

from core import egress_proxy, tool_scope
from core.file_roots import within
from core.egress_guard import BlockedEgress, check_host, check_url

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MARKDOWN_LEN = 50_000
MAX_SCREENSHOT_B64 = 2_000_000  # saved in OUTPUT_DIR above this size

#: Where `screenshot_url` saves a screenshot, and the only place it writes (F1066): the folder this server was
#: started with (`--output-dir`; the backend gives it `<vault>/tool_outputs/screenshots`). None on a worker's
#: shared server, which serves every tenant on its task and has no folder of theirs: nothing is saved there.
OUTPUT_DIR: Path | None = None


def output_dir_from(argv: list[str]) -> Path | None:
    """`--output-dir <path>` from this server's arguments, or None."""
    if "--output-dir" in argv[:-1]:
        return Path(argv[argv.index("--output-dir") + 1])
    return None


def _screenshot_target(requested: str | None) -> Path:
    """Where a screenshot asked to be saved as `requested` goes: inside `OUTPUT_DIR`, links and `..` resolved, or
    `ValueError`. A bare name is placed in the folder; no name gets one."""
    folder = OUTPUT_DIR
    if folder is None:
        raise ValueError("Screenshots are not saved to disk here: leave out output_path and the image comes back "
                         "in the answer.")
    path = Path(requested or f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png").expanduser()
    if not path.is_absolute():
        path = folder / path
    folder.mkdir(parents=True, exist_ok=True)
    if not within(path, [folder]):
        raise ValueError(f"Screenshots are saved only inside {folder}: give a file name such as 'page.png'.")
    return path


def _write_new(path: Path, data: bytes) -> Path:
    """Write `data` to a file that did not exist: `path`, else `path` with -1, -2… before its suffix. Never over a
    file, and never through a link at the last step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    for n in range(1000):
        candidate = path if n == 0 else path.with_name(f"{path.stem}-{n}{path.suffix}")
        try:
            fd = os.open(candidate, flags, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return candidate
    raise ValueError(f"too many screenshots named {path.name}")

# ---------------------------------------------------------------------------
# Egress guard — the browser must not reach private networks
# ---------------------------------------------------------------------------
#
# The pre-flight `check_url` calls in the handlers give clean, attributable
# errors, but they only see the URL the tool was handed. Redirect hops,
# `js_code`-driven navigation, and in-page fetches all happen inside Chromium,
# so the deny has to sit on the browser's own network layer too: every request
# in every context goes through `_guard_route`, and anything that lands in a
# blocked range is aborted before a connection is opened.

_routed_contexts: weakref.WeakSet = weakref.WeakSet()


async def _guard_route(route) -> None:
    allowed = False
    try:
        parsed = urlparse(route.request.url)
        if parsed.scheme in {"http", "https"}:
            await check_host(parsed.hostname or "")
            allowed = True
    except Exception:
        # BlockedEgress, a resolution failure, or a guard bug: never fail open.
        allowed = False
    try:
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")
    except Exception:
        pass  # the page or request is already gone


async def _attach_egress_guard(page, context=None, **kwargs):
    ctx = context if context is not None else page.context
    if ctx not in _routed_contexts:
        await ctx.route("**/*", _guard_route)
        _routed_contexts.add(ctx)
    return page


async def _preflight(url: str) -> str | None:
    """Error message if this URL must not be fetched, else None."""
    try:
        await check_url(url)
    except BlockedEgress as exc:
        return f"URL blocked: {exc}"
    return None


# ---------------------------------------------------------------------------
# The browser — started lazily on first use
# ---------------------------------------------------------------------------
#
# Playwright used directly (F1157): one headless Chromium per process, a context per caller (`_in_scope`), each
# context with stealth, the egress guard on its route and the egress proxy as its network.

#: What a page is read as. Chrome on Windows, as before the move off crawl4ai.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
VIEWPORT = {"width": 1080, "height": 600}
#: How long a page has to answer, and how long a loaded page is given to settle (its scripts' first requests).
PAGE_TIMEOUT_MS = 60_000
SETTLE_MS = 2_000
#: A named session, or a caller's context with none open, ends after this long unused.
IDLE_SECONDS = 30 * 60
#: A plain read's cache (F1110: only a read with no headers and no script is kept).
CACHE_ENTRIES = 64
CACHE_SECONDS = 10 * 60
#: A PDF the address downloads: read when it is no larger than this, and no longer than this many pages.
MAX_PDF_BYTES = 25_000_000
MAX_PDF_PAGES = 300


class CacheMode(enum.Enum):
    ENABLED = "enabled"
    BYPASS = "bypass"


@dataclass(frozen=True)
class ProxyConfig:
    """The proxy a context's pages go through. `username` is the caller's label: the proxy asks for no credentials,
    so it goes nowhere."""
    server: str
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class BrowserConfig:
    proxy_config: ProxyConfig | None = None
    user_agent: str = USER_AGENT
    extra_args: tuple[str, ...] = tuple(egress_proxy.CHROMIUM_ARGS)


@dataclass(frozen=True)
class RunConfig:
    """One page read: what to wait for, run and keep."""
    cache_mode: CacheMode = CacheMode.BYPASS
    excluded_tags: tuple[str, ...] = ("script", "style")
    remove_overlay_elements: bool = False
    screenshot: bool = False
    scan_full_page: bool = False
    delay_before_return_html: float = 0
    wait_for: str | None = None
    wait_for_timeout: int | None = None
    js_code: str | list[str] | None = None
    extraction_strategy: Any = None
    session_id: str | None = None
    proxy_config: ProxyConfig | None = None

    def clone(self, **updates) -> "RunConfig":
        return dataclasses.replace(self, **updates)


@dataclass
class CrawlResult:
    success: bool
    markdown: str = ""
    links: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)
    error_message: str | None = None
    extracted_content: str | None = None
    screenshot: str | None = None


_crawler: "_Browser | None" = None
_crawler_lock = asyncio.Lock()
#: The proxy the browser was started with (the host's, else Synapse's own). Every call's context names it again,
#: with the caller's label (`_in_scope`).
_browser_proxy: str | None = None


def _egress_proxy() -> str | None:
    """The proxy every page this browser loads goes through: the environment's HTTPS proxy, else its HTTP one, or
    None (F580). Chromium reads no proxy variables, so a host that routes egress through a proxy (cloud's I1:
    Smokescreen, which refuses any address that is not public) passed them to this process and nothing used them:
    every `scrape_url` left the worker directly. `NO_PROXY` is not read: it names the platform's own hosts, never a
    page an agent asks for (Knowledge's fetch takes its proxies the same way). The in-browser guard below stays the
    first layer either way, and with no proxy here the browser takes Synapse's own (`browser_config`)."""
    import urllib.request

    found = urllib.request.getproxies_environment()
    return found.get("https") or found.get("http") or None


def browser_config(local_proxy: str | None = None) -> BrowserConfig:
    """`local_proxy` is Synapse's own egress proxy (`core.egress_proxy`), used when the host names none: the
    browser's whole network goes through one or the other, so what the route guard does not see (a page's
    WebSockets, its service workers) is refused there."""
    proxy = _egress_proxy() or local_proxy
    return BrowserConfig(proxy_config=ProxyConfig(server=proxy) if proxy else None)


@dataclass
class _Context:
    context: Any
    #: A session's page, by session id, and when each was last used.
    pages: dict = field(default_factory=dict)
    used: dict = field(default_factory=dict)
    last: float = field(default_factory=time.monotonic)


class _Browser:
    """One headless Chromium, and a context per caller label: two labels never share a cookie jar.

    The label is the part of a run's session id before its first ':' (`_in_scope`). A session's page stays open in
    its label's context until `kill_session` or `IDLE_SECONDS` unused; a label of one call (`call-…`) closes its
    context with its last page."""

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._pw = None
        self._browser = None
        self._contexts: dict[str, _Context] = {}
        self._lock = asyncio.Lock()
        self._cache: OrderedDict = OrderedDict()

    @property
    def crawler_strategy(self) -> "_Browser":
        """Where `_end_session` finds `kill_session` (the name it had under crawl4ai, kept for its callers)."""
        return self

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        try:
            proxy = self.config.proxy_config
            # The browser's own proxy as well as each context's: Chromium on Windows takes a context's proxy only
            # when the browser was started with one, and nothing then leaves without it.
            self._browser = await self._pw.chromium.launch(
                headless=True, args=list(self.config.extra_args),
                **({"proxy": {"server": proxy.server}} if proxy else {}))
        except BaseException:
            await self._pw.stop()
            raise

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._contexts.clear()
            if self._pw is not None:
                await self._pw.stop()

    async def kill_session(self, session_id: str) -> None:
        async with self._lock:
            for label, ctx in list(self._contexts.items()):
                page = ctx.pages.pop(session_id, None)
                if page is None:
                    continue
                ctx.used.pop(session_id, None)
                await _quietly(page.close())
                if not ctx.pages and label.startswith("call-"):
                    del self._contexts[label]
                    await _quietly(ctx.context.close())

    async def arun(self, url: str, config: RunConfig) -> CrawlResult:
        key = (url, config.excluded_tags)
        if config.cache_mode is CacheMode.ENABLED and (hit := self._cached(key)) is not None:
            return hit
        try:
            page = await self._page(config)
        except PlaywrightError as exc:
            return CrawlResult(success=False, error_message=str(exc))
        try:
            result = await _read_page(page, url, config)
        except PlaywrightError as exc:
            result = CrawlResult(success=False, error_message=str(exc))
        if result.success and config.cache_mode is CacheMode.ENABLED:
            self._cache[key] = (time.monotonic(), dataclasses.replace(result))
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return result

    def _cached(self, key) -> CrawlResult | None:
        found = self._cache.get(key)
        if found is None or time.monotonic() - found[0] > CACHE_SECONDS:
            self._cache.pop(key, None)
            return None
        return dataclasses.replace(found[1])

    async def _page(self, config: RunConfig):
        """The run's page: its session's, in its label's context, or a new one there."""
        session_id = config.session_id or f"call-{uuid.uuid4().hex}:{uuid.uuid4().hex}"
        label = session_id.split(":", 1)[0]
        async with self._lock:
            await self._reap()
            ctx = self._contexts.get(label)
            if ctx is None:
                ctx = _Context(context=await self._new_context(config))
                self._contexts[label] = ctx
            page = ctx.pages.get(session_id)
            if page is None or page.is_closed():
                page = await ctx.context.new_page()
                ctx.pages[session_id] = page
            ctx.used[session_id] = ctx.last = time.monotonic()
        return page

    async def _new_context(self, config: RunConfig):
        proxy = config.proxy_config or self.config.proxy_config
        context = await self._browser.new_context(
            user_agent=self.config.user_agent, viewport=VIEWPORT, accept_downloads=True,
            # A service worker's requests reach neither route below; the proxy still holds them, and none is needed
            # to read a page.
            service_workers="block",
            **({"proxy": {"server": proxy.server, "username": proxy.username, "password": proxy.password}}
               if proxy else {}))
        try:
            await Stealth().apply_stealth_async(context)
            await _attach_egress_guard(None, context=context)
        except BaseException:
            await _quietly(context.close())
            raise
        return context

    async def _reap(self) -> None:
        """Close what has gone unused past `IDLE_SECONDS`: a session's page, then a context with none left."""
        now = time.monotonic()
        for label, ctx in list(self._contexts.items()):
            for session_id, used in list(ctx.used.items()):
                if now - used > IDLE_SECONDS:
                    ctx.used.pop(session_id, None)
                    page = ctx.pages.pop(session_id, None)
                    if page is not None:
                        await _quietly(page.close())
            if not ctx.pages and now - ctx.last > IDLE_SECONDS:
                del self._contexts[label]
                await _quietly(ctx.context.close())


async def _quietly(closing) -> None:
    try:
        await closing
    except Exception:  # noqa: BLE001 — the page or context is gone already; nothing to close
        pass


async def get_crawler() -> _Browser:
    global _crawler
    async with _crawler_lock:
        if _crawler is None:
            global _browser_proxy
            local = None if _egress_proxy() else (await egress_proxy.shared()).url
            config = browser_config(local)
            _browser_proxy = _egress_proxy() or local
            crawler = _Browser(config)
            await crawler.start()
            _crawler = crawler
    return _crawler


async def close_crawler() -> None:
    """Close the browser, if one was started. Never raises: it runs as the server stops, and a browser that will not
    close must not be the process's last word (F1016). The next call starts a new one."""
    global _crawler
    async with _crawler_lock:
        crawler, _crawler = _crawler, None
    if crawler is None:
        return
    try:
        await crawler.close()
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"web_scraper: the browser did not close cleanly: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------

async def _read_page(page, url: str, config: RunConfig) -> CrawlResult:
    """Load `url` in `page` and read it, in crawl4ai 0.9's order: load, scan, wait, delay, script, overlays, content,
    screenshot. A wait that never comes true is a failure that says "Wait condition failed" (`_arun_once`)."""
    await _send_headers(page, url=url)
    downloads: list = []

    def on_download(download) -> None:  # a function: Playwright tags its listeners, which a bound builtin can't take
        downloads.append(download)

    page.on("download", on_download)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    except PlaywrightError:
        if not downloads:
            await asyncio.sleep(0.5)  # the download event can follow the aborted navigation
        if downloads:
            return await _read_download(downloads[0], url)
        raise
    finally:
        page.remove_listener("download", on_download)
    try:
        await page.wait_for_load_state("networkidle", timeout=SETTLE_MS)
    except PlaywrightError:
        pass  # a page that keeps talking is read as it is
    if config.scan_full_page:
        await _scroll_through(page)
    if config.wait_for:
        try:
            await _wait(page, config.wait_for, config.wait_for_timeout or PAGE_TIMEOUT_MS)
        except PlaywrightError as exc:
            return CrawlResult(success=False, error_message=f"Wait condition failed: {exc}")
    if config.delay_before_return_html:
        await asyncio.sleep(config.delay_before_return_html)
    if config.js_code:
        for script in [config.js_code] if isinstance(config.js_code, str) else config.js_code:
            await _run_script(page, script)
    if config.remove_overlay_elements:
        await _quietly(page.evaluate(_REMOVE_OVERLAYS))
    html = await page.content()
    found = await page.evaluate(_LINKS_AND_IMAGES, ", ".join(config.excluded_tags))
    result = CrawlResult(success=True, markdown=_markdown(html, page.url, config.excluded_tags),
                         links=_links(found["links"], page.url), media={"images": found["images"]})
    if config.extraction_strategy is not None:
        result.extracted_content = json.dumps(config.extraction_strategy.extract(html), ensure_ascii=False)
    if config.screenshot:
        shot = await page.screenshot(full_page=config.scan_full_page, type="png")
        result.screenshot = base64.b64encode(shot).decode("ascii")
    return result


async def _wait(page, wait_for: str, timeout: int) -> None:
    """`js:` (or a function) waits for it to be true; `css:` (or anything else) for the element to show."""
    wait_for = wait_for.strip()
    if wait_for.startswith("js:"):
        await page.wait_for_function(wait_for[3:].strip(), timeout=timeout)
    elif wait_for.startswith(("()", "function")):
        await page.wait_for_function(wait_for, timeout=timeout)
    else:
        await page.wait_for_selector(wait_for.removeprefix("css:").strip(), timeout=timeout)


async def _run_script(page, script: str) -> None:
    """`script` as the body of an async function, so a top-level `await` works; its own error is the page's, and a
    navigation it starts is waited for."""
    try:
        await page.evaluate(f"(async () => {{ try {{ return await (async () => {{\n{script}\n}})(); }} "
                            f"catch (err) {{ return String(err); }} }})()")
    except PlaywrightError as exc:
        if "Execution context was destroyed" not in str(exc):
            raise
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        await page.wait_for_load_state("networkidle", timeout=1_000)
    except PlaywrightError:
        pass


async def _scroll_through(page, step_delay: float = 0.2, max_steps: int = 50) -> None:
    """Scroll to the bottom a screen at a time, so what loads on scroll is there, then back to the top."""
    for _ in range(max_steps):
        at_bottom = await page.evaluate(
            "() => { window.scrollBy(0, window.innerHeight);"
            " return window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2; }")
        await asyncio.sleep(step_delay)
        if at_bottom:
            break
    await page.evaluate("() => window.scrollTo(0, 0)")


_REMOVE_OVERLAYS = """() => {
  const words = /cookie|consent|gdpr|modal|popup|overlay|newsletter|subscribe|banner|paywall/i;
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    const style = getComputedStyle(el);
    if (style.position !== 'fixed' && style.position !== 'sticky') continue;
    const box = el.getBoundingClientRect();
    const big = box.width * box.height > 0.3 * innerWidth * innerHeight;
    const z = parseInt(style.zIndex, 10) || 0;
    if (big || z >= 100 || words.test(`${el.id} ${el.className}`)) el.remove();
  }
  for (const el of [document.documentElement, document.body]) {
    if (el) { el.style.overflow = 'auto'; el.style.position = 'static'; }
  }
}"""

_LINKS_AND_IMAGES = """(excluded) => {
  const out = (el) => !excluded || !el.closest(excluded);
  const links = Array.from(document.querySelectorAll('a[href]')).filter(out).map(a => ({
    href: a.href, text: (a.innerText || a.textContent || '').trim().replace(/\\s+/g, ' '),
    title: (a.getAttribute('title') || '').trim()}));
  const images = Array.from(document.images).filter(out).filter(img => img.src && !img.src.startsWith('data:'))
    .map(img => {
      let desc = '', el = img.parentElement;
      while (el && !desc) { desc = (el.innerText || '').trim().slice(0, 200); el = el.parentElement; }
      const width = img.naturalWidth || img.width, height = img.naturalHeight || img.height;
      const score = (img.alt ? 1 : 0) + (width > 150 ? 1 : 0) + (height > 150 ? 1 : 0)
        + (/\\.(jpe?g|png|webp)(\\?|$)/i.test(img.src) ? 1 : 0) + (desc ? 1 : 0);
      return {src: img.currentSrc || img.src, alt: img.alt || '', desc, score, type: 'image'};
    });
  return {links, images};
}"""

#: Query parameters that only say where a click came from: a link without them is the same page.
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref_src)$", re.IGNORECASE)
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "ac", "edu"})


def _base_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) > 2 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _normal_url(href: str) -> str | None:
    """`href` with no fragment and no tracking parameters, its query in order; None for what is not a web page."""
    parsed = urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                             if not _TRACKING.match(k)))
    return urlunparse(parsed._replace(query=query, fragment=""))


def _links(found: list[dict], page_url: str) -> dict:
    """The page's links, each once, as internal (the page's own site, subdomains included) or external."""
    base = _base_domain(page_url)
    seen: set[str] = set()
    out: dict = {"internal": [], "external": []}
    for link in found:
        href = _normal_url(link.get("href", ""))
        if href is None or href in seen:
            continue
        seen.add(href)
        domain = _base_domain(href)
        out["internal" if domain == base else "external"].append(
            {"href": href, "text": link.get("text", ""), "title": link.get("title", ""), "base_domain": domain})
    return out


#: An empty link to a place in the page (a heading's own anchor): kept as `[](url#place)`, which research's
#: `_section` matches a heading by, and which the converter would otherwise drop with the link's empty text.
_EMPTY_ANCHOR = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["'](#[^"'<>\s]+)["'][^>]*>\s*</a>""", re.IGNORECASE)


def _markdown(html: str, url: str, excluded_tags: tuple[str, ...]) -> str:
    base = url.split("#", 1)[0]
    html = _EMPTY_ANCHOR.sub(lambda m: f"[]({base}{m.group(1)})", html)
    result = _html_markdown(html, ConversionOptions(
        base_url=url, exclude_selectors=list(excluded_tags), extract_metadata=False,
        # What a reader can get to (an inactive tab, a closed section) is read; text hidden from every reader is
        # not, which is where a prompt hides in a page.
        hidden_content="reachable"))
    return (result.content or "").strip()


async def _read_download(download, url: str) -> CrawlResult:
    """The file an address downloads: a PDF is read as Knowledge reads one; anything else is not a page."""
    try:
        path = await asyncio.wait_for(download.path(), timeout=PAGE_TIMEOUT_MS / 1000)
    except (asyncio.TimeoutError, PlaywrightError) as exc:
        await _quietly(download.cancel())
        return CrawlResult(success=False, error_message=f"The download did not finish: {exc}")
    size = os.path.getsize(path)
    if size > MAX_PDF_BYTES:
        return CrawlResult(success=False, error_message=(
            f"The file is {size // 1_000_000} MB; a PDF is read up to {MAX_PDF_BYTES // 1_000_000} MB."))
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF"):
        return CrawlResult(success=False, error_message=(
            f"The address is a file to download ({download.suggested_filename}), not a page; "
            "a PDF is the only file read here."))
    try:
        markdown = await asyncio.to_thread(_pdf_markdown, data, url)
    except Exception as exc:  # noqa: BLE001 — an unreadable PDF is this call's failure, not the server's
        return CrawlResult(success=False, error_message=f"The PDF could not be read: {exc}")
    return CrawlResult(success=True, markdown=markdown, links={"internal": [], "external": []},
                       media={"images": []})


def _pdf_markdown(data: bytes, url: str) -> str:
    from core.knowledge.parse import parse_bytes

    doc = parse_bytes(data, path=Path(urlparse(url).path).name or "page.pdf", source="web",
                      sha256=hashlib.sha256(data).hexdigest(), mime="application/pdf", url=url,
                      max_pages=MAX_PDF_PAGES)
    return "\n\n".join(("#" * min(b.level or 1, 6) + " " + b.text) if b.kind == "heading" else b.text
                       for b in doc.blocks if b.text.strip())


class CssExtraction:
    """Repeating records from a page by CSS selectors: `baseSelector` finds each, `fields` (and `baseFields`, read off
    the record's own element) say what to take from it. The schema and output of crawl4ai's
    JsonCssExtractionStrategy, which the tool's callers already write.

    A field's `type` is `text`, `attribute` (its `attribute`), `html`, `regex` (its `pattern`, `group`) or a list of
    those in turn; `nested` (a record from the first match), `list` or `nested_list` (a record per match). A field
    with no match gets its `default`, else is left out; `transform` is lowercase, uppercase or strip. `computed`
    fields are not run: they were code."""

    def __init__(self, schema: dict):
        self.schema = schema

    def extract(self, html: str) -> list[dict]:
        from bs4 import BeautifulSoup

        records = []
        for element in BeautifulSoup(html, "html.parser").select(self.schema["baseSelector"]):
            record = self._record(element, self.schema.get("baseFields", []), single=True)
            record.update(self._record(element, self.schema.get("fields", [])))
            if record:
                records.append(record)
        return records

    def _record(self, element, fields: list[dict], single: bool = False) -> dict:
        record = {}
        for f in fields:
            if f.get("type") == "computed":
                continue
            try:
                value = self._single(element, f) if single else self._field(element, f)
            except Exception:  # noqa: BLE001 — a field that cannot be read is its default, as before
                value = f.get("default")
            if value is not None:
                record[f["name"]] = value
        return record

    def _field(self, element, f: dict):
        kind = f.get("type")
        if kind in ("nested", "list", "nested_list"):
            matches = element.select(f["selector"])
            if kind == "nested":
                return self._record(matches[0], f["fields"]) if matches else {}
            return [self._record(el, f["fields"], single=kind == "list") for el in matches]
        return self._single(element, f)

    def _single(self, element, f: dict):
        if "selector" in f:
            found = element.select(f["selector"])
            if not found:
                return f.get("default")
            element = found[0]
        value = element
        for step in f["type"] if isinstance(f["type"], list) else [f["type"]]:
            if step == "text":
                value = value.get_text(strip=True)
            elif step == "attribute":
                value = value.get(f["attribute"])
                value = " ".join(value) if isinstance(value, list) else value
            elif step == "html":
                value = str(value)
            elif step == "regex" and f.get("pattern"):
                text = value if isinstance(value, str) else value.get_text(strip=True)
                match = re.search(f["pattern"], text)
                value = match.group(f.get("group", 1)) if match else None
            if value is None:
                break
        if isinstance(value, str) and f.get("transform") in ("lowercase", "uppercase", "strip"):
            value = {"lowercase": value.lower, "uppercase": value.upper, "strip": value.strip}[f["transform"]]()
        return value if value is not None else f.get("default")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

app = Server("web-scraper-mcp-server")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(text: str) -> str:
    if len(text) > MAX_MARKDOWN_LEN:
        omitted = len(text) - MAX_MARKDOWN_LEN
        return text[:MAX_MARKDOWN_LEN] + f"\n\n[...truncated — {omitted} chars omitted]"
    return text


def _ok(data: dict) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]


def _err(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps({"success": False, "error": msg}))]


def _score_links(links: list[dict]) -> list[dict]:
    scored = []
    for link in links:
        href = link.get("href", "")
        text = link.get("text", "")
        depth = max(0, href.count("/") - 2)
        text_score = min(1.0, len(text.strip()) / 50) * 0.5
        depth_score = max(0.0, 1.0 - depth * 0.1) * 0.5
        scored.append({**link, "score": round(text_score + depth_score, 3)})
    return sorted(scored, key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# Whose browser state a call uses (F1067)
# ---------------------------------------------------------------------------
#
# One browser serves every caller of this process (on a worker, every tenant of its task). The engine says who a
# call is for (`core.tool_scope`, MCP `_meta`), and each tenant + chat or run gets a context of its own, one cookie
# jar (`_Browser`): its label leads the run's session id and is the proxy user name. The proxy asks for no
# credentials, so the name goes nowhere.
#
# A call with no named session is a session of its own, its page closed when the call ends. A named session is the
# caller's, under its label, until `IDLE_SECONDS` unused; a caller that does not say who it is gets a label for the
# one call, and its sessions and context end with it.

@dataclass
class _Scope:
    label: str
    #: Sessions to end when the call does: a caller that said nothing keeps none.
    ends: set[str] | None = field(default=None)


_scope: ContextVar[_Scope | None] = ContextVar("web_scraper_scope", default=None)


def _scope_for(meta) -> _Scope:
    found = tool_scope.scope_from(meta)
    if found is None:
        return _Scope(label=f"call-{uuid.uuid4().hex[:16]}", ends=set())
    return _Scope(label=hashlib.sha256("\0".join(found).encode()).hexdigest()[:16])


def _in_scope(config: RunConfig) -> tuple[RunConfig, bool]:
    """`config` in the caller's own context, and whether its session ends after this one run."""
    scope = _scope.get()
    direct = scope is None  # a handler called outside a request: this run is all there is
    if direct:
        scope = _Scope(label=f"call-{uuid.uuid4().hex[:16]}")
    updates: dict = {}
    if _browser_proxy:
        updates["proxy_config"] = ProxyConfig(server=_browser_proxy, username=f"synapse-{scope.label}",
                                              password="")
    if config.session_id:
        updates["session_id"] = f"{scope.label}:{config.session_id}"
        if scope.ends is not None:
            scope.ends.add(updates["session_id"])
        ends_now = direct
    else:
        updates["session_id"] = f"{scope.label}:{uuid.uuid4().hex}"
        ends_now = True
    return config.clone(**updates), ends_now


#: The headers a run sends, and the site (host:port) they are for: read by `_send_headers` as the page opens.
_page_headers: ContextVar[tuple[dict, str] | None] = ContextVar("web_scraper_headers", default=None)
#: Each page's headers and site, set before each of its navigations; its route reads them.
_routed_pages: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


async def _send_headers(page, context=None, url=None, **kwargs):
    """Before each navigation: the run's `headers` on its page, for requests to the run's own site only (F1123).

    They are added at the page's own route, which passes every request on to the context's (`_guard_route`: the
    egress guard still sees each one). A script or image from another host does not get them: an `Authorization`
    meant for one site is not every host's."""
    wanted = _page_headers.get()
    if page not in _routed_pages:
        async def route(r):
            headers, site = _routed_pages.get(page) or ({}, "")
            if headers and urlparse(r.request.url).netloc == site:
                await r.fallback(headers={**r.request.headers, **headers})
            else:
                await r.fallback()
        await page.route("**/*", route)
    _routed_pages[page] = wanted or ({}, "")
    return page


async def _end_session(crawler, session_id: str) -> None:
    try:
        await crawler.crawler_strategy.kill_session(session_id)
    except Exception:  # noqa: BLE001 — the page may be gone already; nothing to end
        pass


#: How long a `wait_for` waits before the page is taken as it is (F569, SUB1b's number; fixed by BWA1).
WAIT_FOR_TIMEOUT_MS = 10_000


async def _arun(crawler, url: str, config: RunConfig, headers: dict | None = None):
    """`crawler.arun`, and when its `wait_for` never appeared, the page as it was rather than a failure (F569).

    gpt-4.1-mini waits for `title`, `head` or a selector that is not there, unprompted; the call then failed after
    the whole page timeout, and a 1-credit turn billed 4-5. One more fetch without the wait, which says so in
    `wait_missed`. Any other failure is left as it is.

    Run in the caller's own browser context (`_in_scope`), and a run with no named session ends its own. `headers`
    go to the page's own site (`_send_headers`)."""
    config, ends_now = _in_scope(config)
    token = _page_headers.set((dict(headers), urlparse(url).netloc) if headers else None)
    try:
        return await _arun_once(crawler, url, config)
    finally:
        _page_headers.reset(token)
        if ends_now:
            await _end_session(crawler, config.session_id)


async def _arun_once(crawler, url: str, config: RunConfig):
    result = await crawler.arun(url=url, config=config)
    wait = getattr(config, "wait_for", None)
    if wait and not result.success and "Wait condition failed" in (result.error_message or ""):
        result = await crawler.arun(url=url, config=config.clone(wait_for=None, wait_for_timeout=None)
                                    if hasattr(config, "clone") else _build_run_config())
        # The result's own dict, so any result object carries it too.
        result.__dict__["wait_missed"] = (f"'{wait}' did not appear within {WAIT_FOR_TIMEOUT_MS // 1000} s; "
                                          "this is the page as it was")
    return result


def _cache_mode(cache: bool, headers: dict | None, js_code) -> CacheMode:
    """A page read with someone's headers, or after someone's script, is theirs: the cache, keyed by URL alone and
    shared by every caller, is for a plain read only (F1110)."""
    return CacheMode.ENABLED if cache and not headers and js_code is None else CacheMode.BYPASS


def _build_run_config(
    *,
    wait_for: str | None = None,
    js_code=None,
    headers: dict | None = None,
    cache: bool = False,
    screenshot: bool = False,
    scan_full_page: bool = False,
    delay_before_return_html: float = 0,
    remove_overlay_elements: bool = False,
    excluded_tags: list | None = None,
    extraction_strategy=None,
    session_id: str | None = None,
) -> RunConfig:
    kwargs: dict = dict(
        cache_mode=_cache_mode(cache, headers, js_code),
        excluded_tags=tuple(excluded_tags or ["script", "style"]),
        remove_overlay_elements=remove_overlay_elements,
        screenshot=screenshot,
        scan_full_page=scan_full_page,
        delay_before_return_html=delay_before_return_html,
    )
    if wait_for:
        kwargs["wait_for"] = wait_for
        # Not the 60 s page timeout: a wait for something that never appears held every call that long (F569).
        kwargs["wait_for_timeout"] = WAIT_FOR_TIMEOUT_MS
    if js_code is not None:
        kwargs["js_code"] = js_code
    if extraction_strategy is not None:
        kwargs["extraction_strategy"] = extraction_strategy
    if session_id:
        kwargs["session_id"] = session_id
    return RunConfig(**kwargs)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="scrape_url",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
            description=(
                "Scrape a URL and return its content as clean markdown. "
                "Handles JavaScript-rendered pages, anti-bot protections, and dynamic content. "
                "Optionally returns all links and images found on the page."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to scrape"
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for before extracting (e.g. '.job-card', 'js:() => document.querySelectorAll(\".item\").length > 5')"
                    },
                    "js_code": {
                        "type": "string",
                        "description": "JavaScript to execute on the page before extraction (e.g. to dismiss popups, click buttons)"
                    },
                    "headers": {
                        "type": "object",
                        "description": "Additional HTTP headers, sent to the page's own site only (e.g. {\"Authorization\": \"Bearer token\"})",
                        "additionalProperties": {"type": "string"}
                    },
                    "remove_overlays": {
                        "type": "boolean",
                        "description": "Attempt to remove cookie banners, popups, and modals before extraction (default: true)",
                        "default": True
                    },
                    "include_links": {
                        "type": "boolean",
                        "description": "Include a list of all links found on the page (default: false)",
                        "default": False
                    },
                    "include_images": {
                        "type": "boolean",
                        "description": "Include a list of images found on the page (default: false)",
                        "default": False
                    },
                    "cache": {
                        "type": "boolean",
                        "description": "Use cached result if available — faster but may be stale (default: false)",
                        "default": False
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="scrape_structured",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
            description=(
                "Extract structured data from a URL using CSS selectors. "
                "Define a schema with a baseSelector (the repeating container element) and fields to extract from each. "
                "Ideal for job listings, product catalogs, stock tables, news articles, and any repeating content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to scrape"
                    },
                    "schema": {
                        "type": "object",
                        "description": "CSS extraction schema",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Optional schema name (e.g. 'job_listings')"
                            },
                            "baseSelector": {
                                "type": "string",
                                "description": "CSS selector for the repeating container element (e.g. '.job-card', 'li.result', 'tr.stock-row')"
                            },
                            "fields": {
                                "type": "array",
                                "description": "Fields to extract from each matched container element",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "Output field name"},
                                        "selector": {"type": "string", "description": "CSS selector relative to the container"},
                                        "type": {
                                            "type": "string",
                                            "enum": ["text", "attribute", "html", "nested"],
                                            "description": "text=inner text, attribute=HTML attribute value, html=inner HTML, nested=sub-schema"
                                        },
                                        "attribute": {"type": "string", "description": "HTML attribute name when type is 'attribute' (e.g. 'href', 'src', 'data-id')"},
                                        "default": {"type": "string", "description": "Default value if selector yields no match"}
                                    },
                                    "required": ["name", "selector", "type"]
                                }
                            }
                        },
                        "required": ["baseSelector", "fields"]
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for before extracting"
                    },
                    "js_code": {
                        "type": "string",
                        "description": "JavaScript to execute on the page before extraction"
                    },
                    "headers": {
                        "type": "object",
                        "description": "Additional HTTP headers, sent to the page's own site only",
                        "additionalProperties": {"type": "string"}
                    }
                },
                "required": ["url", "schema"]
            }
        ),
        types.Tool(
            name="crawl_multiple",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
            description=(
                "Scrape multiple URLs in parallel and return markdown content for each. "
                "All requests run concurrently for maximum efficiency. "
                "Returns a result list with success/error status per URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to scrape (maximum 20)",
                        "maxItems": 20
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for on each page"
                    },
                    "js_code": {
                        "type": "string",
                        "description": "JavaScript to execute on each page before extraction"
                    },
                    "headers": {
                        "type": "object",
                        "description": "HTTP headers to send with every page's request to its own site",
                        "additionalProperties": {"type": "string"}
                    },
                    "include_links": {
                        "type": "boolean",
                        "description": "Include extracted links in each result (default: false)",
                        "default": False
                    }
                },
                "required": ["urls"]
            }
        ),
        types.Tool(
            name="extract_links",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
            description=(
                "Extract and analyze all hyperlinks from a webpage. "
                "Returns internal and external links with anchor text and a relevance score. "
                "Useful for discovering job listing URLs, pagination links, or mapping site structure."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to extract links from"
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for before extracting links"
                    },
                    "filter_external": {
                        "type": "boolean",
                        "description": "Return only internal links (same domain as the input URL) (default: false)",
                        "default": False
                    },
                    "filter_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Domains to exclude from results (e.g. ['facebook.com', 'twitter.com'])"
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="screenshot_url",
            # It saves a file (F1066's folder): not a read, and a second call saves a second file.
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                              openWorldHint=True),
            description=(
                "Take a screenshot of a webpage and return it as a base64-encoded PNG. "
                "Supports full-page capture and optional saving to the vault's screenshots folder. "
                "If the screenshot exceeds 2 MB it is saved there automatically."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to screenshot"
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for before taking the screenshot"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "File name to save the PNG under, in the vault's screenshots folder (e.g. 'page.png'). If omitted, returns base64 in the response."
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page height, not just the viewport (default: false)",
                        "default": False
                    },
                    "delay": {
                        "type": "number",
                        "description": "Seconds to wait after page load before capturing (useful for animations, default: 0)",
                        "default": 0
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="scrape_with_session",
            # It logs in, clicks and fills forms (its own description says so): what a POST does, by TAG1's table.
            annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                              openWorldHint=True),
            description=(
                "Execute a multi-step scraping workflow using a persistent browser session. "
                "Browser cookies, localStorage, and authentication state are preserved across all steps. "
                "Each step can navigate to a URL, run JavaScript (login, click, fill forms), wait for elements, and extract content. "
                "Ideal for authenticated scraping of LinkedIn, stock trading platforms, and any site requiring login."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Unique identifier for this browser session. Reuse the same ID in subsequent calls to continue the session."
                    },
                    "steps": {
                        "type": "array",
                        "description": "Ordered list of steps to execute in the browser session",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": "URL to navigate to"
                                },
                                "js_code": {
                                    "type": "string",
                                    "description": "JavaScript to execute after navigation (e.g. fill login form, click submit)"
                                },
                                "wait_for": {
                                    "type": "string",
                                    "description": "CSS selector or JS expression to wait for after JS execution"
                                },
                                "extract": {
                                    "type": "boolean",
                                    "description": "Whether to extract and return page content from this step (default: true)",
                                    "default": True
                                }
                            },
                            "required": ["url"]
                        },
                        "minItems": 1
                    },
                    "headers": {
                        "type": "object",
                        "description": "HTTP headers for every step, sent to each page's own site only",
                        "additionalProperties": {"type": "string"}
                    }
                },
                "required": ["session_id", "steps"]
            }
        ),
        types.Tool(
            name="search_page",
            annotations=types.ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
            description=(
                "Scrape a dynamic or infinite-scroll page by executing multiple scroll cycles. "
                "After each scroll the page is allowed to load new content. "
                "Optionally apply a CSS extraction schema to parse the accumulated content at the end. "
                "Ideal for LinkedIn job feeds, Twitter/X timelines, stock screeners, and paginated dashboards."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to scrape"
                    },
                    "scroll_count": {
                        "type": "integer",
                        "description": "Number of scroll-down iterations (1–20, default: 3)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 3
                    },
                    "wait_for": {
                        "type": "string",
                        "description": "CSS selector or JS expression to wait for after the initial page load"
                    },
                    "js_code": {
                        "type": "string",
                        "description": "Custom JavaScript for each scroll iteration (default: scrolls to bottom of page)"
                    },
                    "extract_schema": {
                        "type": "object",
                        "description": "Optional CSS extraction schema (same format as scrape_structured's schema) applied after all scrolls complete",
                        "properties": {
                            "name": {"type": "string"},
                            "baseSelector": {"type": "string"},
                            "fields": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "selector": {"type": "string"},
                                        "type": {"type": "string", "enum": ["text", "attribute", "html", "nested"]},
                                        "attribute": {"type": "string"},
                                        "default": {"type": "string"}
                                    },
                                    "required": ["name", "selector", "type"]
                                }
                            }
                        },
                        "required": ["baseSelector", "fields"]
                    }
                },
                "required": ["url"]
            }
        ),
    ]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _handle_scrape_url(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")
    if (blocked := await _preflight(url)) is not None:
        return _err(blocked)

    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        js_code=args.get("js_code"),
        headers=args.get("headers"),
        cache=args.get("cache", False),
        remove_overlay_elements=args.get("remove_overlays", True),
        excluded_tags=["script", "style", "nav", "footer"],
    )

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config, headers=args.get("headers"))
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")

    markdown = result.markdown or ""
    if not markdown.strip():
        return _err("Page returned no extractable content. Try adding a wait_for selector or js_code to handle dynamic loading.")

    response: dict = {"success": True, "url": url, "markdown": _truncate(markdown)}
    if getattr(result, "wait_missed", None):
        response["wait_missed"] = result.wait_missed

    if args.get("include_links", False):
        all_links = result.links.get("internal", []) + result.links.get("external", [])
        response["links"] = all_links[:200]

    if args.get("include_images", False):
        response["images"] = (result.media or {}).get("images", [])[:50]

    return _ok(response)


async def _handle_scrape_structured(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    schema = args.get("schema")
    if not url:
        return _err("url is required")
    if not schema or not schema.get("baseSelector") or not schema.get("fields"):
        return _err("schema must include baseSelector and fields")
    if (blocked := await _preflight(url)) is not None:
        return _err(blocked)

    strategy = CssExtraction(schema)
    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        js_code=args.get("js_code"),
        headers=args.get("headers"),
        extraction_strategy=strategy,
    )

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config, headers=args.get("headers"))
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")

    extracted = []
    if result.extracted_content:
        try:
            extracted = json.loads(result.extracted_content)
        except json.JSONDecodeError:
            return _err(f"Failed to parse extracted content as JSON: {result.extracted_content[:500]}")

    return _ok({"success": True, "url": url, "count": len(extracted), "data": extracted})


async def _handle_crawl_multiple(args: dict) -> list[types.TextContent]:
    urls = args.get("urls", [])
    if not urls:
        return _err("urls list is required")
    urls = urls[:20]

    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        js_code=args.get("js_code"),
        headers=args.get("headers"),
    )
    include_links = args.get("include_links", False)

    async def scrape_one(url: str) -> dict:
        if (blocked := await _preflight(url)) is not None:
            return {"url": url, "success": False, "error": blocked}
        try:
            with _stderr_stdout():
                r = await _arun(crawler, url, run_config, headers=args.get("headers"))
            item: dict = {"url": url, "success": r.success}
            if r.success:
                item["markdown"] = _truncate(r.markdown or "")
                if include_links:
                    item["links"] = (r.links.get("internal", []) + r.links.get("external", []))[:100]
            else:
                item["error"] = r.error_message
            return item
        except Exception as exc:
            return {"url": url, "success": False, "error": str(exc)}

    results = await asyncio.gather(*[scrape_one(u) for u in urls])
    succeeded = sum(1 for r in results if r.get("success"))
    return _ok({
        # A parallel batch: partial data is still data, but all-fail must
        # read as failure to the caller deciding whether to retry.
        "success": succeeded > 0,
        "count": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": list(results),
    })


async def _handle_extract_links(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")

    if (blocked := await _preflight(url)) is not None:
        return _err(blocked)

    base_domain = urlparse(url).netloc
    filter_external = args.get("filter_external", False)
    filter_domains = set(args.get("filter_domains") or [])

    crawler = await get_crawler()
    run_config = _build_run_config(wait_for=args.get("wait_for"))

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config)
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")

    all_links = result.links.get("internal", []) + result.links.get("external", [])

    if filter_external:
        all_links = [lnk for lnk in all_links if urlparse(lnk.get("href", "")).netloc == base_domain]

    if filter_domains:
        all_links = [lnk for lnk in all_links if urlparse(lnk.get("href", "")).netloc not in filter_domains]

    scored = _score_links(all_links)
    return _ok({"success": True, "url": url, "count": len(scored), "links": scored})


async def _handle_screenshot_url(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")

    if (blocked := await _preflight(url)) is not None:
        return _err(blocked)
    # Where it would be saved, refused before the page is fetched.
    target = None
    if args.get("output_path"):
        try:
            target = _screenshot_target(str(args["output_path"]))
        except ValueError as exc:
            return _err(str(exc))

    delay = float(args.get("delay", 0))
    full_page = args.get("full_page", False)

    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        screenshot=True,
        scan_full_page=full_page,
        delay_before_return_html=delay,
    )

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config)
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")
    if not result.screenshot:
        return _err("Screenshot capture returned no data")

    screenshot_b64: str = result.screenshot

    # Too large to hand back inline: saved in the folder, under a name of its own.
    if target is None and len(screenshot_b64) > MAX_SCREENSHOT_B64:
        try:
            target = _screenshot_target(None)
        except ValueError:
            return _err(f"The screenshot is larger than {MAX_SCREENSHOT_B64 // 1_000_000} MB and is not saved "
                        "to disk here; take it without full_page")

    if target is not None:
        try:
            img_bytes = base64.b64decode(screenshot_b64)
            saved = _write_new(target, img_bytes)
            return _ok({"success": True, "url": url, "saved_to": str(saved), "size_bytes": len(img_bytes)})
        except (OSError, ValueError) as exc:
            return _err(f"Failed to save screenshot: {exc}")

    return _ok({"success": True, "url": url, "screenshot_base64": screenshot_b64, "format": "png"})


async def _handle_scrape_with_session(args: dict) -> list[types.TextContent]:
    session_id = args.get("session_id", "").strip()
    steps = args.get("steps", [])
    if not session_id:
        return _err("session_id is required")
    if not steps:
        return _err("steps list is required")

    crawler = await get_crawler()
    global_headers = args.get("headers")
    results = []

    for i, step in enumerate(steps):
        step_url = (step.get("url") or "").strip()
        if not step_url:
            results.append({"step": i, "success": False, "error": "url is required in each step"})
            continue
        if (blocked := await _preflight(step_url)) is not None:
            results.append({"step": i, "url": step_url, "success": False, "error": blocked})
            continue

        try:
            run_config = _build_run_config(
                session_id=session_id,
                js_code=step.get("js_code"),
                wait_for=step.get("wait_for"),
                headers=global_headers,
            )
            with _stderr_stdout():
                r = await _arun(crawler, step_url, run_config, headers=global_headers)
            step_result: dict = {"step": i, "url": step_url, "success": r.success}
            if r.success and step.get("extract", True):
                step_result["markdown"] = _truncate(r.markdown or "")
            elif not r.success:
                step_result["error"] = r.error_message
            results.append(step_result)
        except Exception as exc:
            results.append({"step": i, "url": step_url, "success": False, "error": str(exc)})

    return _ok({
        # A sequential workflow: a failed step invalidates what follows, so
        # the whole call only succeeds when every step did.
        "success": all(r.get("success") for r in results),
        "session_id": session_id,
        "steps_completed": len(results),
        "results": results,
    })


async def _handle_search_page(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")

    if (blocked := await _preflight(url)) is not None:
        return _err(blocked)

    scroll_count = max(1, min(int(args.get("scroll_count", 3)), 20))
    extract_schema = args.get("extract_schema")

    # Build a list of JS instructions: one scroll per iteration + a small delay
    scroll_js = args.get("js_code") or "window.scrollTo(0, document.body.scrollHeight);"
    js_steps = [scroll_js] * scroll_count

    extraction_strategy = None
    if extract_schema:
        if not extract_schema.get("baseSelector") or not extract_schema.get("fields"):
            return _err("extract_schema must include baseSelector and fields")
        extraction_strategy = CssExtraction(extract_schema)

    crawler = await get_crawler()
    run_config = _build_run_config(
        js_code=js_steps,
        wait_for=args.get("wait_for"),
        extraction_strategy=extraction_strategy,
        delay_before_return_html=1.0,
        scan_full_page=True,
    )

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config)
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")

    response: dict = {"success": True, "url": url, "scrolls_executed": scroll_count}
    if extract_schema and result.extracted_content:
        try:
            response["data"] = json.loads(result.extracted_content)
        except json.JSONDecodeError:
            response["data"] = result.extracted_content
    else:
        response["markdown"] = _truncate(result.markdown or "")

    return _ok(response)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        meta = app.request_context.meta
    except LookupError:
        meta = None
    scope = _scope_for(meta)
    token = _scope.set(scope)
    try:
        return await _dispatch(name, arguments)
    finally:
        _scope.reset(token)
        if scope.ends:  # a session was opened, so the browser is up
            crawler = await get_crawler()
            for session_id in scope.ends:
                await _end_session(crawler, session_id)


async def _dispatch(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        match name:
            case "scrape_url":            return await _handle_scrape_url(arguments)
            case "scrape_structured":     return await _handle_scrape_structured(arguments)
            case "crawl_multiple":        return await _handle_crawl_multiple(arguments)
            case "extract_links":         return await _handle_extract_links(arguments)
            case "screenshot_url":        return await _handle_screenshot_url(arguments)
            case "scrape_with_session":   return await _handle_scrape_with_session(arguments)
            case "search_page":           return await _handle_search_page(arguments)
            case _:                       return _err(f"Unknown tool: {name}")
    except Exception as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global OUTPUT_DIR
    OUTPUT_DIR = output_dir_from(sys.argv)
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(read_stream, write_stream, app.create_initialization_options())
        finally:
            await close_crawler()


if __name__ == "__main__":
    asyncio.run(main())
