"""How a knowledge base's eval scores a search, with no judge (V1, D168).

**One search, five rankings.** A run searches each question once per snapshot,
hybrid, as the knowledge base serves it, and keeps the trace's legs. From that
one search come, for $0:

* `lexical` — the keyword leg's order;
* `vector` — the vector leg's order;
* `hybrid` — the two legs fused again with `search.fuse` and the trace's own
  fusion settings (equal to the trace's fusion; a test holds it);
* `served` — the passages the search returned, in their order: reranked when a
  rerank ran, cut at `top_k` and the threshold — what an agent is given;
* `custom` — the legs fused with weights a person asks for afterwards.

`CaseLegs` is that search kept small: each candidate's chunk id once, and each
ranking as positions into that list.

**Relevance is a quote, never a chunk id.** A chunk's id changes with every
snapshot and document version, so a case says where its answer is written: a
span of a document's parsed text (an attachment's own text when it names one)
and the quote in it. A passage is relevant to that evidence when it is the same
version of the same document and covers at least half the span, or when its
own text holds the quote — which survives re-chunking, an edited document and
the same words in another file. Each piece of evidence counts once, at the
first passage that holds it.

**The scores.** hit@k (the evidence is in the top k), MRR (1 / the first rank
holding any), nDCG@10 (each newly found piece of evidence a gain of 1, ideal =
all of them first, at most 1.0), recall@10 and complete@10 (every piece found:
what a multi-hop question needs).

Pure: no database and no network. `search.fuse` is imported where it is used.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.knowledge.api_models import EVAL_HIT_KS

#: Ranks past this are not scored (nDCG@10, recall@10, complete@10).
DEPTH = 10
#: A folded quote shorter than this is a term, not a passage: it is matched by its span only.
MIN_QUOTE_CHARS = 20

LABELS = {
    "lexical": "Keyword only",
    "vector": "Vector only",
    "hybrid": "Hybrid (fused)",
    "served": "As served",
    "custom": "Custom weights",
}


# ── relevance ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Passage:
    """A chunk as relevance reads it."""

    chunk_id: str
    doc_id: str
    doc_sha256: str | None
    char_start: int
    char_end: int
    content: str
    attachment: str | None = None
    path: str = ""

    @classmethod
    def of(cls, row: Any) -> Passage:
        """From a `KnowledgeChunkDB` row (or anything with its fields)."""
        from core.knowledge.chunker import ATTACHMENT_KEY

        meta = getattr(row, "meta", None) or {}
        return cls(chunk_id=row.chunk_id, doc_id=row.doc_id, doc_sha256=row.doc_sha256,
                   char_start=int(row.char_start), char_end=int(row.char_end), content=row.content or "",
                   attachment=meta.get(ATTACHMENT_KEY), path=getattr(row, "path", "") or "")


def _folded(text: str) -> str:
    from core.knowledge.citations import normalize

    return normalize(text)


def covers(passage: Passage, evidence: Mapping[str, Any], *, folded: str | None = None) -> bool:
    """Whether `passage` holds the answer `evidence` points at. `folded` is the passage's folded text, when the
    caller already has it."""
    if (passage.doc_id == evidence.get("doc_id") and passage.attachment == evidence.get("attachment")
            and passage.doc_sha256 and passage.doc_sha256 == evidence.get("doc_sha256")):
        start, end = int(evidence["char_start"]), int(evidence["char_end"])
        need = max(1, math.ceil((end - start) / 2))
        if min(end, passage.char_end) - max(start, passage.char_start) >= need:
            return True
    quote = _folded(str(evidence.get("quote") or ""))
    if len(quote) < MIN_QUOTE_CHARS:
        return False
    return quote in (folded if folded is not None else _folded(passage.content))


def relevance(passages: Mapping[str, Passage], evidence: Sequence[Mapping[str, Any]]) -> dict[str, list[int]]:
    """chunk id → the evidence (by index) it holds, for the passages that hold any."""
    out: dict[str, list[int]] = {}
    for chunk_id, passage in passages.items():
        folded = _folded(passage.content)
        held = [i for i, ev in enumerate(evidence) if covers(passage, ev, folded=folded)]
        if held:
            out[chunk_id] = held
    return out


# ── scoring one ranking ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaseScore:
    """One question's ranking, scored."""

    first: int | None  # the first rank (1-based) holding any evidence
    found: int  # pieces of evidence in the top DEPTH
    evidence: int
    dcg: float

    @property
    def ndcg(self) -> float:
        if not self.evidence:
            return 0.0
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(self.evidence, DEPTH)))
        return min(1.0, self.dcg / ideal)


def score(order: Sequence[str], rel: Mapping[str, Sequence[int]], evidence: int) -> CaseScore:
    """Score `order` (chunk ids, best first) against `rel` (chunk id → the evidence it holds)."""
    first = None
    seen: set[int] = set()
    dcg = 0.0
    for place, chunk_id in enumerate(order[:DEPTH]):
        held = set(rel.get(chunk_id, ()))
        if held and first is None:
            first = place + 1
        new = held - seen
        if new:
            dcg += len(new) / math.log2(place + 2)
            seen |= new
    if first is None:  # past the scored depth it still decides MRR
        for place, chunk_id in enumerate(order[DEPTH:], start=DEPTH):
            if rel.get(chunk_id):
                first = place + 1
                break
    return CaseScore(first=first, found=len(seen), evidence=evidence, dcg=dcg)


def aggregate(scores: Sequence[CaseScore]) -> dict[str, Any]:
    """The `EvalScores` of a group of questions (a body for `api_models.EvalScores`)."""
    n = len(scores)
    if not n:
        return {"cases": 0, "hit_at": {}, "mrr": 0.0, "ndcg_at_10": 0.0, "recall_at_10": 0.0, "complete_at_10": 0.0}

    def mean(values) -> float:
        return round(sum(values) / n, 6)

    return {
        "cases": n,
        "hit_at": {str(k): mean(1.0 if s.first is not None and s.first <= k else 0.0 for s in scores)
                   for k in EVAL_HIT_KS},
        "mrr": mean(1.0 / s.first if s.first else 0.0 for s in scores),
        "ndcg_at_10": mean(s.ndcg for s in scores),
        "recall_at_10": mean(s.found / s.evidence if s.evidence else 0.0 for s in scores),
        "complete_at_10": mean(1.0 if s.evidence and s.found >= s.evidence else 0.0 for s in scores),
    }


# ── one search, kept small ────────────────────────────────────────────────────

@dataclass
class CaseLegs:
    """One question's search in one snapshot: its candidates once, each ranking as positions into them."""

    query_id: str
    ids: list[str] = field(default_factory=list)
    lexical: list[int] | None = None
    vector: list[int] | None = None
    fused: list[int] | None = None  # the search's own fusion, for checking
    served: list[int] = field(default_factory=list)
    rrf_k: int = 60
    weights: dict[str, float] = field(default_factory=dict)  # by leg kind
    reranked: bool = False
    trimmed: bool = False
    degraded: list[str] = field(default_factory=list)

    def chunk_ids(self, positions: Sequence[int] | None) -> list[str] | None:
        return None if positions is None else [self.ids[i] for i in positions]

    def to_json(self) -> dict[str, Any]:
        return {"q": self.query_id, "ids": self.ids, "lexical": self.lexical, "vector": self.vector,
                "fused": self.fused, "served": self.served, "k": self.rrf_k, "w": self.weights,
                "reranked": self.reranked, "trimmed": self.trimmed, "degraded": self.degraded}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CaseLegs:
        return cls(query_id=data["q"], ids=list(data.get("ids") or []), lexical=data.get("lexical"),
                   vector=data.get("vector"), fused=data.get("fused"), served=list(data.get("served") or []),
                   rrf_k=int(data.get("k") or 60), weights=dict(data.get("w") or {}),
                   reranked=bool(data.get("reranked")), trimmed=bool(data.get("trimmed")),
                   degraded=list(data.get("degraded") or []))


def legs_of(trace: Any, kb_id: str) -> CaseLegs:
    """A single knowledge base's search trace (`trace.Trace`), kept small."""
    ids: list[str] = []
    where: dict[str, int] = {}

    def at(chunk_id: str) -> int:
        if chunk_id not in where:
            where[chunk_id] = len(ids)
            ids.append(chunk_id)
        return where[chunk_id]

    by_kind: dict[str, list[list[int]]] = {}
    for leg in trace.legs:
        by_kind.setdefault(leg.leg, []).append([at(c.chunk_id) for c in leg.candidates])
    fused = [at(c.chunk_id) for c in trace.fusion.candidates] if trace.fusion else None
    served = [at(h.chunk_id) for h in trace.hits]
    pipe = trace.pipeline.get(kb_id)
    codes = [d.code for d in trace.degraded]
    return CaseLegs(
        query_id=trace.query_id, ids=ids,
        lexical=by_kind["lexical"][0] if len(by_kind.get("lexical", [])) == 1 else None,
        vector=by_kind["vector"][0] if len(by_kind.get("vector", [])) == 1 else None,
        fused=fused, served=served,
        rrf_k=pipe.fusion.k if pipe else 60, weights=dict(pipe.fusion.weights) if pipe else {},
        reranked=trace.rerank is not None, trimmed="truncated" in codes, degraded=codes)


def refuse(legs: CaseLegs, *, kb_id: str, snapshot_id: str, rrf_k: int,
           weights: Mapping[str, float]) -> list[str] | None:
    """The two legs fused by `search.fuse` with `rrf_k` and `weights` (by leg kind); None without both legs. A leg
    weighted 0 is left out, so keyword 1 / vector 0 ranks as the keyword leg alone."""
    from core.knowledge.kb_settings import Fusion
    from core.knowledge.search import fuse
    from core.knowledge.types import Candidate

    if legs.lexical is None or legs.vector is None:
        return None
    # A leg weighted 0 adds nothing: its candidates would otherwise trail the list at score 0, in chunk-id order.
    named = [(kind, order) for kind, order in (("lexical", legs.lexical), ("vector", legs.vector))
             if float(weights.get(kind, 1.0)) > 0]
    results = [(kind, kind, [Candidate(chunk_id=legs.ids[i], doc_id="", kb_id=kb_id, snapshot_id=snapshot_id,
                                       score=0.0, rank=place + 1) for place, i in enumerate(order)])
               for kind, order in named]
    fusion = Fusion(rrf_k=rrf_k, weights={"lexical": 1.0, "vector": 1.0, **dict(weights)})
    return [c.chunk_id for c, _, _ in fuse(results, {kb_id: fusion})]


def ranking(legs: CaseLegs, config: str, *, kb_id: str, snapshot_id: str,
            rrf_k: int | None = None, weights: Mapping[str, float] | None = None) -> list[str] | None:
    """The chunk ids one configuration ranks, best first; None when this search cannot say."""
    if config == "lexical":
        return legs.chunk_ids(legs.lexical)
    if config == "vector":
        return legs.chunk_ids(legs.vector)
    if config == "served":
        return legs.chunk_ids(legs.served)
    if config == "hybrid":
        return refuse(legs, kb_id=kb_id, snapshot_id=snapshot_id, rrf_k=legs.rrf_k, weights=legs.weights)
    if config == "custom":
        return refuse(legs, kb_id=kb_id, snapshot_id=snapshot_id, rrf_k=rrf_k or legs.rrf_k,
                      weights=weights or legs.weights)
    raise ValueError(f"no configuration {config!r}")


def unavailable(legs: Sequence[CaseLegs], config: str) -> str | None:
    """Why `config` cannot be scored over these searches, or None."""
    if not legs:
        return "No question was searched."
    if config in ("vector", "hybrid", "custom") and all(c.vector is None for c in legs):
        why = "nothing in this index is embedded" if any("vector_unavailable" in c.degraded for c in legs) else \
            "the searches had no vector leg"
        return f"Not available: {why}."
    if config in ("lexical", "hybrid", "custom") and all(c.lexical is None for c in legs):
        return "Not available: the searches had no keyword leg."
    return None


def label(config: str, *, reranked: bool = False, rrf_k: int | None = None,
          weights: Mapping[str, float] | None = None) -> str:
    if config == "served" and reranked:
        return "As served (reranked)"
    if config == "custom" and weights is not None:
        lex, vec = weights.get("lexical", 1.0), weights.get("vector", 1.0)
        return f"Keyword {lex:g} · vector {vec:g} · k {rrf_k or 60}"
    return LABELS[config]
