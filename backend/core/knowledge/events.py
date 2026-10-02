# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""What Knowledge announces: run/chat events, a document's lifecycle, and webhooks.

**Run and chat events** flow through the same stream as `tool_execution` and
`tool_result` (which the tools still emit). `OrchestrationLogger.log_event`
silently drops a type it has no branch for, so each type below names the lane
that adds its branch (`RUN_EVENT_OWNERS`); a strict-xfail test per type keeps
the list honest until that lane lands. Payloads hold ids, counts, codes and
short labels — a query at most `QUERY_PREVIEW` characters, never chunk text.

**Lifecycle events** — a document queued, indexed, failed, paused, … — go to
`knowledge_observers`, the pattern of `core.orchestration.runner.event_observers`:
the UI's live badges, cloud's notifications and the public webhooks subscribe.
`emit` never raises: a broken observer cannot fail an ingestion.

**Webhooks** (P1) expose four of them to customers, never with a tenant id.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from core.log import get_logger

_log = get_logger(__name__)

QUERY_PREVIEW = 500

# ── run and chat events ────────────────────────────────────────────────────────

KNOWLEDGE_SEARCH = "knowledge_search"
KNOWLEDGE_READ = "knowledge_read"
KNOWLEDGE_CITATIONS = "knowledge_citations"
KNOWLEDGE_RETRIEVE = "knowledge_retrieve"
KNOWLEDGE_INGEST = "knowledge_ingest"

RUN_EVENT_TYPES: tuple[str, ...] = (
    KNOWLEDGE_SEARCH, KNOWLEDGE_READ, KNOWLEDGE_CITATIONS, KNOWLEDGE_RETRIEVE, KNOWLEDGE_INGEST,
)

#: The lane that emits each type and adds its `OrchestrationLogger` branch.
RUN_EVENT_OWNERS: dict[str, str] = {
    KNOWLEDGE_SEARCH: "B1",
    KNOWLEDGE_READ: "B1",
    KNOWLEDGE_CITATIONS: "B1",
    KNOWLEDGE_RETRIEVE: "R1",
    KNOWLEDGE_INGEST: "R1",
}


class KnowledgeSearchEvent(TypedDict, total=False):
    """An agent's `search_knowledge` call, as the run log and the chat panel show it."""

    type: Literal["knowledge_search"]
    turn: int
    query_id: str
    kb_ids: list[str]
    query: str  # ≤ QUERY_PREVIEW
    filtered: bool
    hit_count: int
    labels: list[str]  # "[K1]", …
    degraded: list[str]  # codes
    cost_usd: float
    latency_ms: int


class KnowledgeReadEvent(TypedDict, total=False):
    type: Literal["knowledge_read"]
    turn: int
    label: str
    chunk_id: str
    doc_id: str
    path: str
    window: int


class KnowledgeCitationsEvent(TypedDict, total=False):
    """Emitted once, before `final`: the answer's `[K#]` labels resolved.
    `citations` are `api_models.Citation` dicts; `unresolved` are labels the
    answer used that no search in this turn produced."""

    type: Literal["knowledge_citations"]
    citations: list[dict]
    unresolved: list[str]


class KnowledgeRetrieveEvent(TypedDict, total=False):
    type: Literal["knowledge_retrieve"]
    orch_step_id: str
    step_name: str
    query_id: str
    snapshot_ids: list[str]
    hit_count: int
    cost_usd: float
    latency_ms: int
    degraded: list[str]


class KnowledgeIngestEvent(TypedDict, total=False):
    type: Literal["knowledge_ingest"]
    orch_step_id: str
    step_name: str
    kb_id: str
    documents: list[dict]  # [{doc_id, record_id, status}]


# ── a document's lifecycle ─────────────────────────────────────────────────────

LIFECYCLE_TYPES: tuple[str, ...] = (
    "knowledge.document.queued", "knowledge.document.indexed", "knowledge.document.partial",
    "knowledge.document.failed", "knowledge.document.skipped", "knowledge.document.paused",
    "knowledge.document.blocked", "knowledge.document.deleted",
    "knowledge.base.reindexed", "knowledge.base.paused", "knowledge.base.resumed",
    "knowledge.snapshot.failed",
)

#: What customers can subscribe to (P1's public webhooks): public name → lifecycle type. The public
#: name is the lifecycle type itself (CCR-9): a webhook endpoint is not Knowledge's alone.
WEBHOOK_EVENTS: dict[str, str] = {
    "knowledge.document.indexed": "knowledge.document.indexed",
    "knowledge.document.partial": "knowledge.document.partial",
    "knowledge.document.failed": "knowledge.document.failed",
    "knowledge.base.reindexed": "knowledge.base.reindexed",
}


class KnowledgeLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str  # kev_…
    type: str
    created_at: str
    kb_id: str
    doc_id: str | None = None
    path: str | None = None
    status: str | None = None
    previous_status: str | None = None
    status_reason: str | None = None
    version: int | None = None
    sha256: str | None = None
    chunks: int | None = None
    pages: int | None = None
    warnings_count: int | None = None
    est_usd: float | None = None
    snapshot_id: str | None = None


Observer = Callable[[KnowledgeLifecycleEvent], "Awaitable[None] | None"]

#: Subscribers. Append at startup; each is called with every event, in order.
knowledge_observers: list[Observer] = []


async def emit(event: KnowledgeLifecycleEvent) -> None:
    """Hand `event` to every observer. Never raises."""
    if event.type not in LIFECYCLE_TYPES:
        _log.warning(f"unknown knowledge lifecycle event {event.type!r} not emitted")
        return
    for observer in list(knowledge_observers):
        try:
            result = observer(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 — an observer never fails an ingestion
            _log.warning(f"knowledge observer failed on {event.type}: {exc}")
