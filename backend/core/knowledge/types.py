# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""The value types the seams and the search index pass around.

Frozen dataclasses, standard library only: the cloud API and every external
index adapter import these, and neither may pull in anything heavier.

Two of them carry money, and they are the reason the rest exist:

* `Usage` — what one call cost. **Every** seam call returns one (embed,
  rerank, OCR; parse and enrich through the pipeline), so the cost ledger
  (`ledger.py`) never has to guess.
* `Attribution` — whose the cost is: a knowledge base's ingestion
  (`session_id = "knowledge:<kb_id>"`), or the run or chat turn a search was
  made in (that run's and session's own ids, so retrieval shows up in the
  run's cost and on the Usage screens beside the model calls it served).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover
    from core.knowledge.filters import FilterNode

# ── money ──────────────────────────────────────────────────────────────────────

CostStage = Literal["parse", "ocr", "embed", "rerank", "enrich", "rewrite"]
#: platform: our models, billed to the wallet. byok: the org's own key, $0 to
#: the wallet, estimated $ shown. local: a model on the customer's machine.
CostSource = Literal["platform", "byok", "local"]
Unit = Literal["tokens", "pages", "search_units", "requests"]

COST_STAGES: tuple[str, ...] = ("parse", "ocr", "embed", "rerank", "enrich", "rewrite")
COST_SOURCES: tuple[str, ...] = ("platform", "byok", "local")
UNITS: tuple[str, ...] = ("tokens", "pages", "search_units", "requests")


@dataclass(frozen=True)
class Usage:
    """What one call cost.

    `est_usd` is always filled — from the pricing table, or 0 with
    `priced=False` when no rate is known. `charged_microusd` only when a
    wallet actually settled the call (cloud's gateway reports it); None
    everywhere else, BYOK and OSS included.
    """

    stage: CostStage
    cost_source: CostSource
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    units: float = 0.0
    unit: Unit = "tokens"
    est_usd: float = 0.0
    charged_microusd: int | None = None
    priced: bool = True
    latency_ms: int = 0
    request_id: str | None = None


@dataclass(frozen=True)
class Attribution:
    """Whose cost it is, and what it was for.

    `source` is the usage row's source: "knowledge" for ingestion; for a search,
    the caller's own ("chat", "orchestration", …) so the cost groups with the
    turn or run it served.
    """

    kb_id: str
    doc_id: str | None = None
    query_id: str | None = None
    snapshot_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    source: str = "knowledge"

    @staticmethod
    def ingestion_session(kb_id: str) -> str:
        return f"knowledge:{kb_id}"


# ── embedder ───────────────────────────────────────────────────────────────────

#: Retrieval models embed a question and a passage differently (Cohere's
#: `input_type`, Gemini's `task_type`, E5/BGE/nomic prefixes). A seam that
#: cannot tell them apart embeds every query as a document.
EmbedKind = Literal["document", "query"]


@dataclass(frozen=True)
class EmbeddingSpec:
    """Which model, at which width. `dims=None` means the model's native width."""

    model: str
    dims: int | None = None
    profile: str | None = None
    profile_version: str = ""


@dataclass(frozen=True)
class EmbedResult:
    vectors: list[list[float]]
    model: str
    dims: int
    usage: Usage


# ── reranker ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RerankSpec:
    model: str
    top_n: int


@dataclass(frozen=True)
class RerankResult:
    """(document index, score) pairs, best first; ties broken by index ascending."""

    ranking: list[tuple[int, float]]
    model: str
    usage: Usage


# ── ingestion dispatcher ───────────────────────────────────────────────────────

IngestOp = Literal["upsert", "delete", "retry", "reindex", "resume"]
IngestReason = Literal[
    "vault_write", "vault_delete", "vault_move", "upload", "url", "record", "retry",
    "reconcile", "snapshot_build", "budget_resume",
]


@dataclass(frozen=True)
class IngestJob:
    """One unit of ingestion work, as the dispatcher hands it to a runner.

    The tenant travels with the job because a queued job runs in another
    process, where no ContextVar survives.
    """

    tenant: str
    kb_id: str
    op: IngestOp
    reason: IngestReason
    path: str | None = None
    doc_id: str | None = None
    snapshot_id: str | None = None
    requested_at: str = ""

    def dedup_key(self) -> str:
        """Two jobs with this key are the same work; a queue keeps one."""
        target = self.doc_id or self.path or self.snapshot_id or "*"
        return f"knowledge:{self.tenant}:{self.kb_id}:{target}:{self.op}"


@dataclass(frozen=True)
class DispatchReceipt:
    mode: Literal["inline", "background", "queued"]
    job_id: str | None = None


# ── OCR ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BBoxT:
    """A box on a page, in points, origin top-left."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class PageImage:
    page: int
    png: bytes
    width: int
    height: int
    dpi: int


@dataclass(frozen=True)
class OcrPage:
    page: int
    text: str
    confidence: float | None = None
    lines: list[tuple[str, BBoxT]] | None = None


@dataclass(frozen=True)
class OcrResult:
    pages: list[OcrPage]
    engine: str
    usage: Usage  # unit="pages"


# ── search index ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndexCapabilities:
    """What an index can do. The pipeline degrades on these — and says so in the
    trace — rather than assuming.

    * `lexical` — a keyword leg exists.
    * `vector` — a vector leg exists.
    * `filter_pushdown` — filters run inside the index's own query, so a
      top-k is never padded with rows the filter excludes. Without it the
      pipeline over-fetches and post-filters (trace note `post_filtered`).
    * `per_leg_scores` — the legs are searched separately and each returns
      its own ranks/scores. Without it the index must implement
      `search_hybrid` and fusion happens inside it.
    * `search_mode` — "exact" (every candidate scored) or "ann" (approximate:
      replay can't promise identical results; trace note `ann_approximate`).
    * `lexical_languages` — the keyword leg honours a per-KB language.
    """

    name: str
    version: str
    lexical: bool
    vector: bool
    filter_pushdown: bool
    per_leg_scores: bool
    search_mode: Literal["exact", "ann"]
    max_dims: int | None = None
    lexical_languages: bool = False


@dataclass(frozen=True)
class Scope:
    """One knowledge base's one snapshot. Queries always name snapshots, so a
    search is pinned to exactly one index version per KB."""

    kb_id: str
    snapshot_id: str


@dataclass(frozen=True)
class IndexEntry:
    """What an index stores for one chunk.

    `text` and `fields` are always filled in full, even for an index that could
    read them from our own tables — that is what makes an external store a
    drop-in. `fields` is what the filter evaluator reads: the built-ins
    (path, source, mime, title, doc_date, indexed_at) plus the metadata.
    `vector` is None for a chunk that is stored but not embedded (a parent).
    """

    chunk_id: str
    doc_id: str
    kb_id: str
    snapshot_id: str
    text: str
    lang: str
    vector: Sequence[float] | None
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class LexicalQuery:
    scopes: tuple[Scope, ...]
    text: str
    lang: str
    filter: FilterNode | None
    limit: int


@dataclass(frozen=True)
class VectorQuery:
    """All scopes in one query share an embedding model and width."""

    scopes: tuple[Scope, ...]
    vector: Sequence[float]
    filter: FilterNode | None
    limit: int


@dataclass(frozen=True)
class HybridQuery:
    lexical: LexicalQuery
    vector: VectorQuery
    weights: Mapping[str, float]


@dataclass(frozen=True)
class Candidate:
    """One chunk a leg returned. Higher score is better; rank is 1-based."""

    chunk_id: str
    doc_id: str
    kb_id: str
    snapshot_id: str
    score: float
    rank: int


LegName = Literal["lexical", "vector", "hybrid"]


@dataclass(frozen=True)
class LegResult:
    """What one leg returned.

    `filtered` is whether the filter was applied inside the index. `exact` is
    whether every candidate was scored. `scanned` is how many rows were looked
    at, when the index knows. `notes` are degraded-codes for the trace.
    """

    leg: LegName
    candidates: tuple[Candidate, ...]
    filtered: bool
    exact: bool
    scanned: int | None = None
    notes: tuple[str, ...] = ()
    usage: Usage | None = None


@dataclass(frozen=True)
class IndexStats:
    chunks: int
    vectors: int
    dims: int | None
    bytes: int | None = None

