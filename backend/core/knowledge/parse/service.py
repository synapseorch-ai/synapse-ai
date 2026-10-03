"""`parse_document`: the one call ingestion makes to turn a file into a `ParsedDocument`.

Around `parse_bytes` it adds what one document's parse needs in a server:

1. **The parse cache** (`cache.py`): a file parsed before, by the same parser
   with the same OCR, is never parsed again (a re-embed, a retry, B2b's
   blue/green build — which calls this with the file's bytes and gets the
   cached parse back).
2. **Where it runs:** a text file up to `IN_PROCESS_MAX_BYTES` in a thread;
   everything else in a subprocess with a timeout and a memory cap
   (`runner.py`), Docling one at a time per process.
3. **OCR** of the pages with no text layer, through the seam, when a provider
   is set (`ocr.py`).
4. **A heartbeat** while it works, so reconcile never takes a long parse for a
   stuck one.
5. **Progress** for a screen (PG1): the stage, and a paged reader's pages as
   the parsing process reads them (`runner.py`).

It raises what the reader raised (`UnsupportedFormat`, a `ParseRefused`), and
`BudgetExceeded` / `WalletExhausted` from OCR, for the job to turn into a status.
"""
from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.knowledge.parse import limits

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase
    from core.knowledge.parsed import ParsedDocument
    from core.knowledge.status import StageReport
    from core.knowledge.types import Attribution, Usage


@dataclass
class ParseResult:
    parsed: ParsedDocument
    #: For the report's parse stage: where it ran, how long, peak memory, a cache hit, a cut.
    detail: dict[str, Any] = field(default_factory=dict)
    #: What OCR cost, for the report (the ledger rows are already written).
    usage: list[Usage] = field(default_factory=list)
    ocr_stage: StageReport | None = None


_docling_slots: dict[int, asyncio.Semaphore] = {}


def _docling_slot() -> asyncio.Semaphore:
    """At most one Docling parse at a time in this process (about 2 GB each)."""
    loop = id(asyncio.get_running_loop())
    if loop not in _docling_slots:
        _docling_slots.clear()
        _docling_slots[loop] = asyncio.Semaphore(1)
    return _docling_slots[loop]


def docling_ready() -> bool:
    """Docling imports here and its models are on disk (`docling_adapter.artifacts_path`)."""
    if importlib.util.find_spec("core.knowledge.parse.docling_adapter") is None:
        return False
    from core.knowledge.parse.docling_adapter import available

    return available()


async def parse_document(data: bytes, *, path: str, source: str, sha256: str, mime: str,
                         url: str | None = None, modified_at: str | None = None,
                         kb: KnowledgeBase | None = None, attribution: Attribution | None = None,
                         heartbeat: Callable[[], Awaitable[Any]] | None = None) -> ParseResult:
    from core.knowledge import progress
    from core.knowledge.config import knowledge_settings
    from core.knowledge.parse import UnsupportedFormat, formats, parse_bytes
    from core.knowledge.parse.runner import run_parse

    fmt = formats.resolve(path, data)
    if not formats.reader_built(fmt):
        raise UnsupportedFormat(f"{formats.extension(path)} is not read yet.")
    if not formats.has_capability(fmt.needs):
        raise UnsupportedFormat(formats.missing_capability_message(fmt))
    settings = knowledge_settings()
    common = dict(path=path, source=source, sha256=sha256, mime=mime, url=url, modified_at=modified_at)
    tracker = progress.current()
    if fmt.text and len(data) <= limits.IN_PROCESS_MAX_BYTES:
        if tracker is not None:
            tracker.stage("parse")  # one pass: nothing to count
        parsed = await asyncio.to_thread(parse_bytes, data, **common)
        return ParseResult(parsed=parsed, detail={"runner": "thread"})

    from core.knowledge.parse import cache
    from core.knowledge.parse.ocr import engine_id
    from core.knowledge.parse.tree import source_info
    from core.knowledge.seams import get_ocr_provider

    provider = get_ocr_provider()
    docling = fmt.name == "pdf" and docling_ready()
    sig = None if fmt.text else cache.signature(fmt, path=path, docling=docling,
                                                ocr_engine=engine_id(provider) if provider else None)
    if sig is not None:
        hit = await cache.get(sha256, sig, source_info(path=path, sha256=sha256, size=len(data), mime=mime,
                                                       source=source, url=url, modified_at=modified_at))
        if hit is not None:
            return ParseResult(parsed=hit, detail={"runner": "cache"})
    if tracker is not None:
        tracker.stage("parse")  # counted once a paged reader tells its pages
    job = {**common, "max_pages": settings.max_file_pages, "ocr": provider is not None, "docling": docling}
    if docling:
        from core.knowledge.parse.docling_adapter import artifacts_path

        job["docling_artifacts"] = str(artifacts_path())
    async with _beating(heartbeat), AsyncExitStack() as stack:
        async with _docling_slot() if docling else _no_slot():  # held while the child runs, not during OCR
            out = await stack.enter_async_context(run_parse(
                data, job, timeout_s=limits.parse_timeout_s(), memory_mb=limits.parse_memory_mb(docling=docling)))
        detail: dict[str, Any] = {"runner": "subprocess", "duration_ms": out.duration_ms,
                                  "peak_rss_mb": out.peak_rss_mb}
        if out.cut:
            detail["cut"] = out.cut
        result = ParseResult(parsed=out.parsed, detail=detail)
        if provider is not None and out.rendered:
            from core.knowledge.parse.ocr import read_scanned_pages

            result.parsed, result.usage, result.ocr_stage = await read_scanned_pages(
                out.parsed, out, provider, kb=kb, attribution=attribution, sha256=sha256)
        if sig is not None and cache.cacheable(result.parsed, cut=out.cut):
            await cache.put(sha256, sig, result.parsed)
            if provider is not None and out.rendered:  # the whole parse is kept: its pages need not be (F344)
                await cache.drop_pages(sha256, engine_id(provider))
        return result


@asynccontextmanager
async def _no_slot() -> AsyncIterator[None]:
    yield


@asynccontextmanager
async def _beating(heartbeat: Callable[[], Awaitable[Any]] | None) -> AsyncIterator[None]:
    """Call `heartbeat` every `HEARTBEAT_S` until the block ends. A failed beat is logged, never raised."""
    if heartbeat is None:
        yield
        return

    async def beat() -> None:
        from core.log import get_logger

        while True:
            await asyncio.sleep(limits.HEARTBEAT_S)
            try:
                await heartbeat()
            except Exception as exc:  # noqa: BLE001 — a missed beat only risks a duplicate parse
                get_logger(__name__).warning(f"knowledge parse heartbeat failed: {exc}")

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
