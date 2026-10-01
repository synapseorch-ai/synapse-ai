"""`parse_document`: the one call ingestion makes to turn a file into a `ParsedDocument`.

Around `parse_bytes` it adds what one document's parse needs in a server:

1. **The parse cache** (`cache.py`): a file parsed before, by the same parser
   with the same OCR, is never parsed again (a re-embed, a blue/green build).
2. **Where it runs:** a text file up to `IN_PROCESS_MAX_BYTES` in a thread;
   everything else in a subprocess with a timeout and a memory cap
   (`runner.py`), Docling one at a time per process.
3. **OCR** of the pages with no text layer, through the seam, when a provider
   is set (`ocr.py`).
4. **A heartbeat** while it works, so reconcile never takes a long parse for a
   stuck one.

It raises what the reader raised (`UnsupportedFormat`, a `ParseRefused`), and
`BudgetExceeded` / `WalletExhausted` from OCR, for the job to turn into a status.
"""
from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
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
    if fmt.text and len(data) <= limits.IN_PROCESS_MAX_BYTES:
        parsed = await asyncio.to_thread(parse_bytes, data, **common)
        return ParseResult(parsed=parsed, detail={"runner": "thread"})

    docling = fmt.name == "pdf" and docling_ready()
    job = {**common, "max_pages": settings.max_file_pages, "ocr": False, "docling": docling}
    slot = _docling_slot() if docling else _no_slot()
    async with slot, _beating(heartbeat):
        async with run_parse(data, job, timeout_s=limits.parse_timeout_s(),
                             memory_mb=limits.parse_memory_mb(docling=docling)) as out:
            detail = {"runner": "subprocess", "duration_ms": out.duration_ms, "peak_rss_mb": out.peak_rss_mb}
            if out.cut:
                detail["cut"] = out.cut
            return ParseResult(parsed=out.parsed, detail=detail)


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
