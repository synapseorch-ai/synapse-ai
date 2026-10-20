"""Building a document's `IngestionReport`, stage by stage — what the report drawer shows.

A `ReportBuilder` times each stage (`with builder.stage("parse") as st:`),
records a stage that raised as `failed` with the error's stable code, and ends
with the outcome. Nothing is left out without saying so: a stage the job never
reached is simply absent, a stage it passed over is `skipped` with a reason.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from core.knowledge.errors import KnowledgeError
from core.knowledge.status import ErrorInfo, IngestionReport, RetryHint, StageReport

#: What a person can do about each reason, shown beside Retry.
_HINTS: dict[str, str] = {
    "unsupported_format": "This file type is not read yet. It is indexed automatically once it is.",
    "over_size_cap": "The file is larger than the per-file limit (knowledge_max_file_mb).",
    "over_page_cap": "The file has more pages than the per-file limit (knowledge_max_file_pages).",
    "empty_document": "The file has no text to index.",
    "excluded": "The file is excluded from this knowledge base.",
    "budget_cap": "The knowledge base reached its monthly budget. Raise it, or wait for the month to turn.",
    "wallet_402": "The model wallet can't pay for this; the error says why. Retry once it can.",
    "egress_blocked": "The address was refused by the network guard.",
    "robots_disallowed": "The site's robots.txt disallows fetching this page.",
    "fetch_failed": "The page could not be fetched (no answer, or an error from the site). Retry fetches it "
                    "again; the next crawl does too.",
    "internal": "Something inside Synapse failed. Retrying may help; the error says what.",
    "embed_failed": "The embedding model failed. Retrying usually helps.",
    "enrich_failed": ("The model writing Contextual Retrieval's contexts failed. Retrying usually helps; contexts "
                      "already written are kept. Check its key if it says one is missing."),
    "embedding_invalid": "The embedding model answered with vectors that cannot be stored. Check the model.",
    "index_failed": "Writing the search index failed. Retrying usually helps.",
    "parser_timeout": "Reading the file took too long. Retry when the machine is less busy, or split the file.",
    "parser_memory": "Reading the file needed more memory than allowed. Split the file, or raise the parser's "
                     "memory limit.",
    "parser_crashed": "The file could not be read; it may be damaged. Re-save it in its program and upload again.",
    "encrypted": "The file is protected by a password. Remove the password and upload it again.",
    "truncated_pages": "Only some pages were read in time; the rest are not searchable. Retry to read them all.",
    "truncated_size": "The file holds more text than one document may; the rest is not searchable.",
    # Said by whether this install has OCR: see `retry_hint`.
    "no_text_layer": "Some pages are images with no text layer that OCR did not read. Retry reads them again.",
    "ocr_failed": "Reading scanned pages failed. Retrying usually helps.",
    "attachment_skipped": "Some attachments could not be read; the report lists which and why.",
}

#: Reasons a retry cannot change: the same file fails the same way.
_NOT_RETRYABLE = frozenset({"over_size_cap", "over_page_cap", "empty_document", "excluded", "robots_disallowed",
                            "egress_blocked", "embedding_invalid", "encrypted"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_info(exc: BaseException) -> ErrorInfo:
    """An exception as the report records it: its stable code, a message, and whether retrying helps."""
    if isinstance(exc, KnowledgeError):
        return ErrorInfo(code=exc.code, message=str(exc)[:2000], retryable=bool(exc.retryable))
    return ErrorInfo(code="internal", message=f"{type(exc).__name__}: {exc}"[:2000], retryable=True)


def retry_hint(reason: str | None) -> RetryHint:
    if reason is None:
        return RetryHint(allowed=True)
    hint = _HINTS.get(reason)
    if reason == "no_text_layer":
        from core.knowledge.seams import get_ocr_provider

        if get_ocr_provider() is None:  # OSS: nothing reads a scan, so Retry would not either
            hint = "Some pages are images with no text layer, and this install has no OCR to read them."
    return RetryHint(allowed=reason not in _NOT_RETRYABLE, hint=hint)


class StageHandle:
    """What the body of a `stage()` block may set."""

    def __init__(self) -> None:
        self.status = "ok"
        self.detail: dict[str, Any] = {}

    def partial(self, **detail: Any) -> None:
        self.status = "partial"
        self.detail.update(detail)

    def skipped(self, **detail: Any) -> None:
        self.status = "skipped"
        self.detail.update(detail)


class ReportBuilder:
    def __init__(self, *, doc_id: str, path: str, attempt: int, sha256: str | None = None) -> None:
        self.report = IngestionReport(doc_id=doc_id, path=path, sha256=sha256, attempt=attempt,
                                      started_at=_now(), outcome="queued")

    @contextmanager
    def stage(self, name: str) -> Iterator[StageHandle]:
        handle = StageHandle()
        started, clock = _now(), time.monotonic()
        try:
            yield handle
        except BaseException as exc:
            self.report.stages.append(StageReport(stage=name, status="failed", started_at=started,  # type: ignore[arg-type]
                                                  duration_ms=int((time.monotonic() - clock) * 1000),
                                                  detail=handle.detail, error=error_info(exc)))
            raise
        self.report.stages.append(StageReport(stage=name, status=handle.status, started_at=started,  # type: ignore[arg-type]
                                              duration_ms=int((time.monotonic() - clock) * 1000),
                                              detail=handle.detail))

    def add(self, stage: StageReport) -> None:
        self.report.stages.append(stage)

    def finish(self, outcome: str, reason: str | None = None, **fields: Any) -> IngestionReport:
        """The finished report. `fields` sets any other report field (pages, chunks, parser, …)."""
        data = self.report.model_dump()
        data.update(fields)
        data.update(outcome=outcome, status_reason=reason, finished_at=_now(), retry=retry_hint(reason).model_dump())
        self.report = IngestionReport.model_validate(data)
        return self.report
