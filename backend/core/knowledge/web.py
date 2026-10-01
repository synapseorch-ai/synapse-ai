"""Public web pages as a knowledge source: sources, crawls, and the pages they keep (S1, D165).

**A web source** is one URL in a knowledge base's `web.sources` (the record
holds only what a person chose): a `page`, a `sitemap` whose pages are read,
or a `site` crawled breadth-first from its URL — same host, under the URL's
directory, to the base's depth and page caps (each bounded by the deployment's
`knowledge_crawl_*` settings), through the source's include/exclude globs.

**A fetched page is a Vault file**, so one ingestion path serves every source
(D153 #1): HTML becomes `<folder>/_web/<host>/<path>.md` — its readable content
(A2's `parse.html.html_to_markdown`) under a front matter holding its title,
date and URL — and a document A2 reads (a PDF, a spreadsheet) is kept as it
came, at its own extension. The write goes through `sync.on_vault_change` with
`source="fetch"`; the same content queues nothing.

**Crawl state lives in engine `collections`**, one row per source
(`knowledge_web:<kws_id>`, Naveen's call 2 in D165): the latest crawl, its
counts, the pages the source holds (paths relative to the base's folder, so a
moved base keeps them) and the request's metadata and caps. The screen reads it
through `GET /knowledge/bases/{kb_id}/crawls` (CCR-4).

**What a crawl deletes** (Naveen's call 1): a page the site says is gone
(404/410), marks `noindex`, or robots.txt now disallows; and, after a crawl
that finished with no failure and under its cap, every page the source held
that the crawl no longer reached — unless another source still holds it.
Removing a source deletes its pages the same way. A capped or failing crawl
deletes nothing.

**Refusals and failures are documents** for URLs a person named (a `page`
source, a `site`'s start URL): `blocked` (`robots_disallowed`,
`egress_blocked`), `failed` (`fetch_failed`) or `skipped`, with a report whose
`fetch` stage says why. Such a row never had a file (`sha256` is null), so
reconcile keeps it (`sync`) and Retry fetches it again (`refetch`). Links a
crawl found and could not fetch are only counted.

**Re-crawl** is due `recrawl_days` after a crawl finished (0: never). OSS's
reconcile loop starts what is due (`start_due_crawls`) — and any crawl a
restart interrupted. Cloud never scans: it arms a job per `next_due_at` and runs
`run_crawl` (S1's cloud half).
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import mimetypes
import posixpath
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit, urlunsplit

from core.knowledge.ids import CRAWL_PREFIX, WEB_DIR, new_id, new_web_source_id
from core.log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.api_models import CrawlAccepted, UrlIngestRequest, WebCrawlState
    from core.knowledge.fetch import Fetcher, PageLinks
    from core.knowledge.kb_settings import KnowledgeBase, WebSource
    from core.store.models import KnowledgeDocumentDB

_log = get_logger(__name__)

STATE_PREFIX = "knowledge_web:"
MAX_SOURCES = 50
#: Crawls running at once in one process; more wait their turn.
MAX_CONCURRENT_CRAWLS = 2
#: How often OSS's reconcile loop looks for a re-crawl that is due.
DUE_CHECK_EVERY_S = 300.0
#: A crawl that failed outright is tried again this soon, not a week later.
RETRY_FAILED_AFTER = timedelta(days=1)
#: A sitemap file is read up to this size (compressed); its pages are fetched one by one.
SITEMAP_MAX_BYTES = 10 * 1024 * 1024
#: Front-matter counts a fetched page carries into its report (`jobs`): what the extraction left out.
COUNT_KEYS = ("hidden_text_removed", "html_comments_removed")

_HTML_EXTS = frozenset({".html", ".htm", ".xhtml", ".shtml"})
_REFUSED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_DOC_TYPES = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
              "application/json": ".json", "text/xml": ".xml", "application/xml": ".xml"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── URLs and paths ─────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """One spelling per page: lowercase scheme and host (IDNA), no default port, no credentials, no
    fragment, dot segments resolved. Raises `ValueError` for anything that is not an http(s) URL."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Not an http(s) URL: {url[:200]!r}")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError):
        raise ValueError(f"Not a valid web address: {url[:200]!r}") from None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, {"http": 80, "https": 443}[scheme]) else f"{host}:{port}"
    return urlunsplit((scheme, netloc, _dot_segments(parts.path), parts.query, ""))


def _dot_segments(path: str) -> str:
    if path in ("", "/"):
        return "/"
    trailing = path.endswith("/")
    norm = posixpath.normpath(path)
    norm = "/" + norm.lstrip("/") if norm not in (".", "/") else "/"
    return norm + "/" if trailing and not norm.endswith("/") else norm


def _host_dir(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").strip("[]").replace(":", "_")
    return _safe(f"{host}_{parts.port}" if parts.port else host)


def _safe(segment: str, suffix: str = "") -> str:
    """One path segment by the Vault's rules (`ingest._segment`), shortened with a hash past 255 bytes."""
    seg = unicodedata.normalize("NFC", segment)
    seg = "".join("_" if ch in "/\\[]" or unicodedata.category(ch) in _REFUSED_CATEGORIES else ch for ch in seg)
    seg = seg.strip()
    if not seg or seg in (".", ".."):
        seg = "_"
    if seg.startswith("."):
        seg = "_" + seg
    budget = 255 - len(suffix.encode("utf-8"))
    if len(seg.encode("utf-8")) > budget:
        digest = hashlib.sha1(seg.encode("utf-8")).hexdigest()[:8]  # noqa: S324 — a name, not a secret
        seg = seg.encode("utf-8")[:budget - 9].decode("utf-8", errors="ignore").rstrip() + "~" + digest
    return seg + suffix


def page_rel(url: str, *, ext: str = ".md") -> str:
    """Where a page lives inside its base's folder: `_web/<host>/<path><ext>`.

    `/` and `/a/` are `index`; `.html`-like endings give way to `ext`; a query adds `__q<8 hex>` so
    `?page=2` is its own file. A path past the Vault's 1024 bytes is kept under `_long/<hash>`."""
    parts = urlsplit(url)
    segments = [s for s in unquote(parts.path or "/").split("/")][1:] or [""]
    if segments[-1] == "":
        segments[-1] = "index"
    stem, own_ext = posixpath.splitext(segments[-1])
    if own_ext.lower() in _HTML_EXTS or own_ext.lower() == ext.lower():
        segments[-1] = stem or "index"
    if parts.query:
        segments[-1] += "__q" + hashlib.sha1(parts.query.encode("utf-8")).hexdigest()[:8]  # noqa: S324
    rel = "/".join([WEB_DIR, _host_dir(url), *(_safe(s) for s in segments[:-1]), _safe(segments[-1], ext)])
    if len(rel.encode("utf-8")) > 800:  # with the base's folder, under the 1024-byte path limit
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        rel = f"{WEB_DIR}/{_host_dir(url)}/_long/{digest}{ext}"
    return rel


def document_ext(url: str, media: str) -> str | None:
    """The extension a non-HTML answer is kept at: the URL's own when A2 reads it, else its type's."""
    from core.knowledge.parse import supports

    own = posixpath.splitext(urlsplit(url).path)[1].lower()
    if own and supports(f"x{own}"):
        return own
    guessed = _DOC_TYPES.get(media) or (mimetypes.guess_extension(media) if media else None)
    return guessed if guessed and supports(f"x{guessed}") else None


def _scope_dir(root: str) -> str:
    path = urlsplit(root).path or "/"
    return path if path.endswith("/") else posixpath.dirname(path).rstrip("/") + "/"


def _same_host(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.hostname, pa.port or {"http": 80, "https": 443}.get(pa.scheme)) == (
        pb.hostname, pb.port or {"http": 80, "https": 443}.get(pb.scheme)) or (
        pa.hostname == pb.hostname and {pa.port, pb.port} <= {None, 80, 443})


def _globbed(source: WebSource, url: str) -> bool:
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    def hit(pattern: str) -> bool:
        return fnmatch.fnmatchcase(url if "://" in pattern else target, pattern)

    if source.include and not any(hit(p) for p in source.include):
        return False
    return not any(hit(p) for p in source.exclude)


def in_scope(source: WebSource, url: str) -> bool:
    """Whether a `site` crawl from `source.url` follows `url`: same host, under the start URL's
    directory, through the source's include/exclude globs. A sitemap's pages need the same host only."""
    if not _same_host(source.url, url):
        return False
    if source.kind == "site" and not (urlsplit(url).path or "/").startswith(_scope_dir(source.url)):
        return False
    return _globbed(source, url)


# ── what a fetched page becomes ────────────────────────────────────────────────

def _front_value(value: str) -> str:
    return " ".join(str(value).split())[:300]


def page_file(markdown: str, *, url: str, title: str | None, published: str | None,
              hidden: int = 0, comments: int = 0) -> str:
    """The Markdown file a fetched HTML page is kept as. The front matter is stable for the same page
    (never a fetch time), so an unchanged page writes the same bytes and queues nothing."""
    lines = ["---"]
    if title:
        lines.append(f"title: {_front_value(title)}")
    if published:
        lines.append(f"date: {published[:10]}")
    lines.append(f"source_url: {url}")
    if hidden:
        lines.append(f"hidden_text_removed: {hidden}")
    if comments:
        lines.append(f"html_comments_removed: {comments}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + markdown.strip() + "\n"


def fetched_counts(text: str) -> dict[str, int]:
    """What a fetched page's front matter says its extraction left out (for the report's sanitize stage)."""
    out: dict[str, int] = {}
    if not text.startswith("---"):
        return out
    for line in text.split("\n", 40)[1:40]:
        if line.strip() in ("---", "..."):
            break
        key, _, value = line.partition(":")
        if key.strip() in COUNT_KEYS and value.strip().isdigit():
            out[key.strip()] = int(value.strip())
    return out


def _render(body: bytes, content_type: str | None, url: str, header_robots: str | None):
    """(links, extract) for an HTML answer: read twice — for the crawl, then for the content."""
    from core.knowledge.fetch import decode_html, page_links
    from core.knowledge.parse.html import html_to_markdown

    text = decode_html(body, content_type)
    return page_links(text, url, header_robots=header_robots), html_to_markdown(text, url=url)


# ── state (engine `collections`, one row per source) ──────────────────────────

def _state_name(source_id: str) -> str:
    return f"{STATE_PREFIX}{source_id}"


async def load_state(source_id: str) -> dict[str, Any]:
    from core.store import collections

    found = await collections.load_one(_state_name(source_id), {})
    return dict(found or {})


async def save_state(source_id: str, state: dict[str, Any]) -> None:
    from core.store import collections

    await collections.save_one(_state_name(source_id), state)


async def drop_state(source_id: str) -> None:
    from core.store import collections

    await collections.save(_state_name(source_id), [])


async def _held_by_others(kb: KnowledgeBase, source_id: str) -> tuple[set[str], set[str]]:
    """The pages and outcome rows the base's other sources hold (relative paths)."""
    pages: set[str] = set()
    outcomes: set[str] = set()
    for other in kb.web.sources:
        if other.id != source_id:
            state = await load_state(other.id)
            pages |= set(state.get("pages") or [])
            outcomes |= set(state.get("outcomes") or [])
    return pages, outcomes


# ── the crawls this process runs ───────────────────────────────────────────────

_tasks: dict[tuple[str, str], asyncio.Task] = {}
_slots: dict[int, asyncio.Semaphore] = {}


def _key(source_id: str) -> tuple[str, str]:
    from core.tenancy import get_tenant

    return get_tenant(), source_id


def is_running(source_id: str) -> bool:
    task = _tasks.get(_key(source_id))
    return task is not None and not task.done()


def _slot() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_CRAWLS)
    return slot


def _start(kb_id: str, source_id: str, crawl_id: str) -> bool:
    """Run `run_crawl` in the background (OSS). False when the source is already being crawled."""
    from core.background import spawn_background

    key = _key(source_id)
    if is_running(source_id):
        return False

    async def run() -> None:
        try:
            async with _slot():
                await run_crawl(kb_id, source_id, crawl_id)
        finally:
            if _tasks.get(key) is task:
                _tasks.pop(key, None)

    task = spawn_background(run(), name=f"knowledge-crawl-{source_id}")
    _tasks[key] = task
    return True


async def cancel(source_id: str) -> None:
    """Stop a source's crawl, if this process runs one, and wait for it to stop."""
    task = _tasks.get(_key(source_id))
    if task is None or task.done():
        return
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001 — it is stopping either way
        pass


async def _queue(kb: KnowledgeBase, source: WebSource, crawl_id: str, **request: Any) -> bool:
    state = await load_state(source.id)
    state.update(source_id=source.id, kb_id=kb.id, url=source.url, kind=source.kind, crawl_id=crawl_id,
                 status="queued", queued_at=_now().isoformat(), **request)
    await save_state(source.id, state)
    return _start(kb.id, source.id, crawl_id)


# ── sources: added, removed, listed ────────────────────────────────────────────

async def add_urls(kb: KnowledgeBase, body: UrlIngestRequest) -> CrawlAccepted:
    """Record each URL as a source of `body.kind` (an existing one is reused: that is "crawl again
    now") and start their crawls. Raises `ValueError` (a 422) for a bad URL or past `MAX_SOURCES`."""
    from core.knowledge import bases
    from core.knowledge.api_models import CrawlAccepted, KnowledgeBaseUpdate
    from core.knowledge.errors import StaleRevision
    from core.knowledge.kb_settings import WebSource

    urls = list(dict.fromkeys(normalize_url(u) for u in body.urls))
    chosen: list[WebSource] = []
    for attempt in range(3):
        kb = await bases.require_base(kb.id)
        existing = {(s.url, s.kind): s for s in kb.web.sources}
        fresh: dict[tuple[str, str], WebSource] = {}
        chosen = []
        for url in urls:
            key = (url, body.kind)
            source = existing.get(key) or fresh.get(key)
            if source is None:
                source = fresh[key] = WebSource(id=new_web_source_id(), url=url, kind=body.kind)
            chosen.append(source)
        if not fresh:
            break
        if len(kb.web.sources) + len(fresh) > MAX_SOURCES:
            raise ValueError(f"A knowledge base holds at most {MAX_SOURCES} web sources; it has "
                             f"{len(kb.web.sources)}. Add a sitemap or a site crawl instead of many single pages.")
        web = kb.web.model_copy(update={"sources": [*kb.web.sources, *fresh.values()]})
        try:
            await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=kb.revision, web=web))
            break
        except StaleRevision:
            if attempt == 2:
                raise
    crawl_id = new_id(CRAWL_PREFIX)
    request = {"metadata": dict(body.metadata or {}), "max_depth": body.max_depth, "max_pages": body.max_pages}
    for source in chosen:
        await _queue(kb, source, crawl_id, **request)
    return CrawlAccepted(crawl_id=crawl_id, accepted=len(chosen))


async def sources_changed(before: KnowledgeBase, after: KnowledgeBase) -> None:
    """A PATCH changed `web.sources`: a removed source's crawl stops and its pages go; an added one
    is crawled."""
    kept = {s.id for s in after.web.sources}
    had = {s.id for s in before.web.sources}
    for source in before.web.sources:
        if source.id not in kept:
            await forget_source(after, source.id)
    added = [s for s in after.web.sources if s.id not in had]
    if added and after.status == "active":
        crawl_id = new_id(CRAWL_PREFIX)
        for source in added:
            await _queue(after, source, crawl_id)


async def forget_source(kb: KnowledgeBase, source_id: str) -> int:
    """Stop the source's crawl and delete what it alone holds. `kb` no longer lists it. Returns how
    many documents were deleted."""
    from core.knowledge import documents, ingest

    await cancel(source_id)
    state = await load_state(source_id)
    pages, outcomes = await _held_by_others(kb, source_id)
    gone = 0
    for rel in (set(state.get("pages") or []) - pages) | (set(state.get("outcomes") or []) - outcomes):
        row = await documents.get_by_path(kb.id, f"{kb.folder}/{rel}")
        if row is not None and row.status != "deleting":
            await ingest.delete_document(row)
            gone += 1
    await drop_state(source_id)
    return gone


async def forget_base(kb: KnowledgeBase) -> None:
    """A deleted base: its crawls stop and their state goes (its documents go with the base)."""
    for source in kb.web.sources:
        await cancel(source.id)
        await drop_state(source.id)


def _next_due(kb: KnowledgeBase, state: dict[str, Any]) -> datetime | None:
    finished = state.get("finished_at")
    if not finished:
        return None
    at = datetime.fromisoformat(finished)
    if state.get("status") == "failed":
        return at + RETRY_FAILED_AFTER
    return at + timedelta(days=kb.web.recrawl_days) if kb.web.recrawl_days > 0 else None


async def crawl_states(kb: KnowledgeBase) -> list[WebCrawlState]:
    """Each source of `kb` with its latest crawl, in the base's order."""
    from core.knowledge.api_models import CrawlCounts, WebCrawlState

    out = []
    for source in kb.web.sources:
        state = await load_state(source.id)
        status = state.get("status") or "idle"
        if status in ("queued", "running") and not is_running(source.id):
            status = "interrupted"  # the process that ran it stopped; the next due pass runs it again
        due = _next_due(kb, state)
        out.append(WebCrawlState(
            source_id=source.id, url=source.url, kind=source.kind, status=status, crawl_id=state.get("crawl_id"),
            started_at=state.get("started_at"), finished_at=state.get("finished_at"),
            pages=len(state.get("pages") or []), counts=CrawlCounts(**(state.get("counts") or {})),
            next_due_at=due.isoformat() if due else None, last_error=state.get("last_error")))
    return out


async def start_due_crawls(*, force: bool = False) -> int:
    """OSS: start every source whose re-crawl is due, never finished, or was interrupted by a restart.
    At most every `DUE_CHECK_EVERY_S` per tenant unless `force`. Returns how many started."""
    from core.knowledge import bases
    from core.tenancy import get_tenant

    tenant = get_tenant()
    clock = time.monotonic()
    if not force and clock - _last_due.get(tenant, float("-inf")) < DUE_CHECK_EVERY_S:
        return 0
    _last_due[tenant] = clock
    now = _now()
    started = 0
    for kb in await bases.list_bases():
        if kb.status != "active":
            continue
        for source in kb.web.sources:
            if is_running(source.id):
                continue
            state = await load_state(source.id)
            due = _next_due(kb, state)
            if not state.get("finished_at") or state.get("status") in ("queued", "running") or (
                    due is not None and now >= due):
                if await _queue(kb, source, new_id(CRAWL_PREFIX)):
                    started += 1
    return started


_last_due: dict[str, float] = {}


# ── a crawl ────────────────────────────────────────────────────────────────────

class _Stop(Exception):
    """The base went away, was disabled, or Knowledge was switched off: the crawl stops."""


@dataclass
class _Crawl:
    kb: KnowledgeBase
    source: WebSource
    fetcher: Fetcher
    metadata: dict[str, Any]
    doc_cap: int
    by_url: dict[str, Any]
    by_path: dict[str, Any]
    named: set[str] = field(default_factory=set)
    reached: set[str] = field(default_factory=set)    # relative paths this crawl holds
    outcomes: set[str] = field(default_factory=set)   # relative paths of outcome rows it recorded
    seen: set[str] = field(default_factory=set)       # normalized URLs taken
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ("fetched", "unchanged", "written", "deleted", "blocked", "failed", "skipped", "not_followed"), 0))
    capped: bool = False
    last_error: str | None = None
    checked_at: float = 0.0

    def rel(self, path: str) -> str:
        return path[len(self.kb.folder) + 1:] if path.startswith(self.kb.folder + "/") else path

    def keep(self, row) -> None:
        if row is not None and row.sha256:
            self.reached.add(self.rel(row.path))

    def note(self, message: str) -> None:
        self.last_error = message[:2000]


async def _web_rows(kb: KnowledgeBase) -> tuple[dict[str, Any], dict[str, Any]]:
    from core.knowledge import documents

    rows = await documents.list_for(kb.id, path_prefix=f"{kb.folder}/{WEB_DIR}")
    by_url = {}
    for row in rows:
        if row.source_url and row.status != "deleting":
            try:
                by_url[normalize_url(row.source_url)] = row
            except ValueError:
                continue
    return by_url, {row.path: row for row in rows}


def _limits(kb: KnowledgeBase, state: dict[str, Any]) -> tuple[int, int, float, int]:
    from core.knowledge.config import knowledge_settings

    settings = knowledge_settings()
    depth = min(state.get("max_depth") if state.get("max_depth") is not None else kb.web.max_depth,
                settings.crawl_max_depth)
    pages = min(state.get("max_pages") or kb.web.max_pages, settings.crawl_max_pages)
    rps = min(kb.web.rps_per_host, settings.crawl_rps_per_host)
    doc_cap = min(settings.max_file_mb, 25) * 1024 * 1024
    return max(depth, 0), max(pages, 1), rps, doc_cap


async def run_crawl(kb_id: str, source_id: str, crawl_id: str) -> dict[str, Any]:
    """Crawl one source and record what happened in its state. The entrypoint OSS runs in the
    background and cloud's crawl job calls. Never raises for a crawl's own failures."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled
    from core.knowledge.fetch import Fetcher

    kb = await bases.get_base(kb_id)
    source = next((s for s in kb.web.sources if s.id == source_id), None) if kb is not None else None
    if kb is None or source is None:
        return {}
    state = await load_state(source_id)
    if not knowledge_enabled() or kb.status != "active":
        state.update(status="cancelled", finished_at=_now().isoformat(),
                     last_error="The knowledge base is disabled, or Knowledge is off.")
        await save_state(source_id, state)
        return state
    max_depth, max_pages, rps, doc_cap = _limits(kb, state)
    state.update(source_id=source_id, kb_id=kb.id, url=source.url, kind=source.kind, crawl_id=crawl_id,
                 status="running", started_at=_now().isoformat(), finished_at=None, last_error=None)
    await save_state(source_id, state)
    by_url, by_path = await _web_rows(kb)
    status = "done"
    async with Fetcher(rps=rps) as fetcher:
        c = _Crawl(kb=kb, source=source, fetcher=fetcher, metadata=dict(state.get("metadata") or {}),
                   doc_cap=doc_cap, by_url=by_url, by_path=by_path)
        try:
            if source.kind == "page":
                c.named.add(source.url)
                c.seen.add(source.url)
                await _page(c, source.url, links_needed=False)
            elif source.kind == "sitemap":
                await _sitemap(c, max_pages)
            else:
                await _site(c, max_depth, max_pages)
        except _Stop as exc:
            status = "cancelled"
            c.note(str(exc))
        except asyncio.CancelledError:
            status = "cancelled"
            c.note("The crawl was stopped.")
            await asyncio.shield(_finish(c, state, status, complete=False))
            raise
        except _CrawlFailed as exc:
            status = "failed"
            c.note(str(exc))
        except Exception as exc:  # noqa: BLE001 — a crawl's bug ends the crawl, never the server
            _log.warning(f"knowledge crawl of {source_id} failed: {exc!r}")
            status = "failed"
            c.note(f"{type(exc).__name__}: {exc}")
        if status == "done":
            if c.capped:
                status = "capped"
            elif c.counts["fetched"] == 0 and c.counts["failed"]:
                status = "failed"
        complete = status == "done" and c.counts["failed"] == 0
        return await _finish(c, state, status, complete=complete)


async def _finish(c: _Crawl, state: dict[str, Any], status: str, *, complete: bool) -> dict[str, Any]:
    """Delete what a complete crawl no longer reached; record the crawl."""
    from core.knowledge import ingest

    previous = set(state.get("pages") or [])
    previous_outcomes = set(state.get("outcomes") or [])
    if complete:
        try:
            pages, outcomes = await _held_by_others(c.kb, c.source.id)
            for rel in sorted((previous - c.reached - pages) | (previous_outcomes - c.outcomes - outcomes)):
                row = c.by_path.get(f"{c.kb.folder}/{rel}")
                if row is not None and row.status != "deleting" and (rel in previous or not row.sha256):
                    await ingest.delete_document(row)
                    c.counts["deleted"] += 1
        except Exception as exc:  # noqa: BLE001 — what is not deleted now is deleted by the next crawl
            _log.warning(f"knowledge crawl of {c.source.id}: stale pages not deleted: {exc!r}")
            complete = False
    state.update(status=status, finished_at=_now().isoformat(), counts=c.counts, last_error=c.last_error,
                 pages=sorted(c.reached if complete else previous | c.reached),
                 outcomes=sorted(c.outcomes if complete else previous_outcomes | c.outcomes))
    await save_state(c.source.id, state)
    return state


class _CrawlFailed(Exception):
    """The crawl could not start: its sitemap or start URL could not be read."""


async def _check(c: _Crawl) -> None:
    """Stop when the base is gone or disabled, or Knowledge was switched off (checked every few seconds)."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled

    clock = time.monotonic()
    if clock - c.checked_at < 5.0:
        return
    c.checked_at = clock
    kb = await bases.get_base(c.kb.id)
    if kb is None or kb.status != "active" or not knowledge_enabled():
        raise _Stop("The knowledge base was deleted or disabled, or Knowledge was switched off.")
    if not any(s.id == c.source.id for s in kb.web.sources):
        raise _Stop("The source was removed.")


async def _site(c: _Crawl, max_depth: int, max_pages: int) -> None:
    from core.knowledge.fetch import RobotsDisallowed  # noqa: F401 — named in the docstring's terms

    root = c.source.url
    c.named.add(root)
    c.seen.add(root)
    frontier: deque[tuple[str, int]] = deque([(root, 0)])
    taken = 0
    while frontier:
        if taken >= max_pages:
            c.capped = True
            c.counts["not_followed"] += len(frontier)
            break
        url, depth = frontier.popleft()
        taken += 1
        links = await _page(c, url, links_needed=depth < max_depth)
        if links is None:
            continue
        for link in links.links:
            try:
                found = normalize_url(link)
            except ValueError:
                continue
            if found in c.seen:
                continue
            c.seen.add(found)
            if depth + 1 > max_depth or not in_scope(c.source, found):
                c.counts["not_followed"] += 1
                continue
            robots = await c.fetcher.robots(found)
            if not robots.unreachable_ and not robots.allows(found):
                c.counts["blocked"] += 1  # a link robots.txt disallows is never requested, and makes no row
                continue
            frontier.append((found, depth + 1))


async def _sitemap(c: _Crawl, max_pages: int) -> None:
    from core import safe_fetch
    from core.knowledge import sitemaps

    pending: deque[tuple[str, int]] = deque([(c.source.url, 0)])
    read: set[str] = set()
    urls: list[str] = []
    while pending and len(urls) < max_pages:
        await _check(c)
        url, depth = pending.popleft()
        if url in read:
            continue
        read.add(url)
        root = depth == 0
        try:
            got = await c.fetcher.get_resource(url, max_bytes=SITEMAP_MAX_BYTES)
            if not 200 <= got.status < 300:
                raise sitemaps.SitemapError(f"The sitemap {url} answered {got.status}.")
            parsed = sitemaps.parse(got.body)
        except (safe_fetch.FetchRefused, sitemaps.SitemapError) as exc:
            if root:
                raise _CrawlFailed(f"The sitemap could not be read: {exc}") from None
            c.counts["failed"] += 1
            c.note(f"A sitemap it lists could not be read: {exc}")
            continue
        for child in parsed.sitemaps:
            if depth + 1 <= sitemaps.MAX_DEPTH and _same_host(c.source.url, child):
                pending.append((normalize_url(child), depth + 1))
        for entry in parsed.urls:
            try:
                found = normalize_url(entry.url)
            except ValueError:
                continue
            if found in c.seen:
                continue
            c.seen.add(found)
            if not in_scope(c.source, found):
                c.counts["not_followed"] += 1
            elif len(urls) >= max_pages:
                c.capped = True
                c.counts["not_followed"] += 1
            else:
                urls.append(found)
        if parsed.truncated:
            c.capped = True
    if pending and len(urls) >= max_pages:
        c.capped = True
    for url in urls:
        await _page(c, url, links_needed=False)


async def _exists(path: str) -> bool:
    from core.storage import get_blob_store

    return await asyncio.to_thread(get_blob_store().exists, f"vault/{path}")


async def _page(c: _Crawl, url: str, *, links_needed: bool) -> PageLinks | None:
    """Fetch one page and keep, refresh or delete what the base holds for it. Returns its links when
    it was HTML (for a site crawl to follow), else None."""
    from core import safe_fetch
    from core.knowledge.fetch import (
        HTML_MAX_BYTES, HTML_TYPES, RobotsDisallowed, RobotsUnreachable, media_type, validator_of,
    )

    await _check(c)
    named = url in c.named
    held = c.by_url.get(url)
    held = held if held is not None and held.sha256 and held.status != "deleting" else None
    validator = None
    if held is not None and held.etag and await _exists(held.path):
        # A page whose links a site crawl needs is fetched whole: a 304 has no links to follow.
        validator = None if links_needed and held.path.endswith(".md") else held.etag
    try:
        got = await c.fetcher.get(url, validator=validator, max_bytes=c.doc_cap)
    except RobotsDisallowed as exc:
        c.counts["blocked"] += 1
        if held is not None:  # the site no longer allows it: what was fetched before goes
            await _delete(c, held)
        elif named:
            await _outcome(c, url, "blocked", "robots_disallowed", str(exc))
        return None
    except safe_fetch.FetchBlocked as exc:
        c.counts["blocked"] += 1
        c.keep(held)
        if held is None and named:
            await _outcome(c, url, "blocked", "egress_blocked", str(exc))
        return None
    except safe_fetch.FetchTooLarge as exc:
        c.counts["skipped"] += 1
        c.keep(held)
        if held is None and named:
            await _outcome(c, url, "skipped", "over_size_cap", str(exc))
        return None
    except (RobotsUnreachable, safe_fetch.FetchRefused) as exc:
        c.counts["failed"] += 1
        c.note(str(exc))
        c.keep(held)
        if held is None and named:
            await _outcome(c, url, "failed", "fetch_failed", str(exc))
        return None

    c.counts["fetched"] += 1
    if got.status == 304:
        c.counts["unchanged"] += 1
        c.keep(held)
        return None
    if got.status in (404, 410):
        if held is not None:
            await _delete(c, held)
        else:
            c.counts["skipped"] += 1
            if named:
                await _outcome(c, url, "failed", "fetch_failed", f"The page is gone: the server answered {got.status}.",
                               http_status=got.status)
        return None
    if not 200 <= got.status < 300:
        c.counts["failed"] += 1
        c.note(f"{url} answered {got.status}.")
        c.keep(held)
        if held is None and named:
            await _outcome(c, url, "failed", "fetch_failed", f"The server answered {got.status}.",
                           http_status=got.status)
        return None

    final = normalize_url(got.url)
    if final != url:
        if final in c.seen and final not in c.named:
            c.keep(held)
            return None
        c.seen.add(final)
        if c.source.kind != "page" and not in_scope(c.source, final):
            c.counts["not_followed"] += 1  # redirected out of the source's scope
            c.keep(held)
            return None
        held = held or c.by_url.get(final)
    media = media_type(got.headers)
    sniff = got.body[:512].lstrip().lower()
    if media in HTML_TYPES or (not media and sniff.startswith((b"<!doctype html", b"<html"))):
        if len(got.body) > HTML_MAX_BYTES:
            c.counts["skipped"] += 1
            c.keep(held)
            if held is None and named:
                await _outcome(c, url, "skipped", "over_size_cap", "The page is larger than the 5 MB read for a page.")
            return None
        links, extract = await asyncio.to_thread(_render, got.body, got.headers.get("content-type"), final,
                                                 got.headers.get("x-robots-tag"))
        if links.canonical and links.canonical != final and _same_host(final, links.canonical):
            try:
                canonical = normalize_url(links.canonical)
            except ValueError:
                canonical = final
            if canonical != final and (c.source.kind == "page" or in_scope(c.source, canonical)):
                if canonical in c.seen and canonical != url:
                    return links  # the same page, already kept under its canonical address
                c.seen.add(canonical)
                final = canonical
        if links.noindex:
            c.counts["skipped"] += 1
            if held is not None:
                await _delete(c, held)
            elif named:
                await _outcome(c, url, "skipped", "excluded", "The page asks not to be indexed (noindex).")
            return links
        if not extract.markdown.strip():
            c.counts["skipped"] += 1
            c.keep(held)
            if held is None and named:
                await _outcome(c, url, "skipped", "empty_document",
                               "The page has no readable text; it may need JavaScript to render, which is not read yet.")
            return links
        data = page_file(extract.markdown, url=final, title=extract.title, published=extract.published,
                         hidden=extract.hidden_removed, comments=extract.comments_removed).encode("utf-8")
        await _write(c, page_rel(final), final, data, validator_of(got.headers), "text/markdown")
        return links
    ext = document_ext(final, media)
    if ext is None:
        c.counts["skipped"] += 1
        c.keep(held)
        if held is None and named:
            await _outcome(c, url, "skipped", "unsupported_format",
                           f"The address answered {media or 'an unknown type'}, which a knowledge base does not read.")
        return None
    await _write(c, page_rel(final, ext=ext), final, got.body, validator_of(got.headers), media or None)
    return None


async def _write(c: _Crawl, rel: str, url: str, data: bytes, validator: str | None, mime: str | None) -> None:
    from core.knowledge import documents, sync
    from core.storage import get_blob_store
    from core.vault_events import VaultChange

    path = f"{c.kb.folder}/{rel}"
    c.reached.add(rel)
    sha = hashlib.sha256(data).hexdigest()
    existing = c.by_path.get(path)
    if (existing is not None and existing.sha256 == sha and existing.status != "deleting"
            and (existing.meta or {}) == c.metadata and await _exists(path)):
        c.counts["unchanged"] += 1
        if validator and existing.etag != validator:
            await documents.set_columns(existing.id, etag=validator)
        return
    store = get_blob_store()
    size, digest = await asyncio.to_thread(store.put_stream, f"vault/{path}", iter((data,)), content_type=mime)
    rows = await sync.on_vault_change(VaultChange(path=path, op="write", sha256=digest, size=size, source="fetch"),
                                      metadata=c.metadata or None, source_url=url)
    for row in rows:
        if validator:
            await documents.set_columns(row.id, etag=validator)
        c.by_path[row.path] = row
        c.by_url[url] = row
    c.counts["written"] += 1


async def _delete(c: _Crawl, row) -> None:
    from core.knowledge import ingest

    await ingest.delete_document(row)
    c.counts["deleted"] += 1
    c.by_path.pop(row.path, None)
    c.reached.discard(c.rel(row.path))


async def _outcome(c: _Crawl, url: str, status: str, reason: str, message: str, *,
                   http_status: int | None = None) -> None:
    """A URL a person named, refused or failed: a document that says so, with no file."""
    rel = page_rel(url, ext=document_ext(url, "") or ".md")
    c.outcomes.add(rel)
    row = await record_outcome(c.kb, f"{c.kb.folder}/{rel}", url, status, reason, message,
                               metadata=c.metadata, http_status=http_status, existing=c.by_path.get(
                                   f"{c.kb.folder}/{rel}"))
    if row is not None:
        c.by_path[row.path] = row


async def record_outcome(kb: KnowledgeBase, path: str, url: str, status: str, reason: str, message: str, *,
                         metadata: dict | None = None, http_status: int | None = None,
                         existing: Any = None) -> KnowledgeDocumentDB | None:
    """Record a fetch's refusal or failure as a document with no file (`sha256` null): a new one is
    queued then moved to `status` (a new document starts `queued`, status.py), with a report whose
    `fetch` stage says why. Never replaces a document that has content; an unchanged outcome is left
    alone, so it is not announced again at every crawl."""
    from core.knowledge import documents
    from core.knowledge.report import ReportBuilder, retry_hint
    from core.knowledge.status import ErrorInfo, StageReport

    existing = existing if existing is not None else await documents.get_by_path(kb.id, path)
    if existing is not None and (existing.sha256 or existing.status == "deleting"):
        return None
    if existing is not None and existing.status == status and existing.status_reason == reason:
        return existing
    row, _ = await documents.upsert_for_path(kb, path, source="web", sha256=None, size=None, source_url=url,
                                             meta=metadata or None)
    builder = ReportBuilder(doc_id=row.id, path=row.path, attempt=(row.attempts or 0) + 1)
    detail: dict[str, Any] = {"url": url}
    if http_status is not None:
        detail["http_status"] = http_status
    builder.add(StageReport(stage="fetch", status="skipped" if status == "skipped" else "failed", detail=detail,
                            error=ErrorInfo(code=reason, message=message[:2000],
                                            retryable=retry_hint(reason).allowed)))
    report = builder.finish(status, reason)
    return await documents.transition(row.id, status, reason=reason, error=message[:2000], previous=row.status,
                                      report=report.model_dump(mode="json"))


# ── Retry on a page that never had a file ──────────────────────────────────────

async def refetch(row: KnowledgeDocumentDB) -> KnowledgeDocumentDB | None:
    """Fetch a refused or failed page again (Retry on a document with no file). Returns the document,
    queued, or None when it is not resting. The fetch runs in the background."""
    from core.background import spawn_background
    from core.knowledge import bases, documents

    kb = await bases.get_base(row.kb_id)
    if kb is None or kb.status != "active":
        raise ValueError("This knowledge base is disabled; switch it on to fetch the page again.")
    again = await documents.transition(row.id, "queued", previous=row.status)
    if again is None:
        return None
    spawn_background(_refetch(kb.id, again.id), name=f"knowledge-refetch-{again.id}")
    return again


async def _refetch(kb_id: str, doc_id: str) -> None:
    from core.knowledge import bases, documents
    from core.knowledge.fetch import Fetcher
    from core.knowledge.kb_settings import WebSource

    kb = await bases.get_base(kb_id)
    row = await documents.get(doc_id)
    if kb is None or row is None or not row.source_url:
        return
    url = normalize_url(row.source_url)
    rel = row.path[len(kb.folder) + 1:]
    owner = None
    for source in kb.web.sources:
        state = await load_state(source.id)
        if rel in (state.get("outcomes") or []) or rel in (state.get("pages") or []):
            owner = (source, state)
            break
    source = owner[0] if owner else WebSource(id="kws_000000000000", url=url, kind="page")
    metadata = dict((owner[1].get("metadata") if owner else None) or {})
    _, _, rps, doc_cap = _limits(kb, {})
    by_url, by_path = await _web_rows(kb)
    try:
        async with Fetcher(rps=rps) as fetcher:
            c = _Crawl(kb=kb, source=source, fetcher=fetcher, metadata=metadata, doc_cap=doc_cap, by_url=by_url,
                       by_path=by_path, named={url}, seen={url})
            await _page(c, url, links_needed=False)
    except Exception as exc:  # noqa: BLE001 — the document must not stay queued
        _log.warning(f"knowledge refetch of {doc_id} failed: {exc!r}")
        await record_outcome(kb, row.path, url, "failed", "fetch_failed", f"{type(exc).__name__}: {exc}",
                             metadata=metadata or None)
        return
    current = await documents.get(doc_id)
    if current is not None and current.status == "queued" and not current.sha256:
        # Nothing was written and no outcome recorded (a 304 with no file, a redirect elsewhere): say so.
        await record_outcome(kb, row.path, url, "failed", "fetch_failed",
                             "The page did not come back as something to keep; it is tried again at the next crawl.",
                             metadata=metadata or None)
