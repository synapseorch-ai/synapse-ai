"""Knowledge's internal search and trace routes (B1b, D161): the Knowledge screen's test search.

`POST /api/knowledge/search` is B1a's `search.search()` behind the app's own
door, traced as the UI; `GET /api/knowledge/queries/{id}` reads a search's
trace back. Both are the frozen route table's (`api_models.ROUTES`).
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from _fakes.knowledge_corpus import CONTRACTS, REPORTS, corpus
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core.knowledge.snapshots import serving_snapshot
from core.knowledge.trace import FORBIDDEN_KEYS
from core.store import session
from core.store.models import KnowledgeQueryDB

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    return client


class TestSearch:
    async def test_hits_are_labelled_cited_and_traced_as_the_ui(self, on):
        await corpus()
        resp = await on.post("/api/knowledge/search",
                             json={"query": "hosting monthly fee", "kb_ids": [CONTRACTS.id], "include_trace": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        first = body["hits"][0]
        assert first["label"] == "[K1]" and first["citation"]["path"] == "contracts/acme.pdf"
        assert "Hosting" in first["content"]
        assert body["trace"]["origin"]["surface"] == "ui"
        async with session() as s:
            row = (await s.execute(select(KnowledgeQueryDB).where(KnowledgeQueryDB.id == body["query_id"]))).scalar_one()
        assert row.surface == "ui"

    async def test_several_bases_and_a_filter(self, on):
        await corpus()
        resp = await on.post("/api/knowledge/search", json={
            "query": "renewal invoice revenue", "kb_ids": [CONTRACTS.id, REPORTS.id],
            "filters": {"doc_type": "email"}})
        assert resp.status_code == 200, resp.text
        assert {h["citation"]["path"] for h in resp.json()["hits"]} == {"contracts/mail/renewal.eml"}

    async def test_an_unknown_base_is_a_404(self, on):
        await corpus()
        resp = await on.post("/api/knowledge/search", json={"query": "fees", "kb_ids": ["kb_ffffffffffff"]})
        assert resp.status_code == 404, resp.text
        assert "kb_ffffffffffff" in resp.json()["detail"]

    async def test_a_snapshot_of_another_base_is_a_404(self, on):
        await corpus()
        other = await serving_snapshot(REPORTS)
        resp = await on.post("/api/knowledge/search", json={
            "query": "fees", "kb_ids": [CONTRACTS.id], "snapshot_ids": {CONTRACTS.id: other}})
        assert resp.status_code == 404, resp.text
        assert "snapshot" in resp.json()["detail"]

    async def test_a_bad_filter_is_a_422_naming_its_code(self, on):
        await corpus()
        resp = await on.post("/api/knowledge/search",
                             json={"query": "fees", "kb_ids": [CONTRACTS.id], "filters": {"$where": "1"}})
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["type"].startswith("filter.")


class TestTrace:
    async def test_a_search_s_trace_reads_back(self, on):
        await corpus()
        found = (await on.post("/api/knowledge/search",
                               json={"query": "hosting monthly fee", "kb_ids": [CONTRACTS.id]})).json()
        resp = await on.get(f"/api/knowledge/queries/{found['query_id']}")
        assert resp.status_code == 200, resp.text
        trace = resp.json()
        assert trace["query_id"] == found["query_id"] and trace["request"]["query"] == "hosting monthly fee"
        assert [h["label"] for h in trace["hits"]] == [h["label"] for h in found["hits"]]
        assert not [k for k in FORBIDDEN_KEYS if f'"{k}":' in resp.text], "a trace never carries a vector"

    async def test_an_unknown_query_is_a_404(self, on):
        resp = await on.get("/api/knowledge/queries/kq_" + "0" * 32)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "No such query."
