"""Search (B1a, D158): one call for every surface — labelled hits, citations, and a stored trace."""
from __future__ import annotations

import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from core.knowledge import seams, trace_store
from core.knowledge.api_models import SearchRequest
from core.knowledge.errors import KnowledgeDisabled, KnowledgeError
from core.knowledge.index_sql import SqlKnowledgeIndex
from core.knowledge.kb_settings import COLLECTION, KnowledgeBase, Retrieval
from core.knowledge.parsed import ParsedDocument
from core.knowledge.pipeline import index_document
from core.knowledge.pipeline_contract import IndexContext
from core.knowledge.search import joined, load_bases, search
from core.knowledge.snapshots import serving_snapshot
from core.knowledge.trace import FORBIDDEN_KEYS, Origin, Trace
from core.knowledge.types import Attribution, LegResult
from core.store import collections, session
from core.store.models import KnowledgeDocumentDB, KnowledgeQueryDB

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge" / "parsed"
ON = {"knowledge_enabled": True}
ORIGIN = Origin(surface="tool", run_id="run_1", session_id="sess_1", agent_id="agent_1", turn=1)
CONTRACTS = KnowledgeBase(id="kb_00000000000a", name="Contracts", folder="contracts")
REPORTS = KnowledgeBase(id="kb_00000000000b", name="Reports", folder="reports")


def _parsed(name: str) -> ParsedDocument:
    return ParsedDocument.model_validate_json((FIXTURES / f"{name}.json").read_text("utf-8"))


async def _save(*kbs: KnowledgeBase) -> None:
    await collections.save(COLLECTION, [kb.model_dump() for kb in kbs])


async def _index(kb: KnowledgeBase, fixture: str, n: int, path: str, metadata: dict | None = None) -> str:
    doc_id = "kdoc_" + f"{n:032x}"
    async with session() as s:
        s.add(KnowledgeDocumentDB(id=doc_id, tenant_id="default", kb_id=kb.id, path=path, source="upload",
                                  status="indexing", version=1, source_url=None))
    parsed = _parsed(fixture)
    await index_document(IndexContext(
        kb=kb, doc_id=doc_id, doc_version=1, sha256=parsed.source.sha256, path=path, source="upload",
        parsed=parsed, snapshot_id=await serving_snapshot(kb),
        attribution=Attribution(kb_id=kb.id, doc_id=doc_id), metadata=metadata or {}))
    return doc_id


async def _corpus() -> None:
    await _save(CONTRACTS, REPORTS)
    await _index(CONTRACTS, "pdf_contract", 1, "contracts/acme.pdf", {"doc_type": "contract", "region": "EU"})
    await _index(CONTRACTS, "eml_with_attachment", 2, "contracts/mail/renewal.eml", {"doc_type": "email"})
    await _index(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx", {"doc_type": "report"})


async def _search(**kw):
    kw.setdefault("kb_ids", [CONTRACTS.id])
    label_start = kw.pop("label_start", 1)
    return await search(SearchRequest(**kw), origin=ORIGIN, label_start=label_start, settings=ON)


# ── the answer ─────────────────────────────────────────────────────────────────

async def test_off_means_off():
    with pytest.raises(KnowledgeDisabled):
        await search(SearchRequest(query="x", kb_ids=[CONTRACTS.id]), origin=ORIGIN, settings={})


async def test_hits_are_labelled_cited_and_traced():
    await _corpus()
    response = await _search(query="hosting monthly fee", include_trace=True)

    assert response.hits and response.hits[0].label == "[K1]"
    top = response.hits[0]
    assert "Hosting | 1,200" in top.content and top.context.startswith("Document: ")
    assert top.metadata == {"doc_type": "contract", "region": "EU"}
    cite = top.citation
    assert cite.path == "contracts/acme.pdf" and cite.page_start == 2 and cite.source == "upload"
    assert cite.heading_path == ["Master Services Agreement", "Fees"]
    assert _parsed("pdf_contract").text[cite.char_start:cite.char_end] == top.content
    assert cite.sha256 == _parsed("pdf_contract").source.sha256 and cite.doc_version == 1
    assert len(cite.snippet) <= 200 and cite.indexed_at
    assert response.degraded == ["vector_unavailable"]

    trace = response.trace
    assert trace is not None and trace.query_id == response.query_id
    assert [h.label for h in trace.hits] == [h.label for h in response.hits]
    assert trace.index.name == "sql" and trace.legs[0].leg == "lexical" and trace.legs[0].filtered is False
    assert trace.pipeline[CONTRACTS.id].chunker_version == "structure-1"
    assert trace.origin.run_id == "run_1" and trace.replay.exact_possible
    assert not _keys(trace.model_dump(mode="json")) & FORBIDDEN_KEYS

    stored = await trace_store.get(response.query_id)
    assert stored == trace
    async with session() as s:
        row = (await s.execute(select(KnowledgeQueryDB))).scalar_one()
    assert (row.surface, row.run_id, row.hit_count, row.degraded) == ("tool", "run_1", len(response.hits), True)
    assert row.kb_ids == [CONTRACTS.id] and row.query_embedding is None


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


async def test_labels_continue_from_where_the_turn_stopped():
    await _corpus()
    response = await _search(query="renewal invoice fees", label_start=4)
    assert [h.label for h in response.hits] == [f"[K{4 + i}]" for i in range(len(response.hits))]
    assert response.trace is None, "the trace is stored, and returned only when asked"


async def test_the_filter_and_the_prefix_run_inside_the_query():
    await _corpus()
    everything = await _search(query="renewal invoice fees agreement")
    assert {h.metadata["doc_type"] for h in everything.hits} == {"contract", "email"}
    contracts = await _search(query="renewal invoice fees agreement", filters={"doc_type": "contract"},
                              include_trace=True)
    assert contracts.hits and {h.metadata["doc_type"] for h in contracts.hits} == {"contract"}
    assert contracts.trace.legs[0].filtered and contracts.trace.request.filters == {"doc_type": {"$eq": "contract"}}
    mail = await _search(query="renewal invoice fees agreement", path_prefix="contracts/mail")
    assert mail.hits and {h.citation.path for h in mail.hits} == {"contracts/mail/renewal.eml"}


async def test_an_attachment_is_cited_with_its_name():
    await _corpus()
    response = await _search(query="invoice 2025-117 due october")
    hit = next(h for h in response.hits if h.metadata.get("_attachment"))
    assert hit.metadata["_attachment"] == "invoice.txt" and hit.citation.heading_path == ["invoice.txt"]


async def test_top_k_and_the_threshold():
    await _corpus()
    one = await _search(query="renewal invoice fees agreement", top_k=1)
    assert len(one.hits) == 1
    none = await _search(query="renewal", score_threshold=1e9, include_trace=True)
    assert none.hits == [] and none.trace.cuts[0].stage == "threshold" and none.trace.cuts[0].dropped_count >= 1


async def test_several_knowledge_bases_and_languages_fuse_by_rank():
    english = CONTRACTS.model_copy(update={"language": "english"})
    await _save(english, REPORTS)
    await _index(english, "pdf_contract", 1, "contracts/acme.pdf")
    await _index(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx")
    response = await _search(query="agreements revenue", kb_ids=[english.id, REPORTS.id], include_trace=True)
    assert {h.kb_id for h in response.hits} == {english.id, REPORTS.id}, "'agreements' is stemmed in English"
    assert len(response.trace.legs) == 2 and response.trace.fusion is not None
    assert {tuple(h.ranks) for h in response.trace.hits} <= {("lexical:english",), ("lexical:simple",)}


# ── what a search refuses, and what it says ────────────────────────────────────

async def test_an_unknown_or_disabled_base_is_refused():
    await _save(CONTRACTS, REPORTS.model_copy(update={"status": "disabled"}))
    with pytest.raises(KnowledgeError) as unknown:
        await _search(query="x", kb_ids=["kb_000000000999"])
    assert unknown.value.code == "kb_not_found"
    with pytest.raises(KnowledgeError):
        await _search(query="x", kb_ids=[REPORTS.id])


async def test_a_base_with_nothing_indexed_answers_empty():
    await _save(CONTRACTS)
    response = await _search(query="anything")
    assert response.hits == []


async def test_a_pinned_snapshot_is_searched_and_an_unknown_one_refused():
    await _corpus()
    snap = await serving_snapshot(CONTRACTS)
    pinned = await _search(query="hosting", snapshot_ids={CONTRACTS.id: snap}, include_trace=True)
    assert pinned.hits and pinned.trace.request.pinned_snapshots == {CONTRACTS.id: snap}
    with pytest.raises(KnowledgeError) as err:
        await _search(query="hosting", snapshot_ids={CONTRACTS.id: "ksnap_" + "0" * 32})
    assert err.value.code == "snapshot_not_found"


async def test_a_vector_search_says_the_vector_leg_is_not_here_yet():
    await _corpus()
    response = await _search(query="hosting", mode="vector", include_trace=True)
    assert response.hits == [] and response.degraded == ["vector_unavailable"]
    lexical = await _search(query="hosting", mode="lexical")
    assert lexical.hits and lexical.degraded == []


async def test_an_index_without_pushdown_is_post_filtered_and_says_so():
    class NoPushdown(SqlKnowledgeIndex):
        capabilities = replace(SqlKnowledgeIndex.capabilities, filter_pushdown=False)

        async def search_lexical(self, query):
            leg = await super().search_lexical(replace(query, filter=None, path_prefix=None))
            return LegResult("lexical", leg.candidates, filtered=False, exact=True)

    await _corpus()
    seams.set_knowledge_index(NoPushdown())
    response = await _search(query="renewal invoice fees agreement", filters={"doc_type": "email"},
                             include_trace=True)
    assert response.hits and {h.metadata["doc_type"] for h in response.hits} == {"email"}
    assert "post_filtered" in response.degraded
    assert any(c.stage == "post_filter" for c in response.trace.cuts)


# ── expansion ──────────────────────────────────────────────────────────────────

async def test_neighbours_widen_the_content_but_not_the_citation():
    await _corpus()
    plain = (await _search(query="hosting monthly fee")).hits[0]
    wide = (await _search(query="hosting monthly fee", neighbors=1)).hits[0]
    assert wide.citation == plain.citation
    assert plain.content in wide.content and "Fees exclude VAT." in wide.content
    assert "This agreement is made" in wide.content


async def test_a_kb_can_expand_hits_into_their_section():
    kb = CONTRACTS.model_copy(update={"retrieval": Retrieval(parent_expansion="section")})
    await _save(kb)
    await _index(kb, "pdf_contract", 1, "contracts/acme.pdf")
    hit = (await _search(query="exclude VAT")).hits[0]
    assert hit.content.startswith("Fees\n\nService | Monthly fee") and hit.content.endswith("Fees exclude VAT.")
    assert "This agreement is made" not in hit.content, "another section is not the hit's section"


def test_joining_overlapping_chunks_keeps_each_character_once():
    def row(start, end, text):
        return types.SimpleNamespace(char_start=start, char_end=end, content=text, meta={})
    text = "one two three four five"
    assert joined([row(0, 13, text[0:13]), row(8, 23, text[8:23])]) == text
    assert joined([row(0, 3, "one"), row(10, 13, "xyz")]) == "one\n\nxyz"


# ── the registry and the store ─────────────────────────────────────────────────

async def test_bases_come_from_ingestions_registry_when_it_exists(monkeypatch):
    fake = types.ModuleType("core.knowledge.bases")

    async def list_bases():
        return [REPORTS]

    fake.list_bases = list_bases
    monkeypatch.setitem(sys.modules, "core.knowledge.bases", fake)
    assert await load_bases([REPORTS.id]) == [REPORTS]


async def test_a_trace_that_cannot_be_stored_never_fails_the_search(monkeypatch):
    await _corpus()

    def broken(**_kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(trace_store, "KnowledgeQueryDB", broken)
    response = await _search(query="hosting")
    assert response.hits


async def test_old_traces_are_purged_unless_an_eval_keeps_them():
    await _corpus()
    await _search(query="hosting")
    old = datetime.now(timezone.utc) - timedelta(days=45)
    async with session() as s:
        for qid, keep in (("kq_" + "1" * 32, None), ("kq_" + "2" * 32, "evalset_1")):
            s.add(KnowledgeQueryDB(id=qid, tenant_id="default", surface="ui", trace_version=1, trace={},
                                   retained_by=keep, created_at=old))
    assert await trace_store.purge_expired() == 1
    async with session() as s:
        left = (await s.execute(select(func.count()).select_from(KnowledgeQueryDB))).scalar_one()
    assert left == 2, "today's search and the eval's old one"


async def test_record_purges_at_most_once_a_day(monkeypatch):
    calls = []

    async def counting(**_kw):
        calls.append(1)
        return 0

    monkeypatch.setattr(trace_store, "purge_expired", counting)
    monkeypatch.setattr(trace_store, "_last_purge", {})
    await _corpus()
    await _search(query="hosting")
    await _search(query="renewal")
    assert calls == [1]


def test_a_stored_trace_validates_as_one():
    assert Trace.model_fields["trace_version"].default == 1


# ── B2a (D163): the vector leg, fusion, rerank, costs ──────────────────────────

from core.knowledge.testing.fakes import FakeReranker, HashingEmbedder  # noqa: E402
from core.knowledge.testing.reference_index import reference_rrf  # noqa: E402
from core.store.models import KnowledgeCostDB, UsageLogDB  # noqa: E402

QUERY = "renewal invoice fees agreement"


async def _embedded_corpus(embedder: HashingEmbedder | None = None, **kb_updates) -> HashingEmbedder:
    embedder = embedder or HashingEmbedder(dims=32)
    seams.set_knowledge_embedder(embedder)
    contracts = KnowledgeBase.model_validate({**CONTRACTS.model_dump(), **kb_updates})
    await _save(contracts, REPORTS)
    await _index(contracts, "pdf_contract", 1, "contracts/acme.pdf", {"doc_type": "contract", "region": "EU"})
    await _index(contracts, "eml_with_attachment", 2, "contracts/mail/renewal.eml", {"doc_type": "email"})
    await _index(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx", {"doc_type": "report"})
    return embedder


def _fused_order(trace) -> list[str]:
    legs = [LegResult(leg.leg, tuple(_cands(leg)), filtered=leg.filtered, exact=leg.exact) for leg in trace.legs]
    return [cid for cid, _ in reference_rrf(legs, {"lexical": 1.0, "vector": 1.0})]


def _cands(leg):
    from core.knowledge.types import Candidate
    return [Candidate(chunk_id=c.chunk_id, doc_id="", kb_id="", snapshot_id="", score=c.score, rank=c.rank)
            for c in leg.candidates]


async def test_a_hybrid_search_asks_both_legs_and_fuses_them_by_rank():
    embedder = await _embedded_corpus()
    response = await _search(query=QUERY, include_trace=True)
    trace = response.trace

    assert response.degraded == [] and [leg.leg for leg in trace.legs] == ["lexical", "vector"]
    assert trace.fusion is not None and trace.fusion.params.weights == {"lexical": 1.0, "vector": 1.0}
    assert [h.chunk_id for h in response.hits] == _fused_order(trace)[:len(response.hits)]
    assert trace.legs[0].requested == 50 and trace.legs[1].requested == 50, "each leg over-fetches"
    both = next(h for h in response.hits if len(h.scores) == 2)
    assert set(both.scores) == {"lexical", "vector"}
    assert set(trace.hits[0].ranks) <= {"lexical", "vector"}
    assert trace.pipeline[CONTRACTS.id].embed_model == "hash-64" and trace.pipeline[CONTRACTS.id].embed_dims == 32
    query_call = embedder.calls[-1]
    assert query_call["kind"] == "query" and query_call["texts"] == [QUERY]
    assert query_call["spec"].model == "hash-64", "the query is embedded with the snapshot's own model"


async def test_a_knowledge_base_s_weights_decide_the_fusion():
    await _embedded_corpus(fusion={"weights": {"lexical": 0.0, "vector": 1.0}})
    vector_only = await _search(query=QUERY, include_trace=True)
    vector_leg = next(leg for leg in vector_only.trace.legs if leg.leg == "vector")
    assert [h.chunk_id for h in vector_only.hits] == [c.chunk_id for c in vector_leg.candidates][:len(vector_only.hits)]
    assert vector_only.trace.fusion.params.weights == {"lexical": 0.0, "vector": 1.0}


def test_fusion_is_the_reference_rrf_for_one_knowledge_base():
    import random

    from core.knowledge.kb_settings import Fusion
    from core.knowledge.search import fuse
    from core.knowledge.types import Candidate

    rng = random.Random(11)
    ids = [f"kch_{n:032x}" for n in range(40)]

    def leg(name):
        picked = rng.sample(ids, 25)
        return LegResult(name, tuple(Candidate(cid, "d", "kb", "s", 1.0 / (i + 1), i + 1)
                                     for i, cid in enumerate(picked)), filtered=True, exact=True)

    lexical, vector = leg("lexical"), leg("vector")
    weights = {"lexical": 0.7, "vector": 1.3}
    ours = fuse([("lexical", "lexical", lexical.candidates), ("vector", "vector", vector.candidates)],
                {"kb": Fusion(rrf_k=30, weights=weights)})
    want = reference_rrf([lexical, vector], weights, k=30)
    assert [c.chunk_id for c, _, _ in ours] == [cid for cid, _ in want]
    assert all(abs(score - w) < 1e-12 for (_, score, _), (_, w) in zip(ours, want))


async def test_the_query_vector_is_kept_beside_the_trace_never_in_it():
    import hashlib

    await _embedded_corpus()
    response = await _search(query=QUERY, include_trace=True)
    async with session() as s:
        row = (await s.execute(select(KnowledgeQueryDB).where(KnowledgeQueryDB.id == response.query_id))).scalar_one()
    assert row.query_embedding is not None and len(row.query_embedding) == 32 * 4
    assert response.trace.replay.query_vector_sha256 == hashlib.sha256(row.query_embedding).hexdigest()
    assert response.trace.replay.exact_possible
    assert not _keys(response.trace.model_dump(mode="json")) & FORBIDDEN_KEYS


async def test_an_embedding_that_fails_leaves_the_keyword_leg_and_says_so():
    embedder = await _embedded_corpus()
    embedder.fail_on = {QUERY}
    response = await _search(query=QUERY, include_trace=True)
    assert response.hits and response.degraded == ["embed_failed"]
    assert [leg.leg for leg in response.trace.legs] == ["lexical"] and response.trace.fusion is None
    assert response.trace.degraded[0].detail.startswith("hash-64:")


async def test_a_lexical_search_never_embeds_the_query():
    embedder = await _embedded_corpus()
    calls = len(embedder.calls)
    response = await _search(query=QUERY, mode="lexical", include_trace=True)
    assert len(embedder.calls) == calls and [leg.leg for leg in response.trace.legs] == ["lexical"]
    assert response.degraded == []


async def test_a_vector_search_answers_from_the_vector_leg_alone():
    await _embedded_corpus()
    response = await _search(query=QUERY, mode="vector", include_trace=True)
    assert response.hits and [leg.leg for leg in response.trace.legs] == ["vector"]
    assert all(set(h.scores) == {"vector"} for h in response.hits) and response.degraded == []


async def test_a_base_with_nothing_embedded_says_so_beside_one_that_has():
    seams.set_knowledge_embedder(None)  # REPORTS is indexed keyword-only
    await _save(CONTRACTS, REPORTS)
    await _index(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx")
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    await _index(CONTRACTS, "pdf_contract", 1, "contracts/acme.pdf")
    response = await _search(query="revenue fees", kb_ids=[CONTRACTS.id, REPORTS.id], include_trace=True)
    assert response.degraded == ["vector_unavailable"] and "Reports" in response.trace.degraded[0].detail
    vector_leg = next(leg for leg in response.trace.legs if leg.leg == "vector")
    assert vector_leg.scopes == [{"kb_id": CONTRACTS.id, "snapshot_id": await serving_snapshot(CONTRACTS)}]


async def test_two_embedding_models_are_two_vector_legs_and_no_kept_query_vector():
    seams.set_knowledge_embedder(HashingEmbedder(dims=32, model="hash-a"))
    await _save(CONTRACTS, REPORTS)
    await _index(CONTRACTS, "pdf_contract", 1, "contracts/acme.pdf")
    second = HashingEmbedder(dims=32, model="hash-b")  # one fake serves both models: same width
    seams.set_knowledge_embedder(second)
    await _index(REPORTS, "xlsx_two_sheets", 3, "reports/q3.xlsx")
    response = await _search(query="revenue fees", kb_ids=[CONTRACTS.id, REPORTS.id], include_trace=True)
    assert [leg.leg for leg in response.trace.legs] == ["lexical", "vector", "vector"]
    assert set(response.trace.fusion.params.weights) == {"lexical", "vector:hash-a", "vector:hash-b"}
    assert sorted(c["spec"].model for c in second.calls if c["kind"] == "query") == ["hash-a", "hash-b"]
    assert response.trace.replay.query_vector_sha256 is None and not response.trace.replay.exact_possible


# ── rerank ─────────────────────────────────────────────────────────────────────

async def test_a_reranker_asked_for_reorders_the_fused_candidates():
    await _embedded_corpus()
    reranker = FakeReranker()
    seams.set_reranker(reranker)
    response = await _search(query="hosting monthly fee", rerank=True, include_trace=True)
    trace = response.trace

    assert trace.rerank is not None and trace.rerank.model == "fake-rerank"
    assert trace.rerank.input_count == len(reranker.calls[0]["documents"]) <= 50
    assert [h.chunk_id for h in response.hits] == [r.chunk_id for r in trace.rerank.results][:len(response.hits)]
    assert all("rerank" in h.scores and h.score == h.scores["rerank"] for h in response.hits)
    assert trace.hits[0].ranks["rerank"] == 1 and reranker.calls[0]["query"] == "hosting monthly fee"
    assert trace.pipeline[CONTRACTS.id].rerank_model == "fake-rerank", "the model that answered"
    assert response.degraded == []


async def test_a_reranker_that_fails_leaves_the_fused_order_and_says_so():
    await _embedded_corpus()
    seams.set_reranker(FakeReranker(fail=True))
    response = await _search(query=QUERY, rerank=True, include_trace=True)
    assert response.degraded == ["rerank_failed"] and response.trace.rerank is None
    assert [h.chunk_id for h in response.hits] == _fused_order(response.trace)[:len(response.hits)]


async def test_a_rerank_asked_for_with_none_configured_says_so():
    await _embedded_corpus()
    response = await _search(query=QUERY, rerank=True)
    assert response.degraded == ["rerank_unconfigured"] and response.hits


async def test_by_default_a_configured_reranker_reranks_and_none_is_no_degradation():
    await _embedded_corpus()
    plain = await _search(query=QUERY, include_trace=True)
    assert plain.trace.rerank is None and plain.degraded == []
    seams.set_reranker(FakeReranker())
    reranked = await _search(query=QUERY, include_trace=True)
    assert reranked.trace.rerank is not None
    off = KnowledgeBase.model_validate({**CONTRACTS.model_dump(), "rerank": {"enabled": False}})
    await _save(off, REPORTS)
    assert (await _search(query=QUERY, include_trace=True)).trace.rerank is None, "a base can switch it off"


async def test_a_threshold_reads_the_reranker_s_score():
    await _embedded_corpus()
    seams.set_reranker(FakeReranker())
    response = await _search(query="hosting monthly fee", rerank=True, score_threshold=0.99, include_trace=True)
    assert response.hits and all(h.score >= 0.99 for h in response.hits)
    assert any(c.stage == "threshold" for c in response.trace.cuts)


# ── costs ──────────────────────────────────────────────────────────────────────

async def test_the_query_s_embedding_is_a_cost_of_the_run_that_searched():
    await _embedded_corpus(HashingEmbedder(dims=32, cost_source="byok", usd_per_mtok=20.0))
    response = await _search(query=QUERY, include_trace=True)

    assert response.cost.total_est_usd > 0 and set(response.cost.by_stage) == {"embed"}
    assert response.trace.costs.rows[0].stage == "embed" and response.trace.costs.rows[0].cost_source == "byok"
    async with session() as s:
        cost = (await s.execute(select(KnowledgeCostDB).where(
            KnowledgeCostDB.query_id == response.query_id))).scalar_one()
        usage = (await s.execute(select(UsageLogDB).where(UsageLogDB.run_id == "run_1"))).scalars().all()
    assert (cost.stage, cost.run_id, cost.session_id, cost.kb_id) == ("embed", "run_1", "sess_1", CONTRACTS.id)
    assert [(u.source, u.tool_name, u.session_id) for u in usage] == [("orchestration", "knowledge:embed", "sess_1")]


# ── B2b (D167): a knowledge base at its monthly budget ─────────────────────────

async def _spent(kb_id: str, usd: float) -> None:
    from core.knowledge.ledger import CostRow, record_costs
    from core.knowledge.types import Usage

    await record_costs([CostRow(Usage(stage="embed", cost_source="byok", model="m", provider="p", est_usd=usd),
                                Attribution(kb_id=kb_id))])


async def test_a_base_at_its_cap_is_searched_by_keyword_only_and_says_so():
    """Fails against B2a's search: it embedded the query whatever the base had spent (Naveen, D167 #3)."""
    embedder = await _embedded_corpus(budget_monthly_usd=0.01)
    await _spent(CONTRACTS.id, 0.02)
    embedder.calls.clear()
    response = await _search(query=QUERY, include_trace=True)
    assert response.degraded == ["budget_paused"] and response.hits
    assert [leg.leg for leg in response.trace.legs] == ["lexical"]
    assert [c for c in embedder.calls if c["kind"] == "query"] == [], "no query embedding was paid for"
    assert "Contracts" in next(d.detail for d in response.trace.degraded if d.code == "budget_paused")


async def test_room_under_the_cap_searches_both_legs():
    await _embedded_corpus(budget_monthly_usd=5.0)
    await _spent(CONTRACTS.id, 0.02)
    response = await _search(query=QUERY, include_trace=True)
    assert response.degraded == [] and {leg.leg for leg in response.trace.legs} == {"lexical", "vector"}


async def test_a_base_at_its_cap_pays_for_no_rerank():
    await _embedded_corpus(budget_monthly_usd=0.01)
    await _spent(CONTRACTS.id, 0.02)
    reranker = FakeReranker()
    seams.set_reranker(reranker)
    response = await _search(query=QUERY, rerank=True, include_trace=True)
    assert reranker.calls == [] and response.trace.rerank is None and "budget_paused" in response.degraded


async def _platform_rerank_near_its_cap() -> FakeReranker:
    """A base reranking on our gateway with $0.0022 left under its cap: room for a $0.002 search unit at list,
    none at the $0.0025 the deployment charges."""
    await _embedded_corpus(budget_monthly_usd=0.01, rerank={"enabled": True, "model": "synapse.cohere-rerank-3-5"})
    await _spent(CONTRACTS.id, 0.0078)
    reranker = FakeReranker()
    seams.set_reranker(reranker)
    return reranker


async def test_a_platform_rerank_is_capped_at_what_the_deployment_charges():
    """F334 (W1, D179): the cap check prices the rerank at the deployment's rate, not the route's list price."""
    from core.knowledge import costs
    from core.knowledge.costs import Rate

    reranker = await _platform_rerank_near_its_cap()

    async def pricer(model):
        return Rate(per_unit=0.0025, unit="search_units") if model == "synapse.cohere-rerank-3-5" else None

    costs.set_platform_pricer(pricer)
    response = await _search(query=QUERY, include_trace=True)
    assert reranker.calls == [] and "budget_paused" in response.degraded


async def test_at_its_list_price_the_same_rerank_fits_under_the_cap():
    reranker = await _platform_rerank_near_its_cap()
    response = await _search(query=QUERY, include_trace=True)
    assert len(reranker.calls) == 1 and response.degraded == []


async def _voyage_rerank_with_room(room_usd: float) -> FakeReranker:
    """A base reranking with Voyage, priced by the token ($0.05 per 1M), with `room_usd` left under its cap."""
    await _embedded_corpus(budget_monthly_usd=0.01, rerank={"enabled": True, "model": "voyage.rerank-3"})
    await _spent(CONTRACTS.id, 0.01 - room_usd)
    reranker = FakeReranker()
    seams.set_reranker(reranker)
    return reranker


async def test_a_rerank_priced_by_the_token_is_counted_before_it_is_paid_for():
    """F373: a Voyage rerank counted $0 in the check before a search, so a base at its cap paid for one anyway."""
    reranker = await _voyage_rerank_with_room(0.000_002)  # two micro-dollars: less than any pool's tokens
    response = await _search(query=QUERY, include_trace=True)
    assert reranker.calls == [] and response.trace.rerank is None
    assert "budget_paused" in response.degraded and {leg.leg for leg in response.trace.legs} == {"lexical", "vector"}
    assert "Contracts" in next(d.detail for d in response.trace.degraded if d.code == "budget_paused")


async def test_room_for_its_tokens_and_the_voyage_rerank_runs():
    reranker = await _voyage_rerank_with_room(0.001)
    response = await _search(query=QUERY, include_trace=True)
    assert len(reranker.calls) == 1 and response.degraded == []


async def test_only_the_base_at_its_cap_loses_its_vector_leg():
    embedder = await _embedded_corpus(budget_monthly_usd=0.01)
    await _spent(CONTRACTS.id, 0.02)
    embedder.calls.clear()
    response = await _search(query=QUERY, kb_ids=[CONTRACTS.id, REPORTS.id], include_trace=True)
    vector = [leg for leg in response.trace.legs if leg.leg == "vector"]
    assert len(vector) == 1 and [s["kb_id"] for s in vector[0].scopes] == [REPORTS.id]
    assert "budget_paused" in response.degraded


async def test_a_keyword_search_at_the_cap_costs_nothing_and_is_not_degraded():
    await _embedded_corpus(budget_monthly_usd=0.01)
    await _spent(CONTRACTS.id, 0.02)
    response = await _search(query=QUERY, mode="lexical")
    assert response.degraded == [] and response.hits
