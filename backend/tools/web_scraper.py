import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import time
import uuid
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse


@contextmanager
def _stderr_stdout():
    """Redirect stdout→stderr for the duration of a crawl4ai call.

    crawl4ai's progress reporter writes directly to sys.stdout, which corrupts
    the MCP stdio JSON-RPC stream.  The MCP framework writes its JSON-RPC
    responses only after the tool handler returns, so temporarily swapping
    sys.stdout here is safe.
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

# Suppress crawl4ai's stdout progress messages — they corrupt the MCP stdio stream
logging.getLogger("crawl4ai").setLevel(logging.ERROR)
logging.getLogger("crawl4ai.async_webcrawler").setLevel(logging.ERROR)

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode, ProxyConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

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
# Crawler singleton — started lazily on first use
# ---------------------------------------------------------------------------

_crawler: AsyncWebCrawler | None = None
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
    return BrowserConfig(
        headless=True,
        browser_type="chromium",
        enable_stealth=True,
        verbose=False,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        extra_args=list(egress_proxy.CHROMIUM_ARGS),
        **({"proxy_config": {"server": proxy}} if proxy else {}),
    )


async def get_crawler() -> AsyncWebCrawler:
    global _crawler
    async with _crawler_lock:
        if _crawler is None:
            global _browser_proxy
            local = None if _egress_proxy() else (await egress_proxy.shared()).url
            config = browser_config(local)
            _browser_proxy = _egress_proxy() or local
            _crawler = AsyncWebCrawler(config=config)
            _crawler.crawler_strategy.set_hook(
                "on_page_context_created", _attach_egress_guard
            )
            await _crawler.start()
    return _crawler


async def close_crawler() -> None:
    """Close the browser, if one was started, with crawl4ai's `close()` — 0.9 has no `stop()`, and calling it killed
    every shutdown with the browser still up (F1016). Never raises: it runs as the server stops, and a browser that
    will not close must not be the process's last word. The next call starts a new one."""
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
# One browser serves every caller of this process (on a worker, every tenant of its task), and crawl4ai keeps one
# context, one cookie jar, for every call with the same settings; a named session lived in it under a name any
# caller could use. The engine says now who a call is for (`core.tool_scope`, MCP `_meta`), and each tenant + chat
# or run gets a context of its own: its label is the proxy user name, which is part of the settings crawl4ai keys
# contexts by, so two labels never share one. The proxy asks for no credentials, so the name goes nowhere.
#
# A call with no named session is a session of its own, ended when the call is: nothing it set stays in the
# browser. A named session is the caller's, under its label, until crawl4ai's idle limit; a caller that does not say
# who it is gets a label for the one call, and its sessions end with it.

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


def _in_scope(config: CrawlerRunConfig) -> tuple[CrawlerRunConfig, bool]:
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


async def _end_session(crawler, session_id: str) -> None:
    try:
        await crawler.crawler_strategy.kill_session(session_id)
    except Exception:  # noqa: BLE001 — the page may be gone already; nothing to end
        pass


#: How long a `wait_for` waits before the page is taken as it is (F569, SUB1b's number; fixed by BWA1).
WAIT_FOR_TIMEOUT_MS = 10_000


async def _arun(crawler, url: str, config: CrawlerRunConfig):
    """`crawler.arun`, and when its `wait_for` never appeared, the page as it was rather than a failure (F569).

    gpt-4.1-mini waits for `title`, `head` or a selector that is not there, unprompted; crawl4ai then failed the
    call after its whole page timeout, and a 1-credit turn billed 4-5. One more fetch without the wait, which says
    so in `wait_missed`. Any other failure is left as it is.

    Run in the caller's own browser context (`_in_scope`), and a run with no named session ends its own."""
    config, ends_now = _in_scope(config)
    try:
        return await _arun_once(crawler, url, config)
    finally:
        if ends_now:
            await _end_session(crawler, config.session_id)


async def _arun_once(crawler, url: str, config: CrawlerRunConfig):
    result = await crawler.arun(url=url, config=config)
    wait = getattr(config, "wait_for", None)
    if wait and not result.success and "Wait condition failed" in (result.error_message or ""):
        result = await crawler.arun(url=url, config=config.clone(wait_for=None, wait_for_timeout=None)
                                    if hasattr(config, "clone") else _build_run_config())
        # The result's own dict, so a pydantic CrawlResult carries it too.
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
    word_count_threshold: int = 5,
    excluded_tags: list | None = None,
    extraction_strategy=None,
    session_id: str | None = None,
) -> CrawlerRunConfig:
    kwargs: dict = dict(
        cache_mode=_cache_mode(cache, headers, js_code),
        word_count_threshold=word_count_threshold,
        excluded_tags=excluded_tags or ["script", "style"],
        remove_overlay_elements=remove_overlay_elements,
        screenshot=screenshot,
        scan_full_page=scan_full_page,
        delay_before_return_html=delay_before_return_html,
    )
    if wait_for:
        kwargs["wait_for"] = wait_for
        # Not crawl4ai's 60 s page timeout: a wait for something that never appears held every call that long (F569).
        kwargs["wait_for_timeout"] = WAIT_FOR_TIMEOUT_MS
    if js_code is not None:
        kwargs["js_code"] = js_code
    if headers:
        kwargs["headers"] = headers
    if extraction_strategy is not None:
        kwargs["extraction_strategy"] = extraction_strategy
    if session_id:
        kwargs["session_id"] = session_id
    return CrawlerRunConfig(**kwargs)


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
                        "description": "Additional HTTP headers (e.g. {\"Authorization\": \"Bearer token\"})",
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
                        "description": "Additional HTTP headers",
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
                        "description": "HTTP headers to send with every request",
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
                        "description": "HTTP headers to use across all steps",
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
        result = await _arun(crawler, url, run_config)
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

    strategy = JsonCssExtractionStrategy(schema, verbose=False)
    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        js_code=args.get("js_code"),
        headers=args.get("headers"),
        extraction_strategy=strategy,
    )

    with _stderr_stdout():
        result = await _arun(crawler, url, run_config)
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
                r = await _arun(crawler, url, run_config)
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
                r = await _arun(crawler, step_url, run_config)
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
        extraction_strategy = JsonCssExtractionStrategy(extract_schema, verbose=False)

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
