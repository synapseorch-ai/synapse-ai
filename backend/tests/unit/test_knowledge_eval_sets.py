"""A knowledge base's question sets (V1, D168): saved only as a person edits them, kept by revision, and the real
searches they borrow kept past the cleanup. SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from _fakes.knowledge_corpus import CONTRACTS, REPORTS, corpus
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge import evals, trace_store
from core.knowledge.api_models import (
    MAX_EVAL_CASES, EvalPromote, EvalSetCreate, EvalSetUpdate, SearchRequest,
)
from core.knowledge.errors import StaleRevision
from core.knowledge.search import search
from core.knowledge.trace import Origin
from core.store import session
from core.store.models import KnowledgeEvalRunDB, KnowledgeQueryDB

pytestmark = pytest.mark.usefixtures("knowledge_store")
ON = {"knowledge_enabled": True}


def _case(question="When does the contract renew?", **kw):
    case = {"kind": "direct", "question": question, "reference_answer": "On 1 November.",
            "evidence": [{"doc_id": "kdoc_" + "2" * 32, "path": "contracts/mail/renewal.eml", "doc_sha256": "a" * 64,
                          "char_start": 60, "char_end": 114, "quote": "It renews automatically on 1 November."}]}
    case.update(kw)
    return case


async def _searched(query: str, kb=CONTRACTS, surface: str = "ui") -> str:
    response = await search(SearchRequest(query=query, kb_ids=[kb.id]), origin=Origin(surface=surface), settings=ON)
    return response.query_id


async def _retained_by(query_id: str) -> str | None:
    async with session() as s:
        return (await s.get(KnowledgeQueryDB, query_id)).retained_by


class TestSets:
    async def test_a_set_is_saved_with_ids_and_read_back(self):
        await corpus()
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Renewals", cases=[
            _case(), _case("Who signs it?", kind="paraphrase")]))
        assert made.id.startswith("kset_") and made.revision == 1
        assert [c.id.startswith("case_") for c in made.cases] == [True, True]
        assert made.counts_by_kind == {"direct": 1, "paraphrase": 1} and made.case_count == 2
        listed = await evals.list_sets(CONTRACTS.id)
        assert [s.id for s in listed] == [made.id] and listed[0].last_run is None
        assert await evals.list_sets(REPORTS.id) == []

    async def test_an_edit_names_its_revision_and_keeps_case_ids(self):
        await corpus()
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Renewals", cases=[_case()]))
        first = made.cases[0]
        edited = first.model_copy(update={"question": "When is the renewal?"})
        saved = await evals.update_set(CONTRACTS.id, made.id, EvalSetUpdate(revision=1, cases=[edited, _case("New?")]))
        assert saved.revision == 2
        assert saved.cases[0].id == first.id and saved.cases[0].created_at == first.created_at
        assert saved.cases[0].question == "When is the renewal?"
        with pytest.raises(StaleRevision):
            await evals.update_set(CONTRACTS.id, made.id, EvalSetUpdate(revision=1, name="Late"))
        assert (await evals.get_set(CONTRACTS.id, made.id)).name == "Renewals"

    async def test_a_set_holds_at_most_two_hundred_cases(self):
        with pytest.raises(ValidationError):
            EvalSetUpdate(revision=1, cases=[_case()] * (MAX_EVAL_CASES + 1))

    async def test_another_bases_set_is_not_found(self):
        await corpus()
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Renewals"))
        with pytest.raises(evals.EvalNotFound):
            await evals.get_set(REPORTS.id, made.id)

    async def test_a_set_with_a_run_under_way_is_not_deleted(self):
        await corpus()
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Renewals"))
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "1" * 32, kb_id=CONTRACTS.id, set_id=made.id, status="running"))
        with pytest.raises(evals.EvalBusy):
            await evals.delete_set(CONTRACTS.id, made.id)
        async with session() as s:
            await s.execute(update(KnowledgeEvalRunDB).values(status="completed"))
        await evals.delete_set(CONTRACTS.id, made.id)
        async with session() as s:
            assert (await s.execute(select(KnowledgeEvalRunDB))).all() == []
        assert await evals.list_sets(CONTRACTS.id) == []


class TestPromotedSearches:
    async def test_a_search_becomes_a_case_from_the_passages_ticked(self):
        await corpus()
        query_id = await _searched("hosting monthly fee")
        trace = await trace_store.get(query_id)
        ticked = trace.hits[0]
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Real"))
        saved = await evals.promote(CONTRACTS.id, made.id, EvalPromote(query_id=query_id, chunk_ids=[ticked.chunk_id]))
        case = saved.cases[-1]
        assert (case.kind, case.question, case.query_id) == ("promoted", "hosting monthly fee", query_id)
        ev = case.evidence[0]
        assert (ev.doc_id, ev.path, ev.doc_sha256) == (ticked.doc_id, "contracts/acme.pdf", ticked.doc_sha256)
        assert ev.char_end > ev.char_start and "Hosting" in ev.quote
        assert await _retained_by(query_id) == made.id

    async def test_a_kept_search_outlives_the_cleanup(self):
        await corpus()
        kept, other = await _searched("hosting monthly fee"), await _searched("renewal invoice")
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Real"))
        chunk = (await trace_store.get(kept)).hits[0].chunk_id
        await evals.promote(CONTRACTS.id, made.id, EvalPromote(query_id=kept, chunk_ids=[chunk]))
        removed = await trace_store.purge_expired(now=datetime.now(timezone.utc) + timedelta(days=60))
        assert removed == 1
        assert await trace_store.get(kept) is not None and await trace_store.get(other) is None

    async def test_only_what_the_search_returned_can_be_ticked(self):
        await corpus()
        query_id = await _searched("hosting monthly fee")
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Real"))
        with pytest.raises(ValueError):
            await evals.promote(CONTRACTS.id, made.id, EvalPromote(query_id=query_id, chunk_ids=["kch_" + "9" * 32]))
        reports_query = await _searched("revenue", kb=REPORTS)
        with pytest.raises(evals.EvalNotFound):
            await evals.promote(CONTRACTS.id, made.id, EvalPromote(query_id=reports_query, chunk_ids=["kch_x"]))

    async def test_a_removed_case_releases_its_search_or_passes_it_on(self):
        await corpus()
        query_id = await _searched("hosting monthly fee")
        chunk = (await trace_store.get(query_id)).hits[0].chunk_id
        one = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="One"))
        two = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="Two"))
        one = await evals.promote(CONTRACTS.id, one.id, EvalPromote(query_id=query_id, chunk_ids=[chunk]))
        two = await evals.promote(CONTRACTS.id, two.id, EvalPromote(query_id=query_id, chunk_ids=[chunk]))
        assert await _retained_by(query_id) == one.id  # the first keeps it; the second does not take it
        await evals.update_set(CONTRACTS.id, one.id, EvalSetUpdate(revision=one.revision, cases=[]))
        assert await _retained_by(query_id) == two.id
        await evals.delete_set(CONTRACTS.id, two.id)
        assert await _retained_by(query_id) is None

    async def test_recent_searches_are_this_bases_and_never_the_evals_own(self):
        await corpus()
        mine = await _searched("hosting monthly fee")
        await _searched("renewal", surface="eval")
        await _searched("revenue", kb=REPORTS)
        found = await evals.recent_queries(CONTRACTS.id)
        assert [q.query_id for q in found] == [mine]
        assert found[0].query == "hosting monthly fee" and found[0].hits[0].path == "contracts/acme.pdf"

    async def test_a_deleted_base_takes_its_sets_and_runs(self):
        await corpus()
        query_id = await _searched("hosting monthly fee")
        chunk = (await trace_store.get(query_id)).hits[0].chunk_id
        made = await evals.create_set(CONTRACTS.id, EvalSetCreate(name="One"))
        await evals.promote(CONTRACTS.id, made.id, EvalPromote(query_id=query_id, chunk_ids=[chunk]))
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "2" * 32, kb_id=CONTRACTS.id, set_id=made.id, status="completed"))
        await evals.forget_base(CONTRACTS.id)
        assert await evals.list_sets(CONTRACTS.id) == []
        assert await evals.list_runs(CONTRACTS.id) == []
        assert await _retained_by(query_id) is None


class TestRoutes:
    @pytest.fixture
    async def on(self, client):
        assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
        return client

    async def test_a_set_through_the_api(self, on):
        await corpus()
        base = f"/api/knowledge/bases/{CONTRACTS.id}/eval/sets"
        made = await on.post(base, json={"name": "Renewals", "cases": [_case()]})
        assert made.status_code == 201, made.text
        set_id = made.json()["id"]
        assert (await on.get(base)).json()[0]["case_count"] == 1
        stale = await on.put(f"{base}/{set_id}", json={"revision": 7, "name": "x"})
        assert stale.status_code == 409
        assert (await on.put(f"{base}/{set_id}", json={"revision": 1, "unknown": 1})).status_code == 422
        assert (await on.get(f"{base}/kset_{'f' * 32}")).status_code == 404
        assert (await on.delete(f"{base}/{set_id}")).status_code == 204
        assert (await on.get(f"/api/knowledge/bases/kb_ffffffffffff/eval/sets")).status_code == 404

    async def test_promote_and_recent_searches_through_the_api(self, on):
        await corpus()
        searched = await on.post("/api/knowledge/search", json={"query": "hosting monthly fee",
                                                                  "kb_ids": [CONTRACTS.id]})
        query_id, chunk = searched.json()["query_id"], searched.json()["hits"][0]["chunk_id"]
        recent = await on.get(f"/api/knowledge/bases/{CONTRACTS.id}/eval/queries")
        assert recent.status_code == 200 and recent.json()[0]["query_id"] == query_id
        set_id = (await on.post(f"/api/knowledge/bases/{CONTRACTS.id}/eval/sets", json={"name": "Real"})).json()["id"]
        bad = await on.post(f"/api/knowledge/bases/{CONTRACTS.id}/eval/sets/{set_id}/promote",
                            json={"query_id": query_id, "chunk_ids": ["kch_nope"]})
        assert bad.status_code == 422
        good = await on.post(f"/api/knowledge/bases/{CONTRACTS.id}/eval/sets/{set_id}/promote",
                             json={"query_id": query_id, "chunk_ids": [chunk]})
        assert good.status_code == 200, good.text
        assert good.json()["cases"][0]["kind"] == "promoted"
