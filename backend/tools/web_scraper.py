import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
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

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MARKDOWN_LEN = 50_000
MAX_SCREENSHOT_B64 = 2_000_000  # auto-save to tmp file above this size

# ---------------------------------------------------------------------------
# Crawler singleton — started lazily on first use
# ---------------------------------------------------------------------------

_crawler: AsyncWebCrawler | None = None
_crawler_lock = asyncio.Lock()


async def get_crawler() -> AsyncWebCrawler:
    global _crawler
    async with _crawler_lock:
        if _crawler is None:
            config = BrowserConfig(
                headless=True,
                browser_type="chromium",
                enable_stealth=True,
                verbose=False,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            _crawler = AsyncWebCrawler(config=config)
            await _crawler.start()
    return _crawler


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
        cache_mode=CacheMode.ENABLED if cache else CacheMode.BYPASS,
        word_count_threshold=word_count_threshold,
        excluded_tags=excluded_tags or ["script", "style"],
        remove_overlay_elements=remove_overlay_elements,
        screenshot=screenshot,
        scan_full_page=scan_full_page,
        delay_before_return_html=delay_before_return_html,
    )
    if wait_for:
        kwargs["wait_for"] = wait_for
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
            description=(
                "Take a screenshot of a webpage and return it as a base64-encoded PNG. "
                "Supports full-page capture and optional save-to-disk. "
                "If the screenshot exceeds 2 MB it is automatically saved to a temp file."
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
                        "description": "Absolute file path to save the PNG (e.g. '/tmp/screenshot.png'). If omitted, returns base64 in the response."
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
        result = await crawler.arun(url=url, config=run_config)
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")

    markdown = result.markdown or ""
    if not markdown.strip():
        return _err("Page returned no extractable content. Try adding a wait_for selector or js_code to handle dynamic loading.")

    response: dict = {"success": True, "url": url, "markdown": _truncate(markdown)}

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

    strategy = JsonCssExtractionStrategy(schema, verbose=False)
    crawler = await get_crawler()
    run_config = _build_run_config(
        wait_for=args.get("wait_for"),
        js_code=args.get("js_code"),
        headers=args.get("headers"),
        extraction_strategy=strategy,
    )

    with _stderr_stdout():
        result = await crawler.arun(url=url, config=run_config)
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
        try:
            with _stderr_stdout():
                r = await crawler.arun(url=url, config=run_config)
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
    return _ok({"success": True, "count": len(results), "results": list(results)})


async def _handle_extract_links(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")

    base_domain = urlparse(url).netloc
    filter_external = args.get("filter_external", False)
    filter_domains = set(args.get("filter_domains") or [])

    crawler = await get_crawler()
    run_config = _build_run_config(wait_for=args.get("wait_for"))

    with _stderr_stdout():
        result = await crawler.arun(url=url, config=run_config)
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
        result = await crawler.arun(url=url, config=run_config)
    if not result.success:
        return _err(f"Crawl failed: {result.error_message}")
    if not result.screenshot:
        return _err("Screenshot capture returned no data")

    screenshot_b64: str = result.screenshot
    output_path: str | None = args.get("output_path")

    # Auto-save to temp file if too large for inline embedding
    if not output_path and len(screenshot_b64) > MAX_SCREENSHOT_B64:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        output_path = tmp.name
        tmp.close()

    if output_path:
        try:
            img_bytes = base64.b64decode(screenshot_b64)
            with open(output_path, "wb") as fh:
                fh.write(img_bytes)
            return _ok({"success": True, "url": url, "saved_to": output_path, "size_bytes": len(img_bytes)})
        except Exception as exc:
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

        try:
            run_config = _build_run_config(
                session_id=session_id,
                js_code=step.get("js_code"),
                wait_for=step.get("wait_for"),
                headers=global_headers,
            )
            with _stderr_stdout():
                r = await crawler.arun(url=step_url, config=run_config)
            step_result: dict = {"step": i, "url": step_url, "success": r.success}
            if r.success and step.get("extract", True):
                step_result["markdown"] = _truncate(r.markdown or "")
            elif not r.success:
                step_result["error"] = r.error_message
            results.append(step_result)
        except Exception as exc:
            results.append({"step": i, "url": step_url, "success": False, "error": str(exc)})

    return _ok({
        "success": True,
        "session_id": session_id,
        "steps_completed": len(results),
        "results": results,
    })


async def _handle_search_page(args: dict) -> list[types.TextContent]:
    url = args.get("url", "").strip()
    if not url:
        return _err("url is required")

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
        result = await crawler.arun(url=url, config=run_config)
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
    global _crawler
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(read_stream, write_stream, app.create_initialization_options())
        finally:
            if _crawler is not None:
                await _crawler.stop()


if __name__ == "__main__":
    asyncio.run(main())
