# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""A document's life: the status machine and the per-file ingestion report.

**Statuses.**

* queued → parsing → indexing → indexed (or partial: indexed, with something
  cut or unread, and the report says what).
* failed — the work broke; `status_reason` says how and whether retrying helps.
* skipped — nothing to do: an unsupported format, over a cap, empty, excluded.
* paused — a per-KB budget cap or an empty wallet (a 402). **Never retried in
  a loop**: it resumes when the cap is raised or at the month's start.
* blocked — the egress guard or proxy refused a fetch, or robots.txt disallows
  it. A refusal, not a failure.
* deleting — a tombstone while the index delete runs, so an external store
  never keeps vectors for a document our tables have forgotten.

Every change is a compare-and-set (`UPDATE … WHERE status IN allowed_from(new)`),
so a worker that loses a race to a delete notices and stops.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.knowledge.errors import InvalidTransition
from core.knowledge.parsed import ParserInfo, ParseWarning

DocStatus = Literal["queued", "parsing", "indexing", "indexed", "partial", "failed", "skipped",
                    "paused", "blocked", "deleting"]
STATUSES: tuple[str, ...] = DocStatus.__args__  # type: ignore[attr-defined]

#: Resting states: nothing is working on the document.
RESTING: frozenset[str] = frozenset({"indexed", "partial", "failed", "skipped", "paused", "blocked"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"parsing", "skipped", "paused", "blocked", "failed", "deleting"}),
    "parsing": frozenset({"indexing", "failed", "skipped", "paused", "queued", "deleting"}),
    "indexing": frozenset({"indexed", "partial", "failed", "paused", "queued", "deleting"}),
    **{s: frozenset({"queued", "deleting"}) for s in RESTING},
    "deleting": frozenset({"deleting"}),
}

#: The reasons each status may carry in `status_reason`.
REASONS: dict[str, tuple[str, ...]] = {
    "skipped": ("unsupported_format", "over_size_cap", "over_page_cap", "empty_document", "excluded"),
    "paused": ("budget_cap", "wallet_402"),
    "blocked": ("egress_blocked", "robots_disallowed"),
    "failed": ("parser_timeout", "parser_memory", "parser_crashed", "encrypted", "embed_failed",
               "embedding_invalid", "index_failed", "fetch_failed", "ocr_failed", "internal"),
    "partial": ("truncated_pages", "truncated_size", "no_text_layer", "ocr_failed", "attachment_skipped"),
}


def can_transition(current: str | None, target: str) -> bool:
    """Whether a document may go from `current` (None: it does not exist yet) to `target`."""
    if current is None:
        return target == "queued"
    return target in TRANSITIONS.get(current, frozenset())


def allowed_from(target: str) -> frozenset[str]:
    """The statuses a compare-and-set to `target` may start from."""
    return frozenset(s for s, nexts in TRANSITIONS.items() if target in nexts)


def check_transition(current: str | None, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(str(current), target)


# ── the ingestion report ───────────────────────────────────────────────────────

StageName = Literal["fetch", "parse", "ocr", "sanitize", "chunk", "enrich", "embed", "index"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorInfo(_Model):
    code: str
    message: str = Field(max_length=2000)
    retryable: bool = False


class StageReport(_Model):
    stage: StageName
    status: Literal["ok", "skipped", "partial", "failed"]
    started_at: str | None = None
    duration_ms: int = 0
    detail: dict = Field(default_factory=dict)
    error: ErrorInfo | None = None


class PagesReport(_Model):
    total: int = 0
    with_text: int = 0
    no_text_layer: int = 0
    ocr_applied: int = 0
    ocr_failed: int = 0
    skipped: list[int] = Field(default_factory=list, max_length=200)


class ChunksReport(_Model):
    count: int = 0
    by_kind: dict[str, int] = Field(default_factory=dict)
    tokens: int = 0


class CostReport(_Model):
    est_usd: float = 0.0
    charged_microusd: int | None = None
    by_stage: dict[str, float] = Field(default_factory=dict)


class RetryHint(_Model):
    allowed: bool = True
    hint: str | None = None


class IngestionReport(_Model):
    """What happened to one file, stage by stage — the report drawer's content.

    Stored in `knowledge_documents.report`. Truncation always reads `partial`
    with a warning; nothing is left out without saying so.
    """

    report_version: Literal[1] = 1
    doc_id: str
    path: str
    sha256: str | None = None
    attempt: int = 1
    started_at: str | None = None
    finished_at: str | None = None
    outcome: DocStatus
    status_reason: str | None = None
    stages: list[StageReport] = Field(default_factory=list)
    pages: PagesReport = Field(default_factory=PagesReport)
    chunks: ChunksReport = Field(default_factory=ChunksReport)
    warnings: list[ParseWarning] = Field(default_factory=list)
    cost: CostReport = Field(default_factory=CostReport)
    parser: ParserInfo | None = None
    snapshot_id: str | None = None
    retry: RetryHint = Field(default_factory=RetryHint)
