"""The eval's judge-free scoring (V1, D168): relevance by quote, the scores, and five rankings from one search."""
from __future__ import annotations

import math

import pytest

from _fakes.knowledge_corpus import CONTRACTS, REPORTS, index_fixture, save_bases
from _fakes.knowledge_db import knowledge_store  # noqa: F401
from core.knowledge import evals_metrics as EM
from core.knowledge import seams
from core.knowledge.api_models import SearchRequest
from core.knowledge.kb_settings import KnowledgeBase
from core.knowledge.search import search
from core.knowledge.testing.fakes import FakeReranker, HashingEmbedder
from core.knowledge.trace import Origin, Trace

ON = {"knowledge_enabled": True}
SHA = "a" * 64


def _passage(chunk_id="kch_1", *, doc_id="kdoc_1", sha=SHA, start=0, end=100, content="", attachment=None):
    return EM.Passage(chunk_id=chunk_id, doc_id=doc_id, doc_sha256=sha, char_start=start, char_end=end,
                      content=content, attachment=attachment)


def _evidence(*, doc_id="kdoc_1", sha=SHA, start=40, end=60, quote="the renewal happens each year in november",
              attachment=None):
    return {"doc_id": doc_id, "doc_sha256": sha, "char_start": start, "char_end": end, "quote": quote,
            "attachment": attachment}


# ── relevance ─────────────────────────────────────────────────────────────────

class TestRelevance:
    def test_the_same_version_covering_half_the_span_holds_it(self):
        ev = _evidence(start=40, end=60)
        assert EM.covers(_passage(start=0, end=50), ev)        # 10 of 20 characters
        assert not EM.covers(_passage(start=0, end=49), ev)    # 9 of 20
        assert EM.covers(_passage(start=45, end=300), ev)

    def test_another_version_or_attachment_needs_the_quote(self):
        ev = _evidence(start=0, end=100)
        assert not EM.covers(_passage(sha="b" * 64, start=0, end=100, content="something else"), ev)
        assert not EM.covers(_passage(attachment="invoice.txt", content="something else"), ev)
        assert EM.covers(_passage(sha="b" * 64, content="Note: The Renewal  happens each year in November."), ev)

    def test_the_quote_counts_in_any_document(self):
        """The same words in another file answer the question too: re-chunking and copies keep their credit."""
        ev = _evidence()
        assert EM.covers(_passage(doc_id="kdoc_9", sha=None, content="…the renewal happens each year in november…"),
                         ev)

    def test_a_short_quote_is_matched_by_its_span_only(self):
        ev = _evidence(quote="renewal", start=0, end=7)
        assert not EM.covers(_passage(doc_id="kdoc_9", content="renewal"), ev)
        assert EM.covers(_passage(start=0, end=200), ev)

    def test_relevance_names_what_each_passage_holds(self):
        evidence = [_evidence(start=0, end=10, quote="aaaaaaaaaaaaaaaaaaaaaaaaa"),
                    _evidence(doc_id="kdoc_2", start=0, end=10, quote="bbbbbbbbbbbbbbbbbbbbbbbbbb")]
        passages = {"x": _passage("x", start=0, end=10), "y": _passage("y", doc_id="kdoc_2", start=0, end=10),
                    "z": _passage("z", doc_id="kdoc_3", content="nothing")}
        assert EM.relevance(passages, evidence) == {"x": [0], "y": [1]}


# ── scores ────────────────────────────────────────────────────────────────────

class TestScores:
    def test_one_piece_of_evidence(self):
        s = EM.score(["a", "b", "c"], {"b": [0]}, 1)
        assert (s.first, s.found) == (2, 1)
        assert s.ndcg == pytest.approx(1 / math.log2(3))

    def test_each_piece_counts_once(self):
        s = EM.score(["a", "a2", "b"], {"a": [0], "a2": [0], "b": [1]}, 2)
        assert (s.first, s.found) == (1, 2)
        ideal = 1 + 1 / math.log2(3)
        assert s.ndcg == pytest.approx((1 + 1 / math.log2(4)) / ideal)

    def test_mrr_reads_past_the_scored_depth(self):
        order = [f"c{i}" for i in range(15)]
        s = EM.score(order, {"c12": [0]}, 1)
        assert (s.first, s.found, s.dcg) == (13, 0, 0.0)

    def test_aggregate(self):
        scores = [EM.score(["a", "b"], {"a": [0]}, 1),                       # rank 1
                  EM.score(["x", "y", "z", "w"], {"w": [0]}, 1),             # rank 4
                  EM.score(["p"], {}, 1),                                     # missed
                  EM.score(["m", "n"], {"m": [0], "n": [1]}, 2)]              # multi-hop, both found
        out = EM.aggregate(scores)
        assert out["cases"] == 4
        assert out["hit_at"] == {"1": 0.5, "3": 0.5, "5": 0.75, "10": 0.75}
        assert out["mrr"] == pytest.approx((1 + 0.25 + 0 + 1) / 4)
        assert out["recall_at_10"] == pytest.approx(0.75)
        assert out["complete_at_10"] == pytest.approx(0.75)
        assert EM.aggregate([])["cases"] == 0

    def test_ndcg_is_at_most_one(self):
        assert EM.score(["a"], {"a": [0, 1]}, 2).ndcg == 1.0


# ── one search, five rankings ─────────────────────────────────────────────────

pytestmark_db = pytest.mark.usefixtures("knowledge_store")


async def _indexed(**kb_updates) -> KnowledgeBase:
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    kb = KnowledgeBase.model_validate({**CONTRACTS.model_dump(), **kb_updates})
    await save_bases(kb, REPORTS)
    await index_fixture(kb, "pdf_contract", 1, "contracts/acme.pdf")
    await index_fixture(kb, "eml_with_attachment", 2, "contracts/mail/renewal.eml")
    await index_fixture(kb, "docx_contract", 4, "contracts/zurich.docx")
    return kb


async def _trace(kb: KnowledgeBase, query: str, **kw) -> Trace:
    response = await search(SearchRequest(query=query, kb_ids=[kb.id], include_trace=True, top_k=10, **kw),
                            origin=Origin(surface="eval"), settings=ON)
    return response.trace


@pytestmark_db
class TestRankings:
    async def test_hybrid_fused_again_is_the_searchs_own_fusion(self):
        kb = await _indexed()
        trace = await _trace(kb, "renewal invoice fees agreement")
        legs = EM.legs_of(trace, kb.id)
        snap = trace.pipeline[kb.id].snapshot_id
        assert legs.lexical is not None and legs.vector is not None and trace.fusion is not None
        assert EM.ranking(legs, "hybrid", kb_id=kb.id, snapshot_id=snap) == [
            c.chunk_id for c in trace.fusion.candidates]
        assert EM.ranking(legs, "lexical", kb_id=kb.id, snapshot_id=snap) == [
            c.chunk_id for c in next(leg for leg in trace.legs if leg.leg == "lexical").candidates]
        assert EM.ranking(legs, "served", kb_id=kb.id, snapshot_id=snap) == [h.chunk_id for h in trace.hits]

    async def test_the_searchs_own_weights_are_used(self):
        kb = await _indexed(fusion={"weights": {"lexical": 3.0, "vector": 0.5}, "rrf_k": 5})
        trace = await _trace(kb, "agreement fees renewal")
        legs = EM.legs_of(trace, kb.id)
        assert (legs.rrf_k, legs.weights) == (5, {"lexical": 3.0, "vector": 0.5})
        snap = trace.pipeline[kb.id].snapshot_id
        assert EM.ranking(legs, "hybrid", kb_id=kb.id, snapshot_id=snap) == [
            c.chunk_id for c in trace.fusion.candidates]
        keyword_only = EM.ranking(legs, "custom", kb_id=kb.id, snapshot_id=snap, rrf_k=60,
                                  weights={"lexical": 1.0, "vector": 0.0})
        assert keyword_only[:len(legs.lexical)] == EM.ranking(legs, "lexical", kb_id=kb.id, snapshot_id=snap)

    async def test_a_reranked_search_is_served_in_the_rerankers_order(self):
        seams.set_reranker(FakeReranker())
        kb = await _indexed(rerank={"enabled": True})
        trace = await _trace(kb, "renewal invoice")
        legs = EM.legs_of(trace, kb.id)
        assert legs.reranked and trace.rerank is not None
        assert EM.ranking(legs, "served", kb_id=kb.id, snapshot_id=trace.pipeline[kb.id].snapshot_id) == [
            r.chunk_id for r in trace.rerank.results][:len(trace.hits)]

    async def test_without_vectors_the_vector_rankings_are_not_available(self):
        kb = KnowledgeBase.model_validate(CONTRACTS.model_dump())
        await save_bases(kb)
        await index_fixture(kb, "pdf_contract", 1, "contracts/acme.pdf")  # no embedder: keyword only
        trace = await _trace(kb, "agreement fees")
        legs = EM.legs_of(trace, kb.id)
        assert legs.vector is None and legs.lexical is not None
        assert EM.ranking(legs, "hybrid", kb_id=kb.id, snapshot_id="s") is None
        assert EM.unavailable([legs], "vector").startswith("Not available")
        assert EM.unavailable([legs], "hybrid").startswith("Not available")
        assert EM.unavailable([legs], "lexical") is None
        assert EM.unavailable([legs], "served") is None

    async def test_kept_small_and_read_back(self):
        kb = await _indexed()
        trace = await _trace(kb, "renewal invoice fees agreement")
        legs = EM.legs_of(trace, kb.id)
        again = EM.CaseLegs.from_json(legs.to_json())
        assert again == legs
        assert len(set(legs.ids)) == len(legs.ids)


def _synthetic(legs: dict[str, list[str]], *, pipeline: dict | None = None, degraded=()) -> Trace:
    return Trace.model_validate({
        "trace_version": 1, "query_id": "kq_" + "0" * 32, "created_at": "2026-10-02T00:00:00+00:00",
        "origin": {"surface": "eval"},
        "request": {"query": "q", "kb_ids": ["kb_1"], "top_k": 10},
        "pipeline": {"kb_1": pipeline} if pipeline else {},
        "index": {"name": "x", "version": "1", "capabilities": {"name": "x", "version": "1"}},
        "degraded": [{"code": code} for code in degraded],
        "legs": [{"leg": kind, "scopes": [], "requested": 50, "returned": len(ids), "filtered": True, "exact": True,
                  "latency_ms": 1, "candidates": [{"chunk_id": c, "rank": i + 1, "score": 1.0}
                                                  for i, c in enumerate(ids)]} for kind, ids in legs.items()],
        "fusion": None, "rerank": None, "cuts": [], "hits": [], "costs": {}, "timings_ms": {}, "replay": {},
    })


def test_a_trimmed_trace_says_so():
    legs = EM.legs_of(_synthetic({"lexical": ["kch_a"]}, degraded=["truncated"]), "kb_1")
    assert legs.trimmed and legs.lexical == [0] and legs.vector is None


def test_hybrid_is_fused_with_the_traces_own_settings():
    """The legs disagree; equal weights tie and fall to the chunk id, the trace's weights do not."""
    pipeline = {"snapshot_id": "ksnap_1", "fusion": {"k": 5, "weights": {"lexical": 3.0, "vector": 0.5}},
                "overfetch": 50, "top_k": 10}
    legs = EM.legs_of(_synthetic({"lexical": ["kch_z", "kch_a"], "vector": ["kch_a", "kch_z"]},
                                 pipeline=pipeline), "kb_1")
    assert (legs.rrf_k, legs.weights) == (5, {"lexical": 3.0, "vector": 0.5})
    assert EM.ranking(legs, "hybrid", kb_id="kb_1", snapshot_id="ksnap_1") == ["kch_z", "kch_a"]
    assert EM.ranking(legs, "custom", kb_id="kb_1", snapshot_id="ksnap_1", rrf_k=60,
                      weights={"lexical": 1.0, "vector": 1.0}) == ["kch_a", "kch_z"]


def test_labels():
    assert EM.label("served", reranked=True) == "As served (reranked)"
    assert EM.label("custom", rrf_k=30, weights={"lexical": 2.0, "vector": 1.0}) == "Keyword 2 · vector 1 · k 30"
    assert EM.label("lexical") == "Keyword only"


def test_a_leg_weighted_zero_adds_nothing():
    """Found in the browser walk: a zero-weight leg's candidates were appended at score 0, in chunk-id order, and
    flattered hit@10 over the keyword leg alone."""
    pipeline = {"snapshot_id": "ksnap_1", "fusion": {"k": 60, "weights": {"lexical": 1.0, "vector": 1.0}},
                "overfetch": 50, "top_k": 10}
    legs = EM.legs_of(_synthetic({"lexical": ["kch_b", "kch_c"], "vector": ["kch_a", "kch_b", "kch_d"]},
                                 pipeline=pipeline), "kb_1")
    keyword_only = EM.ranking(legs, "custom", kb_id="kb_1", snapshot_id="ksnap_1", rrf_k=60,
                              weights={"lexical": 1.0, "vector": 0.0})
    assert keyword_only == EM.ranking(legs, "lexical", kb_id="kb_1", snapshot_id="ksnap_1") == ["kch_b", "kch_c"]
