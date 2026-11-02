"""The sites and APIs a message names, read once before planning (D340 B; TRI1's gather, D0; Naveen 10-30).

The planner planned blind: a prod run invented `synapseorchestrator.com` for the site the person meant. Now gather
reads each site the message names, once, and hands the planner what code found, never the page itself:

* `named(text)`: the URLs and bare domains a message names, at most three. A bare domain counts by `core.sites`'
  rule and a common ending; an email's domain is the person's address, not a site (F1253's net); a version (`v2.0`) or
  a file (`main.py`) is neither; a domain already inside a counted URL is not counted twice.
* `read(url)`: one plain GET through `safe_fetch` (the egress guard at every redirect hop, NAT64-aware since F1238), 8
  seconds, 2 MB, no browser and no model (Naveen: a plain fetch, cheap on every turn). What comes back is a
  `NamedPage` of short typed facts: the title, a line of what it is (the page's own words, quoted), one page or a
  site, an API spec it links to, or why it could not be read. A page that is only JavaScript (no title, no heading,
  no text) is `needs_browser`: the plan's fetch of that site is the scraper or a Browser step, not an HTTP tool.
* `read_named(text, already=…)`: the pages of a message, reading only those the conversation has not read already,
  so a re-plan, a question round or the next turn never fetches one again, and a page that changes mid-conversation
  never changes the plan under the person (TRI1).

Every read is a web call on the plan's budget (`builder_research.web_call`: 24 in all, Naveen 10-31). What a page says
reaches the planner only as these capped fields, and the planner is a workflow agent step, so a `remember` or a board
write after it asks (F279).
"""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

#: The most sites one message has read, in the order named.
MAX_NAMED = 3
#: One read: the whole download's deadline, and its size.
READ_SECONDS = 8.0
READ_BYTES = 2 * 1024 * 1024
#: Same-site links from which a page is the front of a site rather than one page.
SITE_LINKS = 15
#: Each typed fact's length: what the planner reads of a page, and no more.
TITLE_CHARS, WHAT_CHARS = 120, 200

#: Endings that name a file, not a site, when a bare word has one (`main.py`, `notes.md`); a site with one is still
#: named with `www.` or `https://`.
_FILE_ENDINGS = frozenset("py md js ts sh rs rb pl cs go json yaml yml txt csv pdf png jpg jpeg gif svg zip tar gz "
                          "html htm css xml doc docx xls xlsx ppt pptx log ini toml lock env sql".split())
_URL = re.compile(r"https?://[^\s<>\"'`)\]]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", re.I)
_BARE = re.compile(r"(?<![\w@./-])((?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+)(/[^\s<>\"'`)\]]*)?", re.I)


@dataclass
class NamedPage:
    """What one read found, as the planner sees it: short facts, never the page."""

    url: str
    reached: bool = False
    final_url: str = ""
    status: int = 0
    title: str = ""
    #: The page's own words (its description or first heading), quoted: what it says it is, not an instruction.
    what: str = ""
    #: `page`, `site` (its front, with many same-site links), `needs_browser` (only JavaScript), or "" unread.
    kind: str = ""
    same_site_links: int = 0
    #: An API spec the page links to (OpenAPI, Swagger, "API reference"), never a guessed address.
    api_spec: str | None = None
    why_not: str = ""
    redirects: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _trim(text: str, limit: int) -> str:
    text = " ".join(html.unescape(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_site(host: str, written: str) -> bool:
    """A bare word is a site when its ending is a site's and not a file's (unless written as a site)."""
    from core.builder_research import _common_ending

    ending = host.rsplit(".", 1)[-1].lower()
    if not ending.isalpha() or not _common_ending(host):
        return False
    return written.lower().startswith("www.") or ending not in _FILE_ENDINGS


def named(text: str) -> list[str]:
    """The URLs and bare domains `text` names, as https URLs, at most `MAX_NAMED`, in the order named."""
    from core.sites import registrable_domain

    text = text or ""
    emails = {m.group(1).lower() for m in _EMAIL.finditer(text)}
    found: list[str] = []
    seen_sites: set[str] = set()
    spans: list[tuple[int, int]] = []
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        host = (urlsplit(url).hostname or "").lower()
        if not host or "." not in host:
            continue
        spans.append(m.span())
        if url not in found:
            found.append(url)
            seen_sites.add(registrable_domain(host))
    without_emails = _EMAIL.sub(" ", text)
    for m in _BARE.finditer(without_emails):
        if any(a <= m.start() < b for a, b in spans):
            continue
        host = m.group(1).lower()
        if host in emails or not _is_site(host, m.group(1)):
            continue
        site = registrable_domain(host.removeprefix("www."))
        if site in seen_sites:
            continue  # inside a URL already counted, or named twice
        seen_sites.add(site)
        found.append(f"https://{host}{(m.group(2) or '').rstrip('.,;:!?')}")
    return found[:MAX_NAMED]


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DESC = re.compile(r"<meta[^>]+(?:name|property)=[\"'](?:description|og:description)[\"'][^>]*>", re.I)
_CONTENT = re.compile(r"content=[\"']([^\"']*)[\"']", re.I)
_HEADING = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
_LINK = re.compile(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_SPEC_HREF = re.compile(r"(openapi|swagger)[^/]*\.(json|ya?ml)$|/(openapi|swagger)(\.json|\.ya?ml)?$", re.I)
_SPEC_WORDS = re.compile(r"\b(openapi|swagger|api reference|api spec)\b", re.I)


def _facts(page: NamedPage, body: str) -> NamedPage:
    """The typed facts of an HTML page: its title, what it says it is, one page or a site, an API spec it links."""
    from core.sites import registrable_domain

    title = _TITLE.search(body)
    page.title = _trim(_TAG.sub(" ", title.group(1)) if title else "", TITLE_CHARS)
    desc = next((c.group(1) for d in _DESC.finditer(body) if (c := _CONTENT.search(d.group(0)))), "")
    heading = _HEADING.search(body)
    page.what = _trim(desc or (_TAG.sub(" ", heading.group(1)) if heading else ""), WHAT_CHARS)
    visible = _trim(_TAG.sub(" ", _SCRIPT.sub(" ", body)), 400)
    if not page.title and not heading and not visible:
        page.kind = "needs_browser"
        page.why_not = "the page is only JavaScript: a plain read shows nothing, so read it with the scraper"
        return page
    site = registrable_domain((urlsplit(page.final_url).hostname or "").lower())
    links = set()
    for m in _LINK.finditer(body):
        href = urljoin(page.final_url, html.unescape(m.group(1)).strip())
        host = (urlsplit(href).hostname or "").lower()
        if host and registrable_domain(host) == site:
            links.add(href.split("#")[0])
        if page.api_spec is None and href.startswith("https://") and (
                _SPEC_HREF.search(urlsplit(href).path) or _SPEC_WORDS.search(_TAG.sub(" ", m.group(2)))):
            page.api_spec = href
    page.same_site_links = len(links)
    page.kind = "site" if len(links) >= SITE_LINKS else "page"
    return page


async def read(url: str) -> NamedPage:
    """One guarded read of `url` (see the module): its `NamedPage`, a refusal or failure said in `why_not`."""
    import httpx

    from core import safe_fetch
    from core.builder_research import web_call

    page = NamedPage(url=url)
    if (over := await web_call()) is not None:
        page.why_not = over
        return page
    try:
        async with httpx.AsyncClient(timeout=READ_SECONDS) as client:
            got = await safe_fetch.fetch(url, client=client, max_bytes=READ_BYTES, timeout_s=READ_SECONDS,
                                         headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    except safe_fetch.FetchBlocked as exc:
        page.why_not = f"not read: {exc}"
        return page
    except safe_fetch.FetchTooLarge:
        page.why_not = f"not read: the page is over {READ_BYTES // (1024 * 1024)} MB"
        return page
    except safe_fetch.FetchRefused as exc:
        page.why_not = f"not read: {exc}"
        return page
    page.final_url, page.status, page.redirects = got.url, got.status, list(got.redirects)
    if not 200 <= got.status < 300:
        page.why_not = f"it answered {got.status}"
        return page
    page.reached = True
    kind = (got.headers.get("content-type") or "").lower()
    if "html" not in kind:
        page.kind = "page"
        page.why_not = f"it is not a web page ({kind.split(';')[0] or 'no type'})"
        if any(t in kind for t in ("json", "yaml")) and _SPEC_HREF.search(urlsplit(got.url).path or ""):
            page.api_spec = got.url
        return page
    return _facts(page, got.body.decode("utf-8", errors="replace"))


#: Where a conversation's reads are kept (D340 D2): gather and research add each read to the run's state under this
#: key, and the chat route seeds the next run with the previous run's (`carried`), so a site is read once a
#: conversation.
PAGES_KEY = "named_pages"


def carried(previous: Any) -> dict[str, dict[str, Any]]:
    """The reads the conversation's previous run kept, to seed the next one (the chat route)."""
    pages = previous.get(PAGES_KEY) if isinstance(previous, dict) else None
    return {u: dict(p) for u, p in pages.items() if isinstance(p, dict)} if isinstance(pages, dict) else {}


def settled(pages: Any) -> dict[str, dict[str, Any]]:
    """The reads a conversation keeps as read: only a site that was reached. One that could not be read is tried again
    the next time it is named (Naveen 10-31: a read can be wrong, and the person's word comes first; their correction
    is the planner's to take, a new address they give is read)."""
    if not isinstance(pages, dict):
        return {}
    return {u: p for u, p in pages.items() if isinstance(p, dict) and p.get("reached")}


async def read_named(text: str, *, already: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The pages `text` names, by URL, as dicts: those in `already` (the conversation's earlier reads) kept as they
    were, and only the rest read (TRI1's D0 calls this)."""
    out: dict[str, dict[str, Any]] = {}
    for url in named(text):
        earlier = (already or {}).get(url)
        out[url] = dict(earlier) if isinstance(earlier, dict) else (await read(url)).as_dict()
    return out
