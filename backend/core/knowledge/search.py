"""Search: a question over one or more knowledge bases, answered with labelled passages and a trace (B1a, D158).

One function serves every surface — the agent tool, the Retrieve step, the API,
MCP, evals and the test-search panel — so they cannot drift apart:

    await search(SearchRequest(query=..., kb_ids=[...]), origin=Origin(surface="tool", ...))

**Pinned to snapshots.** Each KB is searched in its active snapshot, or the one
the request pins (`snapshot_ids`), so a search names exactly one index version
per KB, and the trace records which.

**Keyword only, for now.** B1 searches the keyword leg; B2 adds query embedding,
the vector leg, fusion and rerank. A `hybrid` request (the default) is answered
from the keyword leg and says so in the trace (`vector_unavailable`); a
`vector` request answers empty with the same code. Nothing degrades silently.

**Filters and the path prefix run inside the index's query** when the index can
(`filter_pushdown`), so a top-k is never filled with rows the filter excludes;
otherwise the hits are filtered here and the trace says `post_filtered`.

**Languages.** Scopes are grouped by the keyword language their rows were
indexed with (the snapshot's), one keyword query per group; more than one group
is fused by reciprocal rank, `Σ 1 / (k + rank)`, ties by chunk id.

**Expansion.** `neighbors` (or a KB's `parent_expansion`) widens a hit's
`content` with the chunks around it — the same document, the same snapshot —
or with its whole section (the virtual parent: the run of chunks sharing its
heading path). The hit's citation keeps its own span.

**Labels** run `[K{label_start}]`, `[K{label_start+1}]`, …; the agent tool passes
where its turn's previous searches stopped.

Every search writes its trace to `knowledge_queries` (`trace_store`); a trace that
cannot be written is logged, never raised.
"""
from __future__ import annotations

import importlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.knowledge.agent_contract import format_label
from core.knowledge.api_models import Citation, CostSummary, Hit, SearchRequest, SearchResponse
from core.knowledge.chunker import ATTACHMENT_KEY, estimate_tokens
from core.knowledge.config import knowledge_enabled
from core.knowledge.errors import KnowledgeDisabled, KnowledgeError
from core.knowledge.filters import BUILTINS, evaluate, parse_filter, path_under, to_canonical
from core.knowledge.ids import QUERY_PREFIX, new_id
from core.knowledge.kb_settings import COLLECTION, KnowledgeBase
from core.knowledge.snapshots import active_snapshots, snapshot_info
from core.knowledge.telemetry import KNOWLEDGE_KB_COUNT, SPAN_SEARCH
from core.knowledge.textsearch import normalize_language
from core.knowledge.trace import (
    SNIPPET_MAX, Cut, Degraded, FusedId, FusionParams, FusionTrace, IndexInfo, KbPipeline, LegTrace, Origin,
    RankedId, ReplayInfo, Timings, Trace, TraceHit, TraceRequest, fit,
)
from core.knowledge.types import Candidate, LexicalQuery, Scope
from core.log import get_logger
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB
from core.telemetry import spans as _spans

_log = get_logger(__name__)

_C = KnowledgeChunkDB.__table__
_D = KnowledgeDocumentDB.__table__
RRF_K = 60
MAX_LABEL = 999
VECTOR_LATER = "query embedding and fusion arrive with B2"


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


# ── knowledge bases ───────────────────────────────────────────────────────────

async def load_bases(kb_ids: Sequence[str]) -> list[KnowledgeBase]:
    """The active knowledge bases named, in the order named. Unknown or disabled ones are refused.

    Reads A1's registry (`core.knowledge.bases`) when it is installed, else the
    `knowledge_bases` collection directly; a record that does not validate is
    skipped and logged.
    """
    wanted = list(dict.fromkeys(kb_ids))
    try:
        registry = importlib.import_module("core.knowledge.bases")  # A1's
    except ModuleNotFoundError as exc:
        if exc.name != "core.knowledge.bases":
            raise
        registry = None
    if registry is not None and hasattr(registry, "list_bases"):
        known = {kb.id: kb for kb in await registry.list_bases()}
    else:
        from core.store import collections

        known = {}
        for item in await collections.load(COLLECTION):
            try:
                kb = KnowledgeBase.model_validate(item)
            except Exception as exc:  # noqa: BLE001 — one bad record never hides the others
                _log.warning(f"knowledge base record {item.get('id')!r} skipped: {exc}")
                continue
            known[kb.id] = kb
    missing = [k for k in wanted if k not in known or known[k].status != "active"]
    if missing:
        raise KnowledgeError(f"No active knowledge base {missing[0]!r}.", code="kb_not_found")
    return [known[k] for k in wanted]


# ── rows ──────────────────────────────────────────────────────────────────────

@dataclass
class ChunkView:
    """A chunk as search shows it: its row, and its document's `source_url`."""

    row: Any
    source_url: str | None

    def fields(self) -> dict[str, Any]:
        r = self.row
        fields = {name: getattr(r, name) for name in BUILTINS}
        fields["indexed_at"] = r.indexed_at.isoformat() if r.indexed_at else None
        fields.update(r.meta or {})
        return fields


async def load_chunks(chunk_ids: Sequence[str]) -> dict[str, ChunkView]:
    """The current tenant's chunk rows by chunk id (absent ids are absent)."""
    from core.store import session

    if not chunk_ids:
        return {}
    out: dict[str, ChunkView] = {}
    ids = list(chunk_ids)
    async with session() as s:
        for i in range(0, len(ids), 500):
            rows = (await s.execute(
                select(KnowledgeChunkDB, _D.c.source_url)
                .join(_D, _D.c.id == _C.c.doc_id)
                .where(_C.c.tenant_id == _tenant(), _C.c.chunk_id.in_(ids[i:i + 500])))).all()
            for chunk, source_url in rows:
                out[chunk.chunk_id] = ChunkView(chunk, source_url)
    return out


async def neighbours(row: Any, window: int) -> list[Any]:
    """The rows up to `window` places either side of `row` in its document and snapshot, in order."""
    from core.store import session

    if window <= 0:
        return [row]
    async with session() as s:
        rows = (await s.execute(select(KnowledgeChunkDB).where(
            _C.c.tenant_id == _tenant(), _C.c.doc_id == row.doc_id, _C.c.snapshot_id == row.snapshot_id,
            _C.c.doc_version == row.doc_version, _C.c.ordinal >= row.ordinal - window,
            _C.c.ordinal <= row.ordinal + window).order_by(_C.c.ordinal))).scalars().all()
    return list(rows) or [row]


async def section(row: Any, budget_tokens: int) -> list[Any]:
    """The virtual parent: the run of chunks around `row` sharing its heading path (and attachment),
    grown outward from `row` while it fits `budget_tokens`."""
    from core.store import session

    async with session() as s:
        rows = (await s.execute(select(KnowledgeChunkDB).where(
            _C.c.tenant_id == _tenant(), _C.c.doc_id == row.doc_id, _C.c.snapshot_id == row.snapshot_id,
            _C.c.doc_version == row.doc_version).order_by(_C.c.ordinal))).scalars().all()

    def key(r) -> tuple:
        return (tuple(r.heading_path or []), (r.meta or {}).get(ATTACHMENT_KEY))

    ordinals = {r.ordinal: r for r in rows}
    run = [row]
    used = row.tokens or estimate_tokens(row.content)
    lo = hi = row.ordinal
    while True:
        grew = False
        for nxt in (hi + 1, lo - 1):
            cand = ordinals.get(nxt)
            if cand is None or key(cand) != key(row):
                continue
            cost = cand.tokens or estimate_tokens(cand.content)
            if used + cost > budget_tokens:
                continue
            used += cost
            if nxt > hi:
                hi, run = nxt, run + [cand]
            else:
                lo, run = nxt, [cand] + run
            grew = True
        if not grew:
            return run


def joined(rows: Sequence[Any]) -> str:
    """Consecutive chunks' text as one passage, each character once (overlaps are trimmed)."""
    parts: list[str] = []
    last = None
    for r in rows:
        text = r.content
        same_text = last is not None and (last.meta or {}).get(ATTACHMENT_KEY) == (r.meta or {}).get(ATTACHMENT_KEY)
        if same_text and r.char_start < last.char_end:
            text = text[max(0, last.char_end - r.char_start):]
            parts.append(text)
        else:
            if parts:
                parts.append("\n\n")
            parts.append(text)
        last = r
    return "".join(parts).strip()


def snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET_MAX else flat[:SNIPPET_MAX - 1].rstrip() + "…"


def citation(label: str, view: ChunkView) -> Citation:
    r = view.row
    return Citation(
        label=label, kb_id=r.kb_id, doc_id=r.doc_id, chunk_id=r.chunk_id, path=r.path or "",
        source=r.source or "vault", source_url=view.source_url, title=r.title, page_start=r.page_start,
        page_end=r.page_end, char_start=r.char_start, char_end=r.char_end, heading_path=list(r.heading_path or []),
        snippet=snippet(r.content), sha256=r.doc_sha256, doc_version=r.doc_version,
        indexed_at=r.indexed_at.isoformat() if r.indexed_at else None, snapshot_id=r.snapshot_id,
    )


# ── the search ────────────────────────────────────────────────────────────────

def _rrf(groups: Sequence[tuple[str, Sequence[Candidate]]]) -> list[tuple[Candidate, float, dict[str, int]]]:
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    first: dict[str, Candidate] = {}
    for name, candidates in groups:
        for c in candidates:
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + 1.0 / (RRF_K + c.rank)
            ranks.setdefault(c.chunk_id, {})[name] = c.rank
            first.setdefault(c.chunk_id, c)
    ordered = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
    return [(first[cid], score, ranks[cid]) for cid, score in ordered]


def _span_attributes(req: SearchRequest, **_kw) -> dict:
    return {KNOWLEDGE_KB_COUNT: len(req.kb_ids)}


@_spans.traced_coroutine(SPAN_SEARCH, _span_attributes)
async def search(req: SearchRequest, *, origin: Origin, label_start: int = 1,
                 settings: dict | None = None) -> SearchResponse:
    """Search `req.kb_ids` for `req.query`. See the module docstring."""
    from core.knowledge import trace_store
    from core.knowledge.seams import get_knowledge_index

    if not knowledge_enabled(settings):
        raise KnowledgeDisabled("Knowledge is switched off.")
    t0 = time.monotonic()
    query_id = new_id(QUERY_PREFIX)
    created_at = datetime.now(timezone.utc).isoformat()
    kbs = await load_bases(req.kb_ids)
    by_id = {kb.id: kb for kb in kbs}
    index = get_knowledge_index()
    caps = index.capabilities
    degraded: list[Degraded] = []
    cuts: list[Cut] = []

    pinned = dict(req.snapshot_ids or {})
    if set(pinned) - set(by_id):
        raise KnowledgeError("A pinned snapshot names a knowledge base the search does not.", code="snapshot_not_found")
    snapshot_of = {**(await active_snapshots([k for k in by_id if k not in pinned])), **pinned}
    info = await snapshot_info(list(snapshot_of.values()))
    for kb_id, snap in pinned.items():
        if snap not in info or info[snap]["kb_id"] != kb_id:
            raise KnowledgeError(f"No snapshot {snap!r} of knowledge base {kb_id!r}.", code="snapshot_not_found")
        if info[snap]["status"] == "retired":
            degraded.append(Degraded(code="snapshot_retired", detail=snap))

    node = parse_filter(req.filters)
    prefix = req.path_prefix if req.path_prefix and req.path_prefix.strip("/") else None
    top_k = req.top_k or max((kb.retrieval.top_k for kb in kbs), default=8)
    top_k = max(1, min(top_k, MAX_LABEL - label_start + 1))
    threshold = req.score_threshold
    if threshold is None and len(kbs) == 1:
        threshold = kbs[0].retrieval.score_threshold

    groups: dict[str, list[Scope]] = {}
    for kb in kbs:
        snap = snapshot_of.get(kb.id)
        if snap is None:
            continue
        lang = normalize_language((info.get(snap) or {}).get("language") or kb.language)
        groups.setdefault(lang, []).append(Scope(kb.id, snap))

    legs: list[LegTrace] = []
    results: list[tuple[str, Sequence[Candidate]]] = []
    lexical_ms = 0
    if req.mode in ("hybrid", "vector"):
        degraded.append(Degraded(code="vector_unavailable", detail=VECTOR_LATER))
    if req.mode != "vector" and groups:
        if not caps.lexical:
            degraded.append(Degraded(code="lexical_unavailable"))
        else:
            fetch = top_k if caps.filter_pushdown or (node is None and prefix is None) else min(top_k * 4, 200)
            for lang, scopes in sorted(groups.items()):
                t_leg = time.monotonic()
                leg = await index.search_lexical(LexicalQuery(tuple(scopes), req.query, lang, node, fetch,
                                                              path_prefix=prefix))
                ms = int((time.monotonic() - t_leg) * 1000)
                lexical_ms += ms
                results.append((f"lexical:{lang}" if len(groups) > 1 else "lexical", leg.candidates))
                legs.append(LegTrace(
                    leg="lexical", scopes=[{"kb_id": s.kb_id, "snapshot_id": s.snapshot_id} for s in scopes],
                    requested=fetch, returned=len(leg.candidates), filtered=leg.filtered, exact=leg.exact,
                    latency_ms=ms, candidates=[RankedId(chunk_id=c.chunk_id, rank=c.rank, score=c.score)
                                               for c in leg.candidates]))
                for note in leg.notes:
                    if note not in {d.code for d in degraded}:
                        degraded.append(Degraded(code=note))
                if (node is not None or prefix) and not leg.filtered:
                    if "post_filtered" not in {d.code for d in degraded}:
                        degraded.append(Degraded(code="post_filtered"))

    fusion = None
    if len(results) > 1:
        fused = _rrf(results)
        fusion = FusionTrace(params=FusionParams(k=RRF_K, weights={name: 1.0 for name, _ in results}),
                             candidates=[FusedId(chunk_id=c.chunk_id, score=score, ranks=ranks)
                                         for c, score, ranks in fused])
        ranked = [(c, score, ranks) for c, score, ranks in fused]
    elif results:
        name = results[0][0]
        ranked = [(c, c.score, {name: c.rank}) for c in results[0][1]]
    else:
        ranked = []

    views = await load_chunks([c.chunk_id for c, _, _ in ranked])
    kept = []
    dropped_filter: list[str] = []
    dropped_threshold: list[str] = []
    for c, score, ranks in ranked:
        view = views.get(c.chunk_id)
        if view is None:  # deleted between the index's answer and this read
            continue
        if any(code.code == "post_filtered" for code in degraded):
            f = view.fields()
            if not evaluate(node, f) or (prefix and not path_under(str(f.get("path") or ""), prefix)):
                dropped_filter.append(c.chunk_id)
                continue
        if threshold is not None and fusion is None and score < threshold:  # RRF scores are not comparable
            dropped_threshold.append(c.chunk_id)
            continue
        kept.append((c, score, ranks, view))
    if dropped_filter:
        cuts.append(Cut(stage="post_filter", dropped=dropped_filter[:50], dropped_count=len(dropped_filter),
                        rule="filter or path prefix applied after the index"))
    if dropped_threshold:
        cuts.append(Cut(stage="threshold", dropped=dropped_threshold[:50], dropped_count=len(dropped_threshold),
                        rule=f"score < {threshold}"))
    if len(kept) > top_k:
        extra = [c.chunk_id for c, _, _, _ in kept[top_k:]]
        cuts.append(Cut(stage="top_k", dropped=extra[:50], dropped_count=len(extra), rule=f"top_k = {top_k}"))
        kept = kept[:top_k]

    hits: list[Hit] = []
    trace_hits: list[TraceHit] = []
    for i, (c, score, ranks, view) in enumerate(kept):
        label = format_label(label_start + i)
        r = view.row
        kb = by_id[r.kb_id]
        content = r.content
        window = req.neighbors or (kb.retrieval.neighbor_window if kb.retrieval.parent_expansion == "neighbors" else 0)
        if window:
            content = joined(await neighbours(r, window))
        elif kb.retrieval.parent_expansion == "section":
            content = joined(await section(r, kb.retrieval.max_context_tokens))
        scores = {"lexical": float(c.score)}  # each chunk comes from one keyword group
        cite = citation(label, view)
        hits.append(Hit(label=label, chunk_id=r.chunk_id, doc_id=r.doc_id, kb_id=r.kb_id, content=content,
                        context=r.context or "", score=float(score), scores=scores, metadata=dict(r.meta or {}),
                        citation=cite))
        trace_hits.append(TraceHit(
            label=label, chunk_id=r.chunk_id, doc_id=r.doc_id, doc_sha256=r.doc_sha256, doc_version=r.doc_version,
            snapshot_id=r.snapshot_id, kb_id=r.kb_id, path=r.path or "", page_start=r.page_start,
            page_end=r.page_end, heading_path=list(r.heading_path or []), snippet=cite.snippet,
            score=float(score), scores=scores, ranks=ranks))

    pipeline = {}
    for kb in kbs:
        snap = snapshot_of.get(kb.id)
        if snap is None:
            continue
        details = info.get(snap) or {}
        pipeline[kb.id] = KbPipeline(
            snapshot_id=snap, embed_model=details.get("embed_model"), embed_dims=details.get("embed_dims"),
            chunker_version=details.get("chunker_version"),
            fusion=FusionParams(k=kb.fusion.rrf_k, weights=dict(kb.fusion.weights)), overfetch=kb.fusion.overfetch,
            rerank_model=None, top_k=top_k, score_threshold=threshold, parent_expansion=kb.retrieval.parent_expansion)

    total_ms = int((time.monotonic() - t0) * 1000)
    trace_dict = {
        "query_id": query_id, "created_at": created_at, "origin": origin.model_dump(),
        "request": TraceRequest(
            query=req.query, kb_ids=list(req.kb_ids), filters=to_canonical(node), path_prefix=prefix, top_k=top_k,
            rerank=req.rerank, score_threshold=threshold, neighbors=req.neighbors, mode=req.mode,
            pinned_snapshots=pinned).model_dump(),
        "pipeline": {k: v.model_dump() for k, v in pipeline.items()},
        "index": IndexInfo.of(caps).model_dump(),
        "degraded": [d.model_dump() for d in degraded],
        "legs": [leg.model_dump() for leg in legs],
        "fusion": fusion.model_dump() if fusion else None,
        "cuts": [cut.model_dump() for cut in cuts],
        "hits": [h.model_dump() for h in trace_hits],
        "timings_ms": Timings(lexical=lexical_ms, total=total_ms).model_dump(),
        "replay": ReplayInfo(exact_possible=caps.search_mode == "exact").model_dump(),
    }
    trace = Trace.model_validate(fit(trace_dict))
    await trace_store.record(trace)
    return SearchResponse(query_id=query_id, hits=hits, degraded=[d.code for d in trace.degraded],
                          cost=CostSummary(), trace=trace if req.include_trace else None)
