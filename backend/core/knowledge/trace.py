# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""A retrieval trace (version 1): everything that decided which chunks a search returned.

Stored in `knowledge_queries.trace`, shown in run logs and the test-search
panel, returned by the API when asked. It is what 16 §1 calls "a retrieval
trace in run logs that can be replayed": the query and its rewrite, the
filters, the index and its capabilities, every leg's candidates with their
ranks and scores, the fusion, the rerank, every cut and why, the final `[K#]`
hits with the document version each came from, and what it all cost.

**What it never holds** (validated here): candidate text beyond a ≤200-character
snippet on the final hits, and any embedding. Embeddings can be inverted back
into text (ZSinvert); the query's own vector is kept apart, in
`knowledge_queries.query_embedding`, and never serialized.

**Degradations are recorded, never silent** (`degraded`): a post-filter where
the index could not filter, a missing leg, a failed or unconfigured rerank, an
approximate index, a retired snapshot, a trimmed trace.

**Determinism** (frozen here, implemented by B2): weighted RRF is
`Σ w_leg / (k + rank_leg)` with a missing leg contributing 0, and every tie in
every list breaks by chunk id ascending. That is what lets `replay` compare
ids and ranks exactly and scores within 1e-6.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.knowledge.types import IndexCapabilities

TRACE_VERSION = 1
SNIPPET_MAX = 200
QUERY_MAX = 2000
TRACE_MAX_BYTES = 256 * 1024
#: Keys a trace may never carry. ("vector" is not one: it names the vector *leg* in weights,
#: ranks, scores and timings.)
FORBIDDEN_KEYS: frozenset[str] = frozenset({"embedding", "embeddings", "embedding_vec", "query_embedding",
                                            "query_vector"})

Surface = Literal["tool", "step", "api", "mcp", "eval", "ui"]
SURFACES: tuple[str, ...] = Surface.__args__  # type: ignore[attr-defined]

DEGRADED_CODES: tuple[str, ...] = (
    "post_filtered", "lexical_unavailable", "vector_unavailable", "rerank_failed", "rerank_unconfigured",
    "ann_approximate", "snapshot_retired", "truncated", "budget_paused", "embed_failed",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Origin(_Model):
    surface: Surface
    run_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    step_id: str | None = None
    turn: int | None = None
    api_key_id: str | None = None


class TraceRequest(_Model):
    query: str = Field(max_length=QUERY_MAX)
    rewritten_query: str | None = Field(default=None, max_length=QUERY_MAX)
    kb_ids: list[str]
    filters: dict | None = None  # canonical form (`filters.to_canonical`)
    path_prefix: str | None = None
    top_k: int
    rerank: bool | None = None
    score_threshold: float | None = None
    neighbors: int = 0
    mode: Literal["hybrid", "lexical", "vector"] = "hybrid"
    pinned_snapshots: dict[str, str] = Field(default_factory=dict)


class FusionParams(_Model):
    method: Literal["rrf"] = "rrf"
    k: int = 60
    weights: dict[str, float] = Field(default_factory=lambda: {"lexical": 1.0, "vector": 1.0})


class KbPipeline(_Model):
    snapshot_id: str
    embed_model: str | None = None
    embed_dims: int | None = None
    chunker_version: str | None = None
    fusion: FusionParams = Field(default_factory=FusionParams)
    overfetch: int = 50
    rerank_model: str | None = None
    top_k: int
    score_threshold: float | None = None
    parent_expansion: str = "off"


class IndexInfo(_Model):
    name: str
    version: str
    capabilities: dict  # `dataclasses.asdict(IndexCapabilities)`

    @classmethod
    def of(cls, caps: IndexCapabilities) -> IndexInfo:
        from dataclasses import asdict
        return cls(name=caps.name, version=caps.version, capabilities=asdict(caps))


class Degraded(_Model):
    code: str
    detail: str | None = None


class RankedId(_Model):
    chunk_id: str
    rank: int = Field(ge=1)
    score: float


class LegTrace(_Model):
    leg: Literal["lexical", "vector", "hybrid"]
    scopes: list[dict]  # [{kb_id, snapshot_id}]
    requested: int
    returned: int
    filtered: bool
    exact: bool
    latency_ms: int = 0
    candidates: list[RankedId] = Field(default_factory=list)


class FusedId(_Model):
    chunk_id: str
    score: float
    ranks: dict[str, int] = Field(default_factory=dict)


class FusionTrace(_Model):
    method: Literal["rrf"] = "rrf"
    params: FusionParams = Field(default_factory=FusionParams)
    candidates: list[FusedId] = Field(default_factory=list)


class RerankTrace(_Model):
    model: str
    cost_source: str
    input_count: int
    latency_ms: int = 0
    results: list[RankedId] = Field(default_factory=list)


class Cut(_Model):
    stage: Literal["threshold", "top_k", "dedup", "post_filter", "budget"]
    dropped: list[str] | None = None
    dropped_count: int = 0
    rule: str = ""


class TraceHit(_Model):
    label: str  # "[K1]"
    chunk_id: str
    doc_id: str
    doc_sha256: str | None = None
    doc_version: int | None = None
    snapshot_id: str
    kb_id: str
    path: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    snippet: str = Field(default="", max_length=SNIPPET_MAX)
    score: float
    scores: dict[str, float] = Field(default_factory=dict)
    ranks: dict[str, int] = Field(default_factory=dict)


class CostLine(_Model):
    stage: str
    model: str
    cost_source: str
    units: float = 0.0
    unit: str = "tokens"
    input_tokens: int = 0
    est_usd: float = 0.0
    charged_microusd: int | None = None


class TraceCosts(_Model):
    total_est_usd: float = 0.0
    total_charged_microusd: int | None = None
    rows: list[CostLine] = Field(default_factory=list)


class Timings(_Model):
    rewrite: int = 0
    embed: int = 0
    lexical: int = 0
    vector: int = 0
    fuse: int = 0
    rerank: int = 0
    total: int = 0


class ReplayInfo(_Model):
    query_vector_sha256: str | None = None
    exact_possible: bool = False
    notes: list[str] = Field(default_factory=list)


class Trace(_Model):
    trace_version: Literal[1] = TRACE_VERSION
    query_id: str
    created_at: str
    origin: Origin
    request: TraceRequest
    pipeline: dict[str, KbPipeline] = Field(default_factory=dict)  # by kb_id
    index: IndexInfo | None = None
    degraded: list[Degraded] = Field(default_factory=list)
    legs: list[LegTrace] = Field(default_factory=list)
    fusion: FusionTrace | None = None
    rerank: RerankTrace | None = None
    cuts: list[Cut] = Field(default_factory=list)
    hits: list[TraceHit] = Field(default_factory=list)
    costs: TraceCosts = Field(default_factory=TraceCosts)
    timings_ms: Timings = Field(default_factory=Timings)
    replay: ReplayInfo = Field(default_factory=ReplayInfo)

    @model_validator(mode="after")
    def _no_vectors_no_bulk(self) -> Trace:
        dumped = self.model_dump(mode="json")
        bad = _forbidden_keys(dumped)
        if bad:
            raise ValueError(f"a trace never holds embeddings: {sorted(bad)}")
        size = len(json.dumps(dumped, separators=(",", ":")).encode("utf-8"))
        if size > TRACE_MAX_BYTES:
            raise ValueError(f"a trace is at most {TRACE_MAX_BYTES} bytes; this one is {size} "
                             "(trim it with trace.fit())")
        return self


def _forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in FORBIDDEN_KEYS:
                found.add(key)
            found |= _forbidden_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            found |= _forbidden_keys(inner)
    return found


def fit(data: dict) -> dict:
    """A trace dict trimmed to `TRACE_MAX_BYTES`: the longest candidate lists are halved
    (leg candidates first, then fusion's) and a `truncated` note is added. Hits are
    never trimmed — they are what the answer cited."""
    def size(d: dict) -> int:
        return len(json.dumps(d, separators=(",", ":"), default=str).encode("utf-8"))

    trimmed = False
    while size(data) > TRACE_MAX_BYTES:
        lists = [leg["candidates"] for leg in data.get("legs", []) if leg.get("candidates")]
        if data.get("fusion") and data["fusion"].get("candidates"):
            lists.append(data["fusion"]["candidates"])
        longest = max(lists, key=len, default=None)
        if not longest or len(longest) <= 1:
            break
        del longest[len(longest) // 2:]
        trimmed = True
    if trimmed:
        data.setdefault("degraded", []).append({"code": "truncated", "detail": "candidate lists trimmed"})
    return data


# ── replay ─────────────────────────────────────────────────────────────────────

class ReplayRequest(_Model):
    """`exact` reuses the stored query vector and rerank scores ($0); it needs the
    snapshot still present (retired ones are kept `knowledge_snapshot_grace_days`)
    and an exact index. `live` embeds and reranks again against the active
    snapshot and reports the difference."""

    query_id: str
    mode: Literal["exact", "live"] = "exact"


class ReplayCheck(_Model):
    name: Literal["snapshot_present", "doc_sha_unchanged", "capabilities_unchanged", "legs_match", "hits_match"]
    ok: bool
    detail: str | None = None


class ReplayDiff(_Model):
    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    moved: list[str] = Field(default_factory=list)


class ReplayReport(_Model):
    query_id: str
    mode: Literal["exact", "live"]
    reproducible: bool
    checks: list[ReplayCheck] = Field(default_factory=list)
    diffs: dict[str, ReplayDiff] = Field(default_factory=dict)  # by leg name, and "hits"
    degraded: list[Degraded] = Field(default_factory=list)
    cost: TraceCosts = Field(default_factory=TraceCosts)
