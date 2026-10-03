"""Search: a question over one or more knowledge bases, answered with labelled passages and a trace (B1a, D158;
the vector leg, fusion and rerank: B2a, D163).

One function serves every surface — the agent tool, the Retrieve step, the API,
MCP, evals and the test-search panel — so they cannot drift apart:

    await search(SearchRequest(query=..., kb_ids=[...]), origin=Origin(surface="tool", ...))

**Pinned to snapshots.** Each KB is searched in its active snapshot, or the one
the request pins (`snapshot_ids`), so a search names exactly one index version
per KB, and the trace records which.

**Both legs, fused.** A `hybrid` request (the default) asks the keyword leg and
the vector leg, each for `fusion.overfetch` candidates, and fuses them by
weighted reciprocal rank, `Σ w_leg / (k + rank_leg)`, each chunk weighed by its
own knowledge base's `fusion` (`fuse`). The query is embedded as a *query*, once
per embedding model the snapshots were embedded with, with that model: a
snapshot's vectors are only ever compared with its own model's. A snapshot with
nothing embedded, an embedder that is missing or fails, an index with no vector
leg: the keyword leg answers alone and the trace says why (`vector_unavailable`,
`embed_failed`). Nothing degrades silently.

**Rerank.** When a reranker is configured and asked for (the request's `rerank`,
else the knowledge bases' `rerank.enabled`, else on wherever one is configured),
it reads the fused order's first `rerank.candidates` and its order is the
answer. One that fails leaves the fused order standing (`rerank_failed`); one
asked for with none configured is `rerank_unconfigured`. A score threshold
reads the reranker's score, or a single leg's: fused scores are not comparable.

**Costs.** The query's embedding and the rerank are priced calls: each is a cost
row (`ledger.record_costs`) attributed to the run or chat that searched, and
both are in the response's `cost` and the trace's `costs`. A knowledge base at
its monthly budget (B2b, D167 #3) is searched by keyword only — no query
embedding for its snapshots, and no rerank when the rerank would be charged to
it — and the trace says `budget_paused`.

**Filters and the path prefix run inside the index's query** when the index can
(`filter_pushdown`), so a top-k is never filled with rows the filter excludes;
otherwise the hits are filtered here and the trace says `post_filtered`.

**Languages.** Scopes are grouped by the keyword language their rows were
indexed with (the snapshot's), one keyword query per group; each group is a leg
of its own (`lexical:<language>`) in the fusion.

**Expansion.** `neighbors` (or a KB's `parent_expansion`) widens a hit's
`content` with the chunks around it — the same document, the same snapshot —
or with its whole section (the virtual parent: the run of chunks sharing its
heading path). The hit's citation keeps its own span.

**Labels** run `[K{label_start}]`, `[K{label_start+1}]`, …; the agent tool passes
where its turn's previous searches stopped.

Every search writes its trace to `knowledge_queries` (`trace_store`), with the
query vector beside it (never inside it) when one model embedded it; a trace that
cannot be written is logged, never raised.
"""
from __future__ import annotations

import hashlib
import importlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.knowledge import vectors
from core.knowledge.agent_contract import format_label
from core.knowledge.api_models import Citation, CostSummary, Hit, SearchRequest, SearchResponse
from core.knowledge.chunker import ATTACHMENT_KEY, estimate_tokens
from core.knowledge.config import knowledge_enabled, knowledge_settings
from core.knowledge.errors import EmbedderUnavailable, KnowledgeDisabled, KnowledgeError
from core.knowledge.filters import BUILTINS, evaluate, parse_filter, path_under, to_canonical
from core.knowledge.ids import QUERY_PREFIX, new_id
from core.knowledge.kb_settings import COLLECTION, Fusion, KnowledgeBase
from core.knowledge.ledger import CostRow, record_costs
from core.knowledge.snapshots import active_snapshots, snapshot_info
from core.knowledge.telemetry import KNOWLEDGE_KB_COUNT, SPAN_SEARCH
from core.knowledge.textsearch import normalize_language
from core.knowledge.trace import (
    SNIPPET_MAX, CostLine, Cut, Degraded, FusedId, FusionParams, FusionTrace, IndexInfo, KbPipeline, LegTrace,
    Origin, RankedId, RerankTrace, ReplayInfo, Timings, Trace, TraceCosts, TraceHit, TraceRequest, fit,
)
from core.knowledge.types import (
    Attribution, Candidate, EmbeddingSpec, LexicalQuery, RerankSpec, Scope, Usage, VectorQuery,
)
from core.log import get_logger
from core.store.models import KnowledgeChunkDB, KnowledgeDocumentDB
from core.telemetry import spans as _spans

_log = get_logger(__name__)

_C = KnowledgeChunkDB.__table__
_D = KnowledgeDocumentDB.__table__
RRF_K = 60
MAX_LABEL = 999
#: The most candidates one rerank reads (Cohere's and our gateway's cap).
MAX_RERANK = 100
#: Characters of one candidate a reranker reads.
RERANK_TEXT_CHARS = 4000


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


async def load_chunks(chunk_ids: Sequence[str], *, live_only: bool = False) -> dict[str, ChunkView]:
    """The current tenant's chunk rows by chunk id (absent ids are absent).

    `live_only` leaves out the chunks of a document being deleted (F335): search reads its answers through it, so
    an index that cannot see a document's status never makes one of them an answer."""
    from core.store import session

    if not chunk_ids:
        return {}
    out: dict[str, ChunkView] = {}
    ids = list(chunk_ids)
    live = (_D.c.status != "deleting",) if live_only else ()
    async with session() as s:
        for i in range(0, len(ids), 500):
            rows = (await s.execute(
                select(KnowledgeChunkDB, _D.c.source_url)
                .join(_D, _D.c.id == _C.c.doc_id)
                .where(_C.c.tenant_id == _tenant(), _C.c.chunk_id.in_(ids[i:i + 500]), *live))).all()
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

def fuse(legs: Sequence[tuple[str, str, Sequence[Candidate]]],
         fusion_of: Mapping[str, Fusion]) -> list[tuple[Candidate, float, dict[str, int]]]:
    """Weighted reciprocal-rank fusion, as the trace contract freezes it: `Σ w_leg / (k + rank_leg)` over the legs
    a chunk came back from (a leg it is missing from adds 0), ties by chunk id ascending.

    `legs` are (name, kind, candidates): the name is what ranks and weights are recorded under
    ("lexical:english", "vector"), the kind ("lexical" or "vector") is what a knowledge base weighs. Each chunk is
    weighed by its own knowledge base's `fusion` — its `rrf_k` and its weight for the leg's kind (a kind it does
    not weigh counts 1.0, as `testing.reference_index.reference_rrf` does).
    """
    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    first: dict[str, Candidate] = {}
    for name, kind, candidates in legs:
        for c in candidates:
            settings = fusion_of[c.kb_id]
            weight = float(settings.weights.get(kind, 1.0))
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + weight / (settings.rrf_k + c.rank)
            ranks.setdefault(c.chunk_id, {})[name] = c.rank
            first.setdefault(c.chunk_id, c)
    ordered = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
    return [(first[cid], score, ranks[cid]) for cid, score in ordered]


def _usage_source(origin: Origin) -> str:
    """The usage row's `source` for a search's costs: the run's or the chat's own, so they group with what the search
    served."""
    if origin.run_id or origin.surface == "step":
        return "orchestration"
    return {"tool": "chat", "eval": "evals", "ui": "knowledge"}.get(origin.surface, origin.surface)


def _note(degraded: list[Degraded], code: str, detail: str | None = None) -> None:
    """Say a degradation once (the first detail wins)."""
    if code not in {d.code for d in degraded}:
        degraded.append(Degraded(code=code, detail=detail))


def _rerank_text(view: ChunkView) -> str:
    """What a reranker reads of a candidate: its context line and its text, as the legs searched them."""
    r = view.row
    text = f"{r.context} {r.content}".strip() if r.context else r.content
    return text[:RERANK_TEXT_CHARS]


def _settings(settings: Mapping[str, Any] | None) -> dict:
    if settings is not None:
        return dict(settings)
    from core.config import load_settings
    return dict(load_settings())


def _span_attributes(req: SearchRequest, **_kw) -> dict:
    return {KNOWLEDGE_KB_COUNT: len(req.kb_ids)}


@_spans.traced_coroutine(SPAN_SEARCH, _span_attributes)
async def search(req: SearchRequest, *, origin: Origin, label_start: int = 1,
                 settings: dict | None = None) -> SearchResponse:
    """Search `req.kb_ids` for `req.query`. See the module docstring."""
    from core.knowledge import trace_store
    from core.knowledge.seams import get_knowledge_embedder, get_knowledge_index, get_reranker

    if not knowledge_enabled(settings):
        raise KnowledgeDisabled("Knowledge is switched off.")
    cfg = _settings(settings)
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

    # ── the plan: which legs run, and whether a reranker reads their answer ──
    groups: dict[str, list[Scope]] = {}
    vector_groups: dict[tuple[str, int], list[Scope]] = {}
    unembedded: list[str] = []
    for kb in kbs:
        snap = snapshot_of.get(kb.id)
        if snap is None:
            continue
        details = info.get(snap) or {}
        lang = normalize_language(details.get("language") or kb.language)
        groups.setdefault(lang, []).append(Scope(kb.id, snap))
        if details.get("embed_model") and details.get("embed_dims"):
            vector_groups.setdefault((details["embed_model"], int(details["embed_dims"])), []).append(Scope(kb.id, snap))
        else:
            unembedded.append(kb.name)
    if req.mode == "lexical":
        vector_groups = {}
    else:
        if not caps.vector:
            _note(degraded, "vector_unavailable", "the search index has no vector leg")
            vector_groups = {}
        elif unembedded:
            _note(degraded, "vector_unavailable", "nothing embedded yet in " + ", ".join(unembedded))
    if req.mode == "vector":
        groups = {}
    elif groups and not caps.lexical:
        _note(degraded, "lexical_unavailable")
        groups = {}

    ks = knowledge_settings(cfg)
    rerank_model = next((kb.rerank.model for kb in kbs if kb.rerank.model), None) or ks.rerank_model
    rerank_cfg = {**cfg, "knowledge_rerank_model": rerank_model}
    reranker = get_reranker(rerank_cfg)
    if req.rerank is not None:
        rerank_on = req.rerank
    elif any(kb.rerank.enabled for kb in kbs):
        rerank_on = True
    elif kbs and all(kb.rerank.enabled is False for kb in kbs):
        rerank_on = False
    else:  # the deployment's default: on where a reranker is configured (OSS: none unless someone set one)
        rerank_on = reranker is not None
    if rerank_on and reranker is None:
        _note(degraded, "rerank_unconfigured", "no rerank model is configured")
        rerank_on = False

    # A knowledge base at its monthly budget searches by keyword only ($0), and pays for no rerank (B2b, D167 #3).
    from core.knowledge.budgets import capped as _capped
    from core.knowledge.rerankers import route_for

    over: set[str] = set()
    if vector_groups or rerank_on:  # nothing else in a search costs money
        route = route_for(rerank_model or "") if rerank_on else None
        over = await _capped(kbs, query=req.query, models={
            kb.id: (info.get(snapshot_of.get(kb.id) or "") or {}).get("embed_model") for kb in kbs},
            rerank_usd=float(getattr(route, "usd_per_search", None) or 0.0))
    if over:
        _note(degraded, "budget_paused", "monthly budget reached: " + ", ".join(by_id[k].name for k in sorted(over)))
        vector_groups = {key: kept for key, scopes in vector_groups.items()
                         if (kept := [s for s in scopes if s.kb_id not in over])}

    deep = len(groups) + len(vector_groups) > 1 or rerank_on
    fetch = max(top_k, max((kb.fusion.overfetch for kb in kbs), default=top_k)) if deep else top_k
    if not (caps.filter_pushdown or (node is None and prefix is None)):
        fetch = min(fetch * 4, 200)

    legs: list[LegTrace] = []
    results: list[tuple[str, str, Sequence[Candidate]]] = []
    leg_scores: dict[str, dict[str, float]] = {}
    usages: list[tuple[Usage, Attribution]] = []
    timings = {"embed": 0, "lexical": 0, "vector": 0, "fuse": 0, "rerank": 0}

    def attribution(scope: Scope) -> Attribution:
        return Attribution(kb_id=scope.kb_id, query_id=query_id, snapshot_id=scope.snapshot_id, run_id=origin.run_id,
                           session_id=origin.session_id, agent_id=origin.agent_id, source=_usage_source(origin))

    def take(name: str, kind: str, leg: Any, scopes: Sequence[Scope], ms: int) -> None:
        results.append((name, kind, leg.candidates))
        for c in leg.candidates:
            leg_scores.setdefault(c.chunk_id, {})[name] = float(c.score)
        legs.append(LegTrace(
            leg=kind, scopes=[{"kb_id": s.kb_id, "snapshot_id": s.snapshot_id} for s in scopes], requested=fetch,
            returned=len(leg.candidates), filtered=leg.filtered, exact=leg.exact, latency_ms=ms,
            candidates=[RankedId(chunk_id=c.chunk_id, rank=c.rank, score=c.score) for c in leg.candidates]))
        for code in leg.notes:
            _note(degraded, code)
        if (node is not None or prefix) and not leg.filtered:
            _note(degraded, "post_filtered")

    for lang, scopes in sorted(groups.items()):
        t_leg = time.monotonic()
        leg = await index.search_lexical(LexicalQuery(tuple(scopes), req.query, lang, node, fetch, path_prefix=prefix))
        ms = int((time.monotonic() - t_leg) * 1000)
        timings["lexical"] += ms
        take(f"lexical:{lang}" if len(groups) > 1 else "lexical", "lexical", leg, scopes, ms)

    query_blob: bytes | None = None
    if vector_groups:
        try:
            embedder = get_knowledge_embedder()
        except EmbedderUnavailable as exc:
            _note(degraded, "vector_unavailable", str(exc))
            vector_groups = {}
        for (model, dims), scopes in sorted(vector_groups.items()):
            who = attribution(scopes[0])
            t_embed = time.monotonic()
            try:
                answer = await embedder.embed([req.query], kind="query", spec=EmbeddingSpec(model=model, dims=dims),
                                              settings=cfg, attribution=who)
            except EmbedderUnavailable as exc:
                _note(degraded, "vector_unavailable", f"{model}: {exc}")
                continue
            except KnowledgeError as exc:  # failed, refused, or a wallet that is empty: keyword only, said so
                _note(degraded, "embed_failed", f"{model}: {exc}"[:300])
                continue
            finally:
                timings["embed"] += int((time.monotonic() - t_embed) * 1000)
            usages.append((answer.usage, who))
            vector = answer.vectors[0]
            if len(vector_groups) == 1:
                query_blob = vectors.pack(vector)
            t_leg = time.monotonic()
            leg = await index.search_vector(VectorQuery(tuple(scopes), vector, node, fetch, path_prefix=prefix))
            ms = int((time.monotonic() - t_leg) * 1000)
            timings["vector"] += ms
            take("vector" if len(vector_groups) == 1 else f"vector:{model}", "vector", leg, scopes, ms)

    # ── fusion ──
    t_fuse = time.monotonic()
    fusion = None
    if len(results) > 1:
        fused = fuse(results, {kb.id: kb.fusion for kb in kbs})
        first = kbs[0].fusion
        fusion = FusionTrace(
            params=FusionParams(k=first.rrf_k, weights={name: float(first.weights.get(kind, 1.0))
                                                        for name, kind, _ in results}),
            candidates=[FusedId(chunk_id=c.chunk_id, score=score, ranks=ranks) for c, score, ranks in fused])
        ranked = fused
    elif results:
        name = results[0][0]
        ranked = [(c, c.score, {name: c.rank}) for c in results[0][2]]
    else:
        ranked = []
    timings["fuse"] = int((time.monotonic() - t_fuse) * 1000)

    views = await load_chunks([c.chunk_id for c, _, _ in ranked], live_only=True)
    candidates: list[tuple[Candidate, float, dict[str, int], ChunkView]] = []
    dropped_filter: list[str] = []
    post_filtered = any(code.code == "post_filtered" for code in degraded)
    for c, score, ranks in ranked:
        view = views.get(c.chunk_id)
        if view is None:  # deleted between the index's answer and this read, or its document is being deleted
            continue
        if post_filtered:
            f = view.fields()
            if not evaluate(node, f) or (prefix and not path_under(str(f.get("path") or ""), prefix)):
                dropped_filter.append(c.chunk_id)
                continue
        candidates.append((c, score, ranks, view))
    if dropped_filter:
        cuts.append(Cut(stage="post_filter", dropped=dropped_filter[:50], dropped_count=len(dropped_filter),
                        rule="filter or path prefix applied after the index"))

    # ── rerank ──
    rerank_trace = None
    reranked = False
    reranked_by: str | None = None
    if rerank_on and candidates and candidates[0][0].kb_id in over:
        rerank_on = False  # the rerank would be charged to a base at its cap: the fused order stands
    if rerank_on and candidates:
        window = min(max(top_k, max(kb.rerank.candidates for kb in kbs)),
                     int(getattr(reranker, "max_documents", MAX_RERANK) or MAX_RERANK), MAX_RERANK)
        pool, beyond = candidates[:window], candidates[window:]
        top_n = min(len(pool), max(top_k, max(kb.rerank.top_n for kb in kbs)))
        who = attribution(Scope(pool[0][0].kb_id, pool[0][0].snapshot_id))
        t_rerank = time.monotonic()
        try:
            answer = await reranker.rerank(req.query, [_rerank_text(view) for *_, view in pool],
                                           spec=RerankSpec(model=rerank_model, top_n=top_n), settings=rerank_cfg,
                                           attribution=who)
        except Exception as exc:  # noqa: BLE001 — any reranker's failure keeps the fused order, and says why
            _note(degraded, "rerank_failed", str(exc)[:300])
        else:
            usages.append((answer.usage, who))
            reordered = []
            seen: set[int] = set()
            for place, (i, score) in enumerate(answer.ranking):
                if not 0 <= i < len(pool) or i in seen:
                    continue
                seen.add(i)
                c, _, ranks, view = pool[i]
                reordered.append((c, float(score), {**ranks, "rerank": place + 1}, view))
            left = [cand[0].chunk_id for i, cand in enumerate(pool) if i not in seen] + [b[0].chunk_id for b in beyond]
            if left:
                cuts.append(Cut(stage="top_k", dropped=left[:50], dropped_count=len(left),
                                rule=f"the reranker kept {len(reordered)} of {len(pool)}"))
            candidates = reordered
            reranked = True
            reranked_by = answer.model
            rerank_trace = RerankTrace(
                model=answer.model, cost_source=answer.usage.cost_source, input_count=len(pool),
                latency_ms=int((time.monotonic() - t_rerank) * 1000),
                results=[RankedId(chunk_id=c.chunk_id, rank=n + 1, score=score)
                         for n, (c, score, _, _) in enumerate(reordered)])
        timings["rerank"] = int((time.monotonic() - t_rerank) * 1000)

    kept = []
    dropped_threshold: list[str] = []
    for c, score, ranks, view in candidates:
        # A threshold reads a reranker's score, or a single leg's; fused RRF scores are not comparable.
        if threshold is not None and (reranked or fusion is None) and score < threshold:
            dropped_threshold.append(c.chunk_id)
            continue
        kept.append((c, score, ranks, view))
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
        scores = dict(leg_scores.get(c.chunk_id, {}))
        if reranked:
            scores["rerank"] = float(score)
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
            rerank_model=reranked_by, top_k=top_k, score_threshold=threshold,
            parent_expansion=kb.retrieval.parent_expansion)

    if usages:
        await record_costs([CostRow(u, a) for u, a in usages])
    charged = [u.charged_microusd for u, _ in usages if u.charged_microusd is not None]
    by_stage: dict[str, float] = {}
    for u, _ in usages:
        by_stage[u.stage] = by_stage.get(u.stage, 0.0) + u.est_usd
    total_est = round(sum(u.est_usd for u, _ in usages), 10)
    costs = TraceCosts(total_est_usd=total_est, total_charged_microusd=sum(charged) if charged else None,
                       rows=[CostLine(stage=u.stage, model=u.model, cost_source=u.cost_source, units=u.units,
                                      unit=u.unit, input_tokens=u.input_tokens, est_usd=u.est_usd,
                                      charged_microusd=u.charged_microusd) for u, _ in usages])

    replay_notes = []
    if len(vector_groups) > 1:
        replay_notes.append("several embedding models: the query vectors are not kept")
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
        "rerank": rerank_trace.model_dump() if rerank_trace else None,
        "cuts": [cut.model_dump() for cut in cuts],
        "hits": [h.model_dump() for h in trace_hits],
        "costs": costs.model_dump(),
        "timings_ms": Timings(total=total_ms, **timings).model_dump(),
        "replay": ReplayInfo(
            query_vector_sha256=hashlib.sha256(query_blob).hexdigest() if query_blob else None,
            exact_possible=caps.search_mode == "exact" and len(vector_groups) <= 1,
            notes=replay_notes).model_dump(),
    }
    trace = Trace.model_validate(fit(trace_dict))
    await trace_store.record(trace, query_embedding=query_blob)
    return SearchResponse(query_id=query_id, hits=hits, degraded=[d.code for d in trace.degraded],
                          cost=CostSummary(total_est_usd=total_est, total_charged_microusd=costs.total_charged_microusd,
                                           by_stage=by_stage),
                          trace=trace if req.include_trace else None)
