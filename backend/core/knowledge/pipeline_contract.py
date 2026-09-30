# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""The hand-off between ingestion (A1, A2) and indexing (B1, B2).

Ingestion owns a document's life up to a `ParsedDocument`: sync, fetch, parse,
sanitize, the status machine and the report. Indexing owns what happens to the
parsed tree: chunk, enrich, embed, write the chunk rows and the search index,
record the costs. The two meet at exactly one call:

    outcome = await resolve_index_entrypoint()(IndexContext(...))

`INDEX_ENTRYPOINT` names B1's `core.knowledge.pipeline:index_document`. Until it
exists, `resolve_index_entrypoint()` raises `KnowledgeUnavailable`, and A1's
job fails the document honestly (`failed`, reason `internal`) rather than
pretending; A1 tests the hand-off with a fake. The ingestion side then moves the
document to the status in `outcome` and stores its stages in the report.

Indexing never changes a document's status itself, and ingestion never writes a
chunk: one owner for each.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core.knowledge.errors import KnowledgeUnavailable
from core.knowledge.kb_settings import KnowledgeBase
from core.knowledge.parsed import ParsedDocument, ParseWarning
from core.knowledge.status import StageReport
from core.knowledge.types import Attribution, Usage

INDEX_ENTRYPOINT = "core.knowledge.pipeline:index_document"


@dataclass(frozen=True)
class IndexContext:
    """Everything indexing needs about one document version. The tenant is the current one."""

    kb: KnowledgeBase
    doc_id: str
    doc_version: int
    sha256: str
    path: str
    source: str
    parsed: ParsedDocument
    snapshot_id: str
    attribution: Attribution
    settings: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)  # the document's own, validated at ingest


@dataclass(frozen=True)
class IndexOutcome:
    """What indexing did. `status` is where the document goes next: "indexed", or
    "partial" with a reason when something was cut or left unread."""

    status: Literal["indexed", "partial"]
    chunks: int
    tokens: int
    status_reason: str | None = None
    stages: list[StageReport] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)  # already recorded in the ledger by indexing
    warnings: list[ParseWarning] = field(default_factory=list)


IndexEntrypoint = Callable[[IndexContext], Awaitable[IndexOutcome]]


def resolve_index_entrypoint() -> IndexEntrypoint:
    """B1's `index_document`, or `KnowledgeUnavailable` until B1 has built it."""
    from core.knowledge.seams import _load

    entry = _load(INDEX_ENTRYPOINT)
    if entry is None:
        raise KnowledgeUnavailable("Knowledge indexing is not built yet (lane B1).")
    return entry
