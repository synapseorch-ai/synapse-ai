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
    "wallet_402": "The model wallet is empty. Top it up, then retry.",
    "egress_blocked": "The address was refused by the network guard.",
    "robots_disallowed": "The site's robots.txt disallows fetching this page.",
    "internal": "Something inside Synapse failed. Retrying may help; the error says what.",
    "embed_failed": "The embedding model failed. Retrying usually helps.",
    "embedding_invalid": "The embedding model answered with vectors that cannot be stored. Check the model.",
    "index_failed": "Writing the search index failed. Retrying usually helps.",
}

#: Reasons a retry cannot change: the same file fails the same way.
_NOT_RETRYABLE = frozenset({"over_size_cap", "over_page_cap", "empty_document", "excluded", "robots_disallowed",
                            "egress_blocked", "embedding_invalid"})


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
    return RetryHint(allowed=reason not in _NOT_RETRYABLE, hint=_HINTS.get(reason))


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
