"""OCR for the pages that have no text layer — only those (16 §3.5, D153 #5).

The parsing child renders each such page to a PNG (it holds no credentials
and no seam is registered in it); this module, in the job's process, sends
them to `get_ocr_provider()` in batches of its `max_pages_per_call`, merges
the text into the document (`tree.merge_ocr`), and writes one cost row per
call. KB0 measured OCR at about 9× the parse time for the same text, which is
why a page with a text layer is never sent.

* **The budget is checked before any page is sent** (`ledger.check_budget`;
  B2b supplies the estimate through `price_per_page_usd` until the cost
  preview exists). A spent budget or an empty wallet raises, and the job
  pauses the document — nothing retries in a loop.
* **A failed batch is reported, never silent:** its pages stay unread, an
  `ocr_failed` warning counts them, and the document ends `partial`. Two
  failures in a row stop the rest (the provider is down), also reported.
* **The whole document's OCR has a deadline** (`limits.ocr_timeout_s`).
"""
from __future__ import annotations

import asyncio
import struct
import time
from typing import TYPE_CHECKING, Any

from core.knowledge.errors import BudgetExceeded, WalletExhausted
from core.knowledge.parse import limits
from core.knowledge.parse.tree import merge_ocr
from core.knowledge.parsed import ParsedDocument, ParseWarning

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.kb_settings import KnowledgeBase
    from core.knowledge.parse.runner import ChildOutcome
    from core.knowledge.status import StageReport
    from core.knowledge.types import Attribution, Usage

#: Consecutive failed batches after which the rest are not sent.
MAX_FAILED_BATCHES = 2


def engine_id(provider: Any) -> str:
    """What names an OCR provider in the parse cache's key: a changed engine reads pages again."""
    name = getattr(provider, "engine", None) or getattr(provider, "name", None)
    return str(name) if name else f"{type(provider).__module__}.{type(provider).__qualname__}"


def png_size(png: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk, without decoding it."""
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return 0, 0
    return struct.unpack(">II", png[16:24])


async def read_scanned_pages(parsed: ParsedDocument, out: ChildOutcome, provider: Any, *,
                             kb: KnowledgeBase | None, attribution: Attribution | None,
                             settings: dict | None = None) -> tuple[ParsedDocument, list[Usage], StageReport]:
    """`parsed` with the rendered pages read by `provider`; the calls' usage; the report's `ocr` stage."""
    from core.knowledge import ledger
    from core.knowledge.errors import OcrError
    from core.knowledge.status import ErrorInfo, StageReport
    from core.knowledge.types import Attribution as Attr, PageImage

    unread = {p.number for p in parsed.pages if p.quality.no_text_layer and not p.quality.ocr_applied}
    pages = [n for n in out.rendered if n in unread]
    started, clock = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), time.monotonic()
    if kb is not None:
        price = float(getattr(provider, "price_per_page_usd", 0.0) or 0.0)
        await ledger.check_budget(kb, price * len(pages))  # BudgetExceeded: the job pauses the document
    attribution = attribution or Attr(kb_id=kb.id if kb else "", source="knowledge")
    if settings is None:
        from core.config import load_settings
        settings = load_settings()

    size = max(1, int(getattr(provider, "max_pages_per_call", 1) or 1))
    deadline = clock + limits.ocr_timeout_s()
    texts: dict[int, tuple[str, float | None]] = {}
    usage: list[Usage] = []
    failed: list[int] = []
    errors: list[str] = []
    engine = engine_id(provider)
    streak = 0
    try:
        for start in range(0, len(pages), size):
            batch = pages[start:start + size]
            remaining = deadline - time.monotonic()
            if streak >= MAX_FAILED_BATCHES or remaining <= 0:
                failed.extend(batch)
                continue
            images = []
            for number in batch:
                png = out.page_png(number)
                width, height = png_size(png)
                images.append(PageImage(page=number, png=png, width=width, height=height, dpi=limits.OCR_DPI))
            try:
                result = await asyncio.wait_for(
                    provider.ocr(images, lang_hints=[], settings=settings, attribution=attribution), timeout=remaining)
            except (BudgetExceeded, WalletExhausted):
                raise
            except (OcrError, asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 — reported, never silent
                failed.extend(batch)
                errors.append(f"{type(exc).__name__}: {exc}"[:300])
                streak += 1
                continue
            streak = 0
            engine = result.engine or engine
            usage.append(result.usage)
            answered = {p.page: p for p in result.pages}
            for number in batch:
                page = answered.get(number)
                if page is None:
                    failed.append(number)
                else:
                    texts[number] = (page.text, page.confidence)
    finally:  # what was read is paid for, even when a later batch finds the wallet empty
        if usage:
            await ledger.record_costs([ledger.CostRow(usage=u, attribution=attribution) for u in usage])

    merged = merge_ocr(parsed, texts, engine=engine) if texts else parsed
    if failed:
        shown = ", ".join(str(n) for n in failed[:20]) + (" …" if len(failed) > 20 else "")
        why = errors[-1] if errors else "the provider returned no text for them"
        warning = ParseWarning(code="ocr_failed", count=len(failed),
                               message=f"OCR failed for {len(failed)} page(s) (page {shown}): {why}")
        merged = merged.model_copy(update={"warnings": [*merged.warnings, warning]})
    read = merged.stats.pages_ocr - parsed.stats.pages_ocr
    status = "ok" if not failed else ("partial" if read else "failed")
    stage = StageReport(stage="ocr", status=status, started_at=started,
                        duration_ms=int((time.monotonic() - clock) * 1000),
                        detail={"engine": engine, "pages": len(pages), "read": read, "failed": len(failed)},
                        error=ErrorInfo(code="ocr_failed", message=errors[-1], retryable=True) if errors else None)
    return merged, usage, stage
