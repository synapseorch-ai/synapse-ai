"""An in-memory `KnowledgeIndex` — the conformance suite's reference.

Exact BM25 (k1 1.2, b 0.75, over lowercased `\\w+` tokens) for the keyword leg,
exact cosine for the vector leg, `filters.evaluate` for filters, weighted RRF
(`reference_rrf`) for `search_hybrid`. Pass a reduced `IndexCapabilities` to
behave like a store without filter pushdown, without a keyword leg, or with
approximate search — the pipeline's degraded paths are tested against that.

Never a default index: it forgets everything when the process ends.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace

from core.knowledge.errors import IndexDimensionError
from core.knowledge.filters import evaluate, path_under
from core.knowledge.testing.fakes import tokens
from core.knowledge.types import (
    Candidate, HybridQuery, IndexCapabilities, IndexEntry, IndexStats, LegResult, LexicalQuery, Scope,
    VectorQuery,
)

FULL = IndexCapabilities(name="memory", version="1", lexical=True, vector=True, filter_pushdown=True,
                         per_leg_scores=True, search_mode="exact")
NO_PUSHDOWN = replace(FULL, filter_pushdown=False)
NO_LEXICAL = replace(FULL, lexical=False)
ANN = replace(FULL, search_mode="ann")
HYBRID_ONLY = replace(FULL, per_leg_scores=False)
VARIANTS: dict[str, IndexCapabilities] = {
    "full": FULL, "no_pushdown": NO_PUSHDOWN, "no_lexical": NO_LEXICAL, "ann": ANN, "hybrid_only": HYBRID_ONLY,
}

BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _ranked(scored: list[tuple[IndexEntry, float]], limit: int) -> tuple[Candidate, ...]:
    scored.sort(key=lambda p: (-p[1], p[0].chunk_id))
    return tuple(Candidate(chunk_id=e.chunk_id, doc_id=e.doc_id, kb_id=e.kb_id, snapshot_id=e.snapshot_id,
                           score=s, rank=i + 1) for i, (e, s) in enumerate(scored[:limit]))


def reference_rrf(legs: Sequence[LegResult], weights: Mapping[str, float], k: int = RRF_K,
                  limit: int | None = None) -> list[tuple[str, float]]:
    """Weighted reciprocal-rank fusion: Σ w_leg / (k + rank_leg); ties by chunk id ascending."""
    scores: dict[str, float] = {}
    for leg in legs:
        weight = float(weights.get(leg.leg, 1.0))
        for c in leg.candidates:
            scores[c.chunk_id] = scores.get(c.chunk_id, 0.0) + weight / (k + c.rank)
    fused = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
    return fused[:limit] if limit is not None else fused


class InMemoryKnowledgeIndex:
    def __init__(self, capabilities: IndexCapabilities = FULL):
        self.capabilities = capabilities
        self._rows: dict[tuple[str, str], IndexEntry] = {}

    # ── helpers ────────────────────────────────────────────────────────────────

    def _mine(self) -> list[IndexEntry]:
        tenant = _tenant()
        return [e for (t, _), e in self._rows.items() if t == tenant]

    def _in(self, scopes: Sequence[Scope]) -> list[IndexEntry]:
        wanted = {(s.kb_id, s.snapshot_id) for s in scopes}
        return [e for e in self._mine() if (e.kb_id, e.snapshot_id) in wanted]

    def _snapshot_dims(self, kb_id: str, snapshot_id: str) -> int | None:
        for e in self._mine():
            if e.kb_id == kb_id and e.snapshot_id == snapshot_id and e.vector is not None:
                return len(e.vector)
        return None

    def _delete(self, predicate) -> int:
        tenant = _tenant()
        doomed = [key for key, e in self._rows.items() if key[0] == tenant and predicate(e)]
        for key in doomed:
            del self._rows[key]
        return len(doomed)

    # ── writes ─────────────────────────────────────────────────────────────────

    async def upsert(self, entries: Sequence[IndexEntry]) -> int:
        tenant = _tenant()
        widths: dict[tuple[str, str], int] = {}
        for e in entries:  # validate the whole batch before writing any of it
            if e.vector is None:
                continue
            key = (e.kb_id, e.snapshot_id)
            if self.capabilities.max_dims and len(e.vector) > self.capabilities.max_dims:
                raise IndexDimensionError(f"{len(e.vector)} dimensions; this index holds at most "
                                          f"{self.capabilities.max_dims}")
            expected = widths.get(key) or self._snapshot_dims(*key)
            if expected is not None and len(e.vector) != expected:
                raise IndexDimensionError(f"snapshot {e.snapshot_id} holds {expected}-dimensional vectors, "
                                          f"not {len(e.vector)}")
            widths[key] = len(e.vector)
        for e in entries:
            self._rows[(tenant, e.chunk_id)] = e
        return len(entries)

    async def delete_chunks(self, scope: Scope, chunk_ids: Sequence[str]) -> int:
        ids = set(chunk_ids)
        return self._delete(lambda e: e.kb_id == scope.kb_id and e.snapshot_id == scope.snapshot_id
                            and e.chunk_id in ids)

    async def delete_document(self, kb_id: str, doc_id: str, *, snapshot_id: str | None = None) -> int:
        return self._delete(lambda e: e.kb_id == kb_id and e.doc_id == doc_id
                            and (snapshot_id is None or e.snapshot_id == snapshot_id))

    async def delete_prefix(self, kb_id: str, path_prefix: str) -> int:
        if not path_prefix.strip("/"):
            raise ValueError("delete_prefix needs a prefix; drop_kb deletes a whole knowledge base")
        return self._delete(lambda e: e.kb_id == kb_id and path_under(str(e.fields.get("path") or ""), path_prefix))

    async def drop_snapshot(self, scope: Scope) -> int:
        return self._delete(lambda e: e.kb_id == scope.kb_id and e.snapshot_id == scope.snapshot_id)

    async def drop_kb(self, kb_id: str) -> int:
        return self._delete(lambda e: e.kb_id == kb_id)

    # ── searches ───────────────────────────────────────────────────────────────

    def _filtered(self, pool: list[IndexEntry], node) -> tuple[list[IndexEntry], bool]:
        if node is None:
            return pool, False
        if not self.capabilities.filter_pushdown:
            return pool, False
        return [e for e in pool if evaluate(node, e.fields)], True

    async def search_lexical(self, query: LexicalQuery) -> LegResult:
        if not self.capabilities.lexical:
            return LegResult("lexical", (), filtered=False, exact=True, notes=("lexical_unavailable",))
        pool = self._in(query.scopes)
        terms = tokens(query.text)
        if not terms or not pool:
            return LegResult("lexical", (), filtered=query.filter is not None and self.capabilities.filter_pushdown,
                             exact=True, scanned=len(pool))
        docs = {e.chunk_id: Counter(tokens(e.text)) for e in pool}
        n = len(pool)
        avgdl = sum(sum(c.values()) for c in docs.values()) / n or 1.0
        df = {t: sum(1 for c in docs.values() if t in c) for t in set(terms)}
        idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in df}
        kept, filtered = self._filtered(pool, query.filter)
        scored = []
        for e in kept:
            counts = docs[e.chunk_id]
            dl = sum(counts.values()) or 1
            score = 0.0
            for t in terms:
                tf = counts.get(t, 0)
                if tf:
                    score += idf[t] * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
            if score > 0:
                scored.append((e, score))
        return LegResult("lexical", _ranked(scored, query.limit), filtered=filtered, exact=True, scanned=len(pool))

    async def search_vector(self, query: VectorQuery) -> LegResult:
        if not self.capabilities.vector:
            return LegResult("vector", (), filtered=False, exact=True, notes=("vector_unavailable",))
        pool = [e for e in self._in(query.scopes) if e.vector is not None]
        for e in pool:
            if len(e.vector) != len(query.vector):  # type: ignore[arg-type]
                raise IndexDimensionError(f"query has {len(query.vector)} dimensions; snapshot "
                                          f"{e.snapshot_id} holds {len(e.vector)}")  # type: ignore[arg-type]
        kept, filtered = self._filtered(pool, query.filter)
        scored = [(e, _cosine(e.vector, query.vector)) for e in kept]  # type: ignore[arg-type]
        exact = self.capabilities.search_mode == "exact"
        return LegResult("vector", _ranked(scored, query.limit), filtered=filtered, exact=exact,
                         scanned=len(pool), notes=() if exact else ("ann_approximate",))

    async def search_hybrid(self, query: HybridQuery) -> LegResult:
        lexical = await self.search_lexical(query.lexical)
        vector = await self.search_vector(query.vector)
        limit = max(query.lexical.limit, query.vector.limit)
        fused = reference_rrf([lexical, vector], query.weights, limit=limit)
        by_id = {c.chunk_id: c for c in (*lexical.candidates, *vector.candidates)}
        candidates = tuple(
            Candidate(chunk_id=cid, doc_id=by_id[cid].doc_id, kb_id=by_id[cid].kb_id,
                      snapshot_id=by_id[cid].snapshot_id, score=score, rank=i + 1)
            for i, (cid, score) in enumerate(fused)
        )
        return LegResult("hybrid", candidates, filtered=lexical.filtered and vector.filtered,
                         exact=lexical.exact and vector.exact, notes=(*lexical.notes, *vector.notes))

    async def stats(self, scope: Scope) -> IndexStats:
        rows = self._in([scope])
        vectors = [e for e in rows if e.vector is not None]
        return IndexStats(chunks=len(rows), vectors=len(vectors),
                          dims=len(vectors[0].vector) if vectors else None, bytes=None)  # type: ignore[arg-type]
