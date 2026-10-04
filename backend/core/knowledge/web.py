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
reconcile keeps it (`sync`); queued again — by Retry, or a restart — its ingest
job fetches the page (`fetch_document`). Links a crawl found and could not
fetch are only counted.

**Re-crawl** is due `recrawl_days` after a crawl finished (0: never). OSS's
reconcile loop starts what is due (`start_due_crawls`) — and any crawl a
restart interrupted. Cloud never scans: its tend pass calls `start_due` for one
base, armed at the earliest `next_due_at` (S1c, D181).

**Where a crawl runs.** OSS runs it in this process (`_start`). A deployment
with a `CrawlRunner` (`set_crawl_runner`, cloud's knowledge worker) runs it
elsewhere, so nothing here can ask a task whether it is alive: a crawl is
judged by its row. Queued is live; running is live while its own heartbeat
(a row of its own, `knowledge_web_beat:<kws_id>`, written every
`HEARTBEAT_EVERY_S` with what the crawl has reached so far) is younger than
`RUNNER_STALE`. A crawl runs only while its row still names it and it has not
finished, so a stray or second delivery changes nothing; a worker stopped
mid-crawl leaves it `interrupted`, and the same crawl is armed again, at most
`MAX_ATTEMPTS` times. A removed source's pages are deleted by the runner, not
by the request that removed it.

**A stopped crawl resumes where it stopped** (F353; KL1, D191). Every
`HEARTBEAT_EVERY_S`, when it is interrupted, and at the end of a slice, a crawl
keeps where it got to in a row of its own (`knowledge_web_frontier:<kws_id>`):
its frontier (no more of it than the page cap leaves to take), the addresses it
took, what it reached, its outcomes and counts, a sitemap's list and how far
down it got. The same crawl run again — re-delivered after a deploy, armed again
by a pass, or after an OSS restart — starts from there, and what an earlier run
reached counts as reached, so a complete crawl deletes none of it. Only a crawl
that stopped with no chance to say so (its process killed) counts as an attempt.
**A runner may run a crawl in slices** (`run_crawl(slice_s=)`, cloud: ten
minutes): at the end of one the crawl goes back to `queued`, keeping its place,
and the runner queues it again behind other work. Its time across slices —
fetching, never waiting in line — stops it `capped` at
`knowledge_crawl_max_hours` (0, OSS's default: no limit; cloud: 6, CCR-14).
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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol
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
#: A crawl's heartbeat, apart from its row (S1c): `{crawl_id, at, attempts, reached, outcomes}`.
BEAT_PREFIX = "knowledge_web_beat:"
#: Where a crawl got to, so a stopped one resumes there (F353): see the module docstring. A row of its own, so the
#: heartbeat a tend pass reads stays small.
FRONTIER_PREFIX = "knowledge_web_frontier:"
MAX_SOURCES = 50
#: With a runner: a running crawl whose own heartbeat is older than this was left by a worker that stopped.
RUNNER_STALE = timedelta(minutes=10)
#: With a runner: how often a crawl says it is alive, and what it has reached so far.
HEARTBEAT_EVERY_S = 60.0
#: A crawl started this many times without finishing fails, rather than being started again.
MAX_ATTEMPTS = 3
#: A crawl's time limit (`run_crawl(deadline=)`) is never sooner than this from its start.
MIN_DEADLINE = timedelta(minutes=1)
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
    await collections.save(f"{BEAT_PREFIX}{source_id}", [])
    await collections.save(f"{FRONTIER_PREFIX}{source_id}", [])


async def load_frontier(source_id: str) -> dict[str, Any]:
    from core.store import collections

    return dict(await collections.load_one(f"{FRONTIER_PREFIX}{source_id}", {}) or {})


async def _save_frontier(source_id: str, value: dict[str, Any]) -> None:
    from core.store import collections

    await collections.save_one(f"{FRONTIER_PREFIX}{source_id}", value)


async def _drop_frontier(source_id: str) -> None:
    from core.store import collections

    await collections.save(f"{FRONTIER_PREFIX}{source_id}", [])


async def load_beat(source_id: str) -> dict[str, Any]:
    from core.store import collections

    return dict(await collections.load_one(f"{BEAT_PREFIX}{source_id}", {}) or {})


async def save_beat(source_id: str, beat: dict[str, Any]) -> None:
    from core.store import collections

    await collections.save_one(f"{BEAT_PREFIX}{source_id}", beat)


async def _drop_beat(source_id: str) -> None:
    from core.store import collections

    await collections.save(f"{BEAT_PREFIX}{source_id}", [])


async def _held_by_others(kb: KnowledgeBase, source_id: str) -> tuple[set[str], set[str]]:
    """The pages and outcome rows the base's other sources hold (relative paths) — what their crawls in progress
    reached too (F353)."""
    pages: set[str] = set()
    outcomes: set[str] = set()
    for other in kb.web.sources:
        if other.id != source_id:
            state = await load_state(other.id)
            pages |= set(state.get("pages") or [])
            outcomes |= set(state.get("outcomes") or [])
            kept = await load_frontier(other.id)
            if kept.get("crawl_id") and kept.get("crawl_id") == state.get("crawl_id"):
                pages |= set(kept.get("reached") or [])
                outcomes |= set(kept.get("outcomes") or [])
    return pages, outcomes


# ── the crawls this process runs ───────────────────────────────────────────────

_tasks: dict[tuple[str, str], asyncio.Task] = {}
_slots: dict[int, asyncio.Semaphore] = {}
#: Crawls `cancel` is stopping on a person's word (a removed source or base): those end `cancelled`. Any other cancel
#: is this process stopping, and the crawl is left `interrupted`, to resume (F353).
_cancelling: set[tuple[str, str]] = set()


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
    key = _key(source_id)
    task = _tasks.get(key)
    if task is None or task.done():
        return
    _cancelling.add(key)
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except (asyncio.CancelledError, TimeoutError, Exception):  # noqa: BLE001 — it is stopping either way
        pass
    finally:
        _cancelling.discard(key)


class CrawlRunner(Protocol):
    """Where crawls run when not in this process — cloud's knowledge worker (S1c, D181)."""

    async def start(self, kb_id: str, source_id: str, crawl_id: str) -> None:
        """Have `run_crawl(kb_id, source_id, crawl_id)` run soon, elsewhere. Idempotent per crawl: starting
        one that is already waiting changes nothing. Raising leaves a new crawl `failed`."""

    async def forget(self, kb_id: str, source_id: str, paths: list[str]) -> None:
        """Have `forget_pages(kb, source_id, paths)` run elsewhere: a removed source's pages (paths
        relative to the base's folder), deleted unless another source holds them."""


_runner: CrawlRunner | None = None


def set_crawl_runner(runner: CrawlRunner | None) -> None:
    """Cloud: crawls are run by `runner`. None: by this process (OSS)."""
    global _runner
    _runner = runner


def get_crawl_runner() -> CrawlRunner | None:
    return _runner


#: A crawl's room in the vault: a factory called once per crawl (and per fetched document), whose checker
#: is asked before each page is written with its growth in bytes and answers a refusal, or None to go on.
VaultRoom = Callable[[], Callable[[int], Awaitable[str | None]]]
_vault_room: VaultRoom | None = None


def set_vault_room(factory: VaultRoom | None) -> None:
    """Cloud: a crawl stops at the plan's `vault_mb` (S1c). None: no limit (OSS has none)."""
    global _vault_room
    _vault_room = factory


async def _live(source_id: str, state: dict[str, Any]) -> bool:
    """Whether the source's crawl is waiting or running now. OSS: this process holds its task. With a
    runner: queued, or running with a heartbeat of its own younger than `RUNNER_STALE`."""
    if _runner is None:
        return is_running(source_id)
    status = state.get("status")
    if status == "queued":
        return True
    if status != "running":
        return False
    stamps = [state.get("started_at")]
    beat = await load_beat(source_id)
    if beat.get("crawl_id") and beat.get("crawl_id") == state.get("crawl_id"):
        stamps.append(beat.get("at"))
    seen = [datetime.fromisoformat(s) for s in stamps if s]
    return bool(seen) and _now() - max(seen) < RUNNER_STALE


_queue_locks: dict[tuple[int, str, str], asyncio.Lock] = {}


def _queue_lock(source_id: str) -> asyncio.Lock:
    from core.tenancy import get_tenant

    key = (id(asyncio.get_running_loop()), get_tenant(), source_id)
    lock = _queue_locks.get(key)
    if lock is None:
        lock = _queue_locks[key] = asyncio.Lock()
    return lock


async def _queue(kb: KnowledgeBase, source: WebSource, crawl_id: str, **request: Any) -> bool:
    """Queue a new crawl of `source`, unless one is waiting or running: that one keeps its row (F350 — the
    request used to write `queued` over a running crawl, which then wrote its own row back)."""
    async with _queue_lock(source.id):
        state = await load_state(source.id)
        if await _live(source.id, state):
            return False
        state.update(source_id=source.id, kb_id=kb.id, url=source.url, kind=source.kind, crawl_id=crawl_id,
                     status="queued", queued_at=_now().isoformat(), **request)
        await save_state(source.id, state)
        if _runner is None:
            return _start(kb.id, source.id, crawl_id)
    try:
        await _runner.start(kb.id, source.id, crawl_id)
    except Exception as exc:  # noqa: BLE001 — the crawl says why it did not run
        _log.warning(f"knowledge crawl of {source.id} not queued: {exc!r}")
        state.update(status="failed", finished_at=_now().isoformat(),
                     last_error=f"The crawl could not be queued: {exc}"[:2000])
        await save_state(source.id, state)
        return False
    return True


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
    # Only what this request says changes a source's own metadata and caps: "fetch it again now" (the same
    # URL and kind, nothing else) keeps them, so its pages keep their metadata and are not indexed again.
    request = {name: (dict(value or {}) if name == "metadata" else value)
               for name, value in (("metadata", body.metadata), ("max_depth", body.max_depth),
                                   ("max_pages", body.max_pages)) if name in body.model_fields_set}
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
    many documents were deleted here: none with a runner, which deletes them (`forget_pages`) — and a
    crawl still running stops at its next check and deletes what it reached since its last heartbeat."""
    await cancel(source_id)
    state = await load_state(source_id)
    paths = set(state.get("pages") or []) | set(state.get("outcomes") or [])
    kept = await load_frontier(source_id)
    if kept.get("crawl_id") and kept.get("crawl_id") == state.get("crawl_id"):
        paths |= set(kept.get("reached") or []) | set(kept.get("outcomes") or [])
    if _runner is None:
        gone = await forget_pages(kb, source_id, paths)
        await drop_state(source_id)
        return gone
    beat = await load_beat(source_id)
    if beat.get("crawl_id") and beat.get("crawl_id") == state.get("crawl_id"):
        paths |= set(beat.get("reached") or []) | set(beat.get("outcomes") or [])
    await drop_state(source_id)
    if paths:
        try:
            await _runner.forget(kb.id, source_id, sorted(paths))
        except Exception as exc:  # noqa: BLE001 — the source is already gone from the base
            _log.warning(f"knowledge: the pages of removed source {source_id} were not queued for deletion: {exc!r}")
    return 0


async def forget_pages(kb: KnowledgeBase, source_id: str, paths: Any) -> int:
    """Delete the documents at `paths` (relative to the base's folder) that no other source of `kb` holds.
    `source_id` is the source they came from, which `kb` no longer lists. Returns how many it deleted."""
    from core.knowledge import documents, ingest

    pages, outcomes = await _held_by_others(kb, source_id)
    gone = 0
    for rel in sorted(set(paths) - pages - outcomes):
        row = await documents.get_by_path(kb.id, f"{kb.folder}/{rel}")
        if row is not None and row.status != "deleting":
            await ingest.delete_document(row)
            gone += 1
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
        if status in ("queued", "running") and not await _live(source.id, state):
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
    started = 0
    for kb in await bases.list_bases():
        if kb.status == "active":
            started += await start_due(kb)
    return started


async def start_due(kb: KnowledgeBase) -> int:
    """Start every crawl of `kb` that is due, never finished, or was stopped mid-crawl — cloud's tend pass
    calls this for one base (S1c). With a runner, a crawl that is waiting or was stopped is armed again as
    itself (a no-op while its job exists, and back in place if it was lost), unless it was started
    `MAX_ATTEMPTS` times without finishing; then it fails. Returns how many it started or armed."""
    now = _now()
    started = 0
    for source in kb.web.sources:
        state = await load_state(source.id)
        status = state.get("status")
        if _runner is not None and status in ("queued", "running", "interrupted") and state.get("crawl_id"):
            if status == "running" and await _live(source.id, state):
                continue
            if await _resume(kb, source, state):
                started += 1
            continue
        if _runner is None and is_running(source.id):
            continue
        if (_runner is None and status in ("queued", "running", "interrupted") and state.get("crawl_id")
                and (await load_frontier(source.id)).get("crawl_id") == state["crawl_id"]):
            # OSS stopped mid-crawl (F353): the same crawl, from where it got to.
            if await _resume(kb, source, state):
                started += 1
            continue
        due = _next_due(kb, state)
        if not state.get("finished_at") or status in ("queued", "running", "interrupted") or (
                due is not None and now >= due):
            if await _queue(kb, source, new_id(CRAWL_PREFIX)):
                started += 1
    return started


async def _resume(kb: KnowledgeBase, source: WebSource, state: dict[str, Any]) -> bool:
    """Arm a waiting or stopped crawl again as itself — OSS: start it here — or fail it once it has been started too
    often."""
    beat = await load_beat(source.id)
    kept = await load_frontier(source.id)
    attempts = max(int(beat.get("attempts") or 0) if beat.get("crawl_id") == state["crawl_id"] else 0,
                   int(kept.get("attempts") or 0) if kept.get("crawl_id") == state["crawl_id"] else 0)
    if state.get("status") != "queued" and attempts >= MAX_ATTEMPTS:
        state.update(status="failed", finished_at=_now().isoformat(),
                     last_error=f"The crawl stopped {attempts} times before it finished; it is tried again later.")
        await save_state(source.id, state)
        await _drop_beat(source.id)
        await _drop_frontier(source.id)
        return False
    if _runner is None:
        return _start(kb.id, source.id, state["crawl_id"])
    try:
        await _runner.start(kb.id, source.id, state["crawl_id"])
    except Exception as exc:  # noqa: BLE001 — the next pass arms it again
        _log.warning(f"knowledge crawl of {source.id} not armed again: {exc!r}")
        return False
    return True


_last_due: dict[str, float] = {}


# ── a crawl ────────────────────────────────────────────────────────────────────

class _Stop(Exception):
    """The base went away, was disabled, or Knowledge was switched off: the crawl stops."""


class _Capped(Exception):
    """A limit other than the page cap — the crawl's time, the vault's storage — stopped the crawl:
    `capped`, so it deletes nothing, with the limit named in `last_error` (CCR-11)."""


class _SliceEnd(Exception):
    """The crawl's slice is over (`run_crawl(slice_s=)`): it keeps where it got to and goes back to `queued` (F353)."""


TIME_LIMIT = "Stopped at the crawl's time limit, so nothing was removed; the next crawl starts again."


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
    owned: bool = True  # a source of the base runs it; False: one page fetched for a document no source holds
    crawl_id: str | None = None  # None: `fetch_document`'s one page, which keeps no heartbeat
    attempts: int = 0
    beat_at: float = float("-inf")
    deadline: datetime | None = None
    room: Callable[[int], Awaitable[str | None]] | None = None
    stop_reason: str | None = None  # why a crawl was capped short of its page cap
    # Where it got to, kept so a stopped crawl resumes there (F353; `_checkpoint`).
    frontier: deque = field(default_factory=deque)  # a site's (url, depth) still to take
    taken: int = 0  # a site's pages taken — fetched, refused or failed
    current: tuple[str, int] | None = None  # the page being taken: it goes back at the frontier's head
    dropped: int = 0  # frontier left behind past the page cap: never taken, but the crawl is capped by it
    took: list[str] = field(default_factory=list)  # addresses taken, in order
    landed: set[str] = field(default_factory=set)  # redirect and canonical addresses pages landed on
    sitemap_urls: list[str] | None = None  # a sitemap's pages, once listed
    sitemap_next: int = 0  # how far down them it got
    resumed: bool = False
    max_pages: int = 0
    worked_before: float = 0.0  # seconds fetched by this crawl's earlier slices or runs
    started_mono: float = field(default_factory=time.monotonic)
    max_seconds: float | None = None  # `knowledge_crawl_max_hours`: its time across slices
    slice_end: float | None = None  # monotonic: past it, the next loop head ends the slice
    in_slice: int = 0  # pages taken this slice — a slice always takes one
    saved_at: float = float("-inf")

    def worked(self) -> float:
        return self.worked_before + (time.monotonic() - self.started_mono)

    def see(self, url: str) -> None:
        """An address a page landed on (a redirect's, a canonical): seen, and kept with the frontier."""
        self.seen.add(url)
        self.landed.add(url)

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


async def run_crawl(kb_id: str, source_id: str, crawl_id: str, *, deadline: datetime | None = None,
                    slice_s: float | None = None) -> dict[str, Any]:
    """Crawl one source and record what happened in its state. The entrypoint OSS runs in the
    background and cloud's crawl job calls. Never raises for a crawl's own failures. Past `deadline`
    (a worker's job limit; never sooner than `MIN_DEADLINE`) the crawl stops `capped`, saying so.

    The same crawl run again resumes where it stopped (F353). With `slice_s` it runs for about that long — at
    least one page — and then goes back to `queued`, keeping its place: the state it answers then carries
    `"_more": True`, for the runner to queue it again."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled
    from core.knowledge.fetch import Fetcher, egress_refusal

    kb = await bases.get_base(kb_id)
    source = next((s for s in kb.web.sources if s.id == source_id), None) if kb is not None else None
    if kb is None or source is None:
        return {}
    state = await load_state(source_id)
    if state.get("crawl_id") != crawl_id or state.get("status") not in ("queued", "running", "interrupted"):
        return state  # superseded, finished, or never queued: not this crawl to run (a stray or second delivery)
    if not knowledge_enabled() or kb.status != "active":
        state.update(status="cancelled", finished_at=_now().isoformat(),
                     last_error="The knowledge base is disabled, or Knowledge is off.")
        await save_state(source_id, state)
        return state
    refusal = egress_refusal()
    if refusal is not None:  # cloud with no egress proxy: nothing leaves (Naveen's call 3, D181)
        state.update(status="failed", finished_at=_now().isoformat(), last_error=refusal)
        await save_state(source_id, state)
        if _runner is not None:
            await _drop_beat(source_id)
        return state
    beat = await load_beat(source_id)
    kept = await load_frontier(source_id)
    resumed = kept.get("crawl_id") == crawl_id and kept.get("v") == 1 and kept.get("kind") == source.kind
    attempts = max(int(beat.get("attempts") or 0) if beat.get("crawl_id") == crawl_id else 0,
                   int(kept.get("attempts") or 0) if resumed else 0)
    if not (resumed and state.get("status") in ("queued", "interrupted")):
        attempts += 1  # a new crawl, or one whose process stopped with no chance to say so
    max_depth, max_pages, rps, doc_cap = _limits(kb, state)
    started_at = (state.get("started_at") if resumed else None) or _now().isoformat()
    state.update(source_id=source_id, kb_id=kb.id, url=source.url, kind=source.kind, crawl_id=crawl_id,
                 status="running", started_at=started_at, finished_at=None, last_error=None)
    await save_state(source_id, state)
    if _runner is not None:
        await save_beat(source_id, {"crawl_id": crawl_id, "at": _now().isoformat(), "attempts": attempts})
    by_url, by_path = await _web_rows(kb)
    status = "done"
    from core.knowledge.config import knowledge_settings

    hours = knowledge_settings().crawl_max_hours
    async with Fetcher(rps=rps) as fetcher:
        c = _Crawl(kb=kb, source=source, fetcher=fetcher, metadata=dict(state.get("metadata") or {}),
                   doc_cap=doc_cap, by_url=by_url, by_path=by_path, crawl_id=crawl_id, attempts=attempts,
                   beat_at=time.monotonic(), room=_vault_room() if _vault_room is not None else None,
                   deadline=None if deadline is None else max(deadline, _now() + MIN_DEADLINE),
                   max_pages=max_pages, max_seconds=hours * 3600 if hours and hours > 0 else None,
                   slice_end=None if slice_s is None else time.monotonic() + max(0.0, slice_s))
        if resumed:
            _resume_from(c, kept)
        try:
            if source.kind == "page":
                c.named.add(source.url)
                c.seen.add(source.url)
                await _page(c, source.url, links_needed=False)
            elif source.kind == "sitemap":
                await _sitemap(c, max_pages)
            else:
                await _site(c, max_depth, max_pages)
        except _SliceEnd:
            return await _yield(c, state)
        except _Stop as exc:
            status = "cancelled"
            c.note(str(exc))
        except _Capped as exc:
            c.capped = True
            c.stop_reason = str(exc)
        except asyncio.CancelledError:
            if _runner is not None or _key(source_id) not in _cancelling:
                # Its worker stopped (a deploy, a scale-in) or this process is stopping: the same crawl runs again,
                # from where it got to (F353). A person's cancel (`cancel`) ends it below.
                try:
                    await asyncio.shield(_interrupted(c, state))
                except Exception as exc:  # noqa: BLE001 — its last checkpoint stands
                    _log.warning(f"knowledge crawl of {source_id}: where it stopped was not kept: {exc!r}")
                raise
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


async def _still_mine(c: _Crawl) -> dict[str, Any] | None:
    """The source's row while it still names this crawl, else None — and when the row is gone (the
    source was removed, its base deleted), what this crawl reached that no other source holds is deleted:
    the request that removed it deleted only what the row and the heartbeat had seen."""
    from core.knowledge import bases

    current = await load_state(c.source.id)
    if current.get("crawl_id") == c.crawl_id:
        return current
    if not current:
        kb = await bases.get_base(c.kb.id)
        if kb is not None and c.owned:
            try:
                await forget_pages(kb, c.source.id, c.reached | c.outcomes)
            except Exception as exc:  # noqa: BLE001 — logged; nothing else holds them
                _log.warning(f"knowledge crawl of {c.source.id}: pages of a removed source not deleted: {exc!r}")
    beat = await load_beat(c.source.id)
    if beat.get("crawl_id") == c.crawl_id:
        await _drop_beat(c.source.id)
    if c.crawl_id is not None and (await load_frontier(c.source.id)).get("crawl_id") == c.crawl_id:
        await _drop_frontier(c.source.id)
    return None


async def _interrupted(c: _Crawl, state: dict[str, Any]) -> None:
    """A crawl whose worker stopped: `interrupted`, holding what it reached and where it got to, so the same crawl
    runs again from there."""
    if await _still_mine(c) is None:
        return
    await _checkpoint(c, force=True)
    previous = set(state.get("pages") or [])
    previous_outcomes = set(state.get("outcomes") or [])
    state.update(status="interrupted", counts=c.counts, pages=sorted(previous | c.reached),
                 outcomes=sorted(previous_outcomes | c.outcomes))
    await save_state(c.source.id, state)


async def _finish(c: _Crawl, state: dict[str, Any], status: str, *, complete: bool) -> dict[str, Any]:
    """Delete what a complete crawl no longer reached; record the crawl — unless its row no longer names
    it (removed, or another crawl took over), which it never writes back."""
    from core.knowledge import ingest

    if await _still_mine(c) is None:
        return await load_state(c.source.id)
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
    # A capped crawl names only the limit it stopped at (None: the page cap), never a page that failed on the way.
    last_error = c.stop_reason if status == "capped" else c.last_error
    state.update(status=status, finished_at=_now().isoformat(), counts=c.counts, last_error=last_error,
                 pages=sorted(c.reached if complete else previous | c.reached),
                 outcomes=sorted(c.outcomes if complete else previous_outcomes | c.outcomes))
    await save_state(c.source.id, state)
    if _runner is not None:
        await _drop_beat(c.source.id)
    if c.crawl_id is not None:
        await _drop_frontier(c.source.id)
    return state


async def _yield(c: _Crawl, state: dict[str, Any]) -> dict[str, Any]:
    """The end of a slice: where it got to kept, the source's row `queued` again (live, holding what it reached so
    far), no attempt counted. Answers the row with `"_more": True`."""
    if await _still_mine(c) is None:
        return await load_state(c.source.id)
    await _checkpoint(c, force=True)
    previous = set(state.get("pages") or [])
    previous_outcomes = set(state.get("outcomes") or [])
    state.update(status="queued", queued_at=_now().isoformat(), counts=dict(c.counts),
                 pages=sorted(previous | c.reached), outcomes=sorted(previous_outcomes | c.outcomes))
    await save_state(c.source.id, state)
    if _runner is not None:
        await save_beat(c.source.id, {"crawl_id": c.crawl_id, "at": _now().isoformat(), "attempts": c.attempts,
                                      "reached": sorted(c.reached), "outcomes": sorted(c.outcomes)})
    return {**state, "_more": True}


def _frontier_value(c: _Crawl) -> dict[str, Any]:
    """Where `c` got to: no more of its frontier than its page cap leaves it to take (the rest only counts), the
    page being taken put back at the head."""
    pending = list(c.frontier)
    taken = c.taken
    if c.current is not None:
        pending.insert(0, c.current)
        taken -= 1
    room = max(0, c.max_pages - taken)
    kept = pending[:room]
    seen = set(c.took) | {url for url, _ in kept} | c.landed | set(c.named)
    value: dict[str, Any] = {
        "v": 1, "crawl_id": c.crawl_id, "kind": c.source.kind, "attempts": c.attempts, "at": _now().isoformat(),
        "worked_s": round(c.worked(), 3), "taken": taken, "frontier": [[url, depth] for url, depth in kept],
        "dropped": c.dropped + max(0, len(pending) - room), "took": list(c.took), "seen": sorted(seen),
        "named": sorted(c.named), "reached": sorted(c.reached), "outcomes": sorted(c.outcomes),
        "counts": dict(c.counts), "capped": c.capped, "last_error": c.last_error,
    }
    if c.sitemap_urls is not None:
        value["sitemap"] = {"urls": list(c.sitemap_urls), "next": c.sitemap_next}
    return value


def _resume_from(c: _Crawl, kept: dict[str, Any]) -> None:
    """Seed `c` with where the same crawl got to: what it reached counts as reached, so a complete crawl deletes none
    of it, and it takes up its frontier — or its sitemap's list — where it left off."""
    c.resumed = True
    c.frontier = deque((str(url), int(depth)) for url, depth in kept.get("frontier") or [])
    c.taken = int(kept.get("taken") or 0)
    c.dropped = int(kept.get("dropped") or 0)
    c.took = [str(url) for url in kept.get("took") or []]
    c.seen = set(kept.get("seen") or [])
    c.landed = set(c.seen) - set(c.took) - {url for url, _ in c.frontier}
    c.named = set(kept.get("named") or [])
    c.reached = set(kept.get("reached") or [])
    c.outcomes = set(kept.get("outcomes") or [])
    c.counts.update({k: int(v) for k, v in (kept.get("counts") or {}).items() if k in c.counts})
    c.capped = bool(kept.get("capped"))
    c.last_error = kept.get("last_error")
    c.worked_before = float(kept.get("worked_s") or 0.0)
    sitemap = kept.get("sitemap")
    if isinstance(sitemap, dict):
        c.sitemap_urls = [str(url) for url in sitemap.get("urls") or []]
        c.sitemap_next = int(sitemap.get("next") or 0)


async def _checkpoint(c: _Crawl, *, force: bool = False) -> None:
    """Keep where `c` got to (`FRONTIER_PREFIX`), every `HEARTBEAT_EVERY_S` or now. `fetch_document`'s one page
    keeps nothing."""
    if c.crawl_id is None or not c.owned:
        return
    clock = time.monotonic()
    if not force and clock - c.saved_at < HEARTBEAT_EVERY_S:
        return
    c.saved_at = clock
    await _save_frontier(c.source.id, _frontier_value(c))


def _slice_over(c: _Crawl) -> bool:
    """At a loop's head: the slice has run its time and taken a page at least."""
    return c.slice_end is not None and c.in_slice > 0 and time.monotonic() >= c.slice_end


class _CrawlFailed(Exception):
    """The crawl could not start: its sitemap or start URL could not be read."""


async def _check(c: _Crawl) -> None:
    """Stop when the base is gone or disabled, or Knowledge was switched off (checked every few seconds)."""
    from core.knowledge import bases
    from core.knowledge.config import knowledge_enabled

    if c.deadline is not None and _now() >= c.deadline:
        raise _Capped(TIME_LIMIT)
    if c.max_seconds is not None and c.worked() >= c.max_seconds:
        raise _Capped(TIME_LIMIT)  # its time across slices (F353): fetching, never waiting in line
    await _checkpoint(c)
    await _beat(c)
    clock = time.monotonic()
    if clock - c.checked_at < 5.0:
        return
    c.checked_at = clock
    kb = await bases.get_base(c.kb.id)
    if kb is None or kb.status != "active" or not knowledge_enabled():
        raise _Stop("The knowledge base was deleted or disabled, or Knowledge was switched off.")
    if c.owned and not any(s.id == c.source.id for s in kb.web.sources):
        raise _Stop("The source was removed.")


async def _beat(c: _Crawl) -> None:
    """With a runner, every `HEARTBEAT_EVERY_S`: say this crawl is alive and what it has reached (in a
    row of its own: `_finish` stays the only writer of the source's row). A row that no longer names
    this crawl stops it."""
    if _runner is None or c.crawl_id is None:
        return
    clock = time.monotonic()
    if clock - c.beat_at < HEARTBEAT_EVERY_S:
        return
    c.beat_at = clock
    current = await load_state(c.source.id)
    if current.get("crawl_id") != c.crawl_id:
        raise _Stop("The source was removed, or another crawl of it started.")
    await save_beat(c.source.id, {"crawl_id": c.crawl_id, "at": _now().isoformat(), "attempts": c.attempts,
                                  "reached": sorted(c.reached), "outcomes": sorted(c.outcomes)})


async def _site(c: _Crawl, max_depth: int, max_pages: int) -> None:
    from core.knowledge.fetch import RobotsDisallowed  # noqa: F401 — named in the docstring's terms

    if not c.resumed:
        root = c.source.url
        c.named.add(root)
        c.seen.add(root)
        c.frontier = deque([(root, 0)])
        c.taken = 0
    frontier = c.frontier
    while frontier:
        if c.taken >= max_pages:
            c.capped = True
            c.counts["not_followed"] += len(frontier) + c.dropped
            c.dropped = 0
            break
        if _slice_over(c):
            raise _SliceEnd()
        url, depth = frontier.popleft()
        c.taken += 1
        c.in_slice += 1
        c.current = (url, depth)
        links = await _page(c, url, links_needed=depth < max_depth)
        c.current = None
        c.took.append(url)
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
    if c.dropped:  # resumed with its frontier cut at the cap: what lay past it still caps the crawl
        c.capped = True
        c.counts["not_followed"] += c.dropped
        c.dropped = 0


async def _sitemap(c: _Crawl, max_pages: int) -> None:
    """A sitemap source: its list read (`_list_sitemap`), then its pages fetched in order — from where a stopped
    crawl got to."""
    if c.sitemap_urls is None:
        c.sitemap_urls = await _list_sitemap(c, max_pages)
        c.sitemap_next = 0
    urls = c.sitemap_urls
    while c.sitemap_next < len(urls):
        if _slice_over(c):
            raise _SliceEnd()
        url = urls[c.sitemap_next]
        c.in_slice += 1
        await _page(c, url, links_needed=False)
        c.sitemap_next += 1
        c.took.append(url)


async def _list_sitemap(c: _Crawl, max_pages: int) -> list[str]:
    """The pages a sitemap source lists (its child sitemaps read too), up to the page cap. Quick: read again whole
    when a crawl stopped before it was done."""
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
    return urls


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
        c.see(final)
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
                c.see(canonical)
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
    sha = hashlib.sha256(data).hexdigest()
    existing = c.by_path.get(path)
    if (existing is not None and existing.sha256 == sha and existing.status != "deleting"
            and (existing.meta or {}) == c.metadata and await _exists(path)):
        c.reached.add(rel)
        c.counts["unchanged"] += 1
        if validator and existing.etag != validator:
            await documents.set_columns(existing.id, etag=validator)
        return
    if c.room is not None:
        held = (existing.size_bytes or 0) if existing is not None and existing.sha256 and existing.status != "deleting" else 0
        refusal = await c.room(len(data) - held)
        if refusal:
            raise _Capped(f"Stopped because the vault is full, so nothing was removed: {refusal}")
    c.reached.add(rel)
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


# ── a page with no file, queued: fetch it ──────────────────────────────────────

async def fetch_document(kb: KnowledgeBase, row: KnowledgeDocumentDB) -> None:
    """Fetch the page a queued document with no file stands for — a refusal or failure being retried
    (A1's Retry queues it like any document), or a fetch a restart cut short — and leave it written
    (and queued for indexing) or resting with why. The ingest job calls this (`jobs._ingest`), so it
    runs wherever jobs run: OSS's in-process dispatcher, cloud's worker."""
    from core.knowledge import documents
    from core.knowledge.fetch import Fetcher, egress_refusal
    from core.knowledge.kb_settings import WebSource

    try:
        url = normalize_url(row.source_url or "")
    except ValueError:
        await record_outcome(kb, row.path, row.source_url or "", "failed", "fetch_failed",
                             "The document has no address to fetch.", existing=row)
        return
    refusal = egress_refusal()
    if refusal is not None:
        await record_outcome(kb, row.path, url, "failed", "fetch_failed", refusal, existing=row)
        return
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
                       by_path=by_path, named={url}, seen={url}, owned=owner is not None,
                       room=_vault_room() if _vault_room is not None else None)
            await _page(c, url, links_needed=False)
    except _Capped as exc:  # the vault is full: the page is not written, and the document says why
        await record_outcome(kb, row.path, url, "failed", "fetch_failed", str(exc), metadata=metadata or None)
        return
    except Exception as exc:  # noqa: BLE001 — the document must not stay queued
        _log.warning(f"knowledge fetch of {row.id} failed: {exc!r}")
        await record_outcome(kb, row.path, url, "failed", "fetch_failed", f"{type(exc).__name__}: {exc}",
                             metadata=metadata or None)
        return
    current = await documents.get(row.id)
    if current is not None and current.status == "queued" and not current.sha256:
        # Nothing was written and no outcome recorded (a redirect elsewhere, a duplicate): say so.
        await record_outcome(kb, row.path, url, "failed", "fetch_failed",
                             "The page did not come back as something to keep; it is tried again at the next crawl.",
                             metadata=metadata or None, existing=current)
