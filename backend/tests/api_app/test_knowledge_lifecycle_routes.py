"""A knowledge base's settings over time (B2b, D167): an edit that raises the budget resumes what it
paused, and (below, as each lands) a rebuild on an index change, the cost preview and the costs.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import FakeIndexer, install_fake_indexing
from core.knowledge import dispatch, documents, seams
from core.knowledge.errors import BudgetExceeded
from core.knowledge.jobs import run_ingest_job
from core.knowledge.testing.fakes import RecordingDispatcher

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture(autouse=True)
def _fresh():
    dispatch.reset()
    yield
    dispatch.reset()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    return client


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


async def _kb(client, folder="kb", **kw):
    resp = await client.post("/api/knowledge/bases", json={"name": "Contracts", "folder": folder, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestTheBudget:
    async def test_raising_the_cap_resumes_what_it_paused(self, on, jobs, monkeypatch):
        kb = await _kb(on, budget_monthly_usd=1.0)
        install_fake_indexing(monkeypatch, FakeIndexer(raises=BudgetExceeded(kb["id"], 1.0, 1.0)))
        jobs.jobs.clear()
        await on.post("/api/knowledge/bases/" + kb["id"] + "/records", json={"records": [{"id": "r1", "text": "x"}]})
        await jobs.drain(run_ingest_job)
        (row,) = [r for r in await documents.list_for(kb["id"]) if r.status == "paused"]
        assert row.status_reason == "budget_cap"
        state = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()["state"]
        assert state["paused_reason"] == "budget_cap"

        lowered = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "budget_monthly_usd": 0.5})
        assert lowered.status_code == 200 and (await documents.get(row.id)).status == "paused"
        raised = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 2, "budget_monthly_usd": 5.0})
        assert raised.status_code == 200, raised.text
        assert (await documents.get(row.id)).status == "queued"
        assert [(j.op, j.reason) for j in jobs.jobs] == [("upsert", "budget_resume")]

    async def test_clearing_the_cap_resumes_too(self, on, jobs, monkeypatch):
        kb = await _kb(on, budget_monthly_usd=1.0)
        install_fake_indexing(monkeypatch, FakeIndexer(raises=BudgetExceeded(kb["id"], 1.0, 1.0)))
        await on.post("/api/knowledge/bases/" + kb["id"] + "/records", json={"records": [{"id": "r1", "text": "x"}]})
        await jobs.drain(run_ingest_job)
        cleared = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "clear_budget": True})
        assert cleared.status_code == 200 and cleared.json()["budget_monthly_usd"] is None
        assert {r.status for r in await documents.list_for(kb["id"])} == {"queued"}


class TestARebuild:
    async def test_an_index_change_answers_reindex_started_and_the_state_shows_the_build(self, on, jobs):
        kb = await _kb(on)
        await on.post(f"/api/knowledge/bases/{kb['id']}/records", json={"records": [{"id": "r1", "text": "Refunds."}]})
        await jobs.drain(run_ingest_job)  # B1's real pipeline: the base's first snapshot
        served = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()["state"]["active_snapshot"]
        assert served

        same = await on.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": 1, "retrieval": {"top_k": 3}})
        assert same.status_code == 200 and same.json()["reindex_started"] is False
        changed = await on.patch(f"/api/knowledge/bases/{kb['id']}",
                                 json={"revision": 2, "chunking": {"mode": "fixed"}})
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["reindex_started"] is True
        state = body["state"]
        assert state["active_snapshot"] == served and state["building_snapshot"]
        assert state["build"] == {"snapshot_id": state["building_snapshot"], "status": "building", "docs_total": 1,
                                  "docs_done": 0, "docs_failed": 0, "started_at": state["build"]["started_at"],
                                  "paused_reason": None, "error": None}

        await jobs.drain(run_ingest_job)
        after = (await on.get(f"/api/knowledge/bases/{kb['id']}")).json()["state"]
        assert after["active_snapshot"] == state["building_snapshot"] and after["build"] is None
