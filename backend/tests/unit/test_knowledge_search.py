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
