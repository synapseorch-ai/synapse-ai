"""A knowledge base's eval re-runs itself when its settings change (V1, D168 #3): after an index switch, after a
saved search setting; only sets that ran before; never a person's run cancelled; nothing spent past the budget.
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from _fakes.knowledge_eval import finished, policies, question_set
from core.knowledge import bases, dispatch, evals, evals_auto as AUTO, evals_runs as RUNS, events, seams, snapshots
from core.knowledge.api_models import EvalRunRequest, EvalSetCreate, KnowledgeBaseUpdate
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Chunking, Fusion, Retrieval
from core.knowledge.testing.fakes import FakeReranker, HashingEmbedder, RecordingDispatcher
from core.store import session
from core.store.models import KnowledgeEvalRunDB, KnowledgeQueryDB

pytestmark = pytest.mark.usefixtures("knowledge_store")


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(AUTO, "SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(AUTO, "SETTLE_MAX_S", 5.0)
    monkeypatch.setattr(AUTO, "DEBOUNCE_S", 0.0)
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    AUTO._pending.clear()
    yield
    for task in [*RUNS.tasks().values(), *AUTO.waiting().values()]:
        task.cancel()
    AUTO._pending.clear()
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


async def ran(kb, made) -> str:
    started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
    await finished()
    return started.id


async def edit(kb_id: str, **fields):
    current = await bases.require_base(kb_id)
    before, after = await bases.update_base(kb_id, KnowledgeBaseUpdate(revision=current.revision, **fields))
    return before, after


async def runs_of(set_id: str) -> list[KnowledgeEvalRunDB]:
    async with session() as s:
        return list((await s.execute(select(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.set_id == set_id)
                                     .order_by(KnowledgeEvalRunDB.created_at))).scalars().all())


async def eval_searches() -> int:
    async with session() as s:
        return (await s.execute(select(func.count()).select_from(KnowledgeQueryDB).where(
            KnowledgeQueryDB.surface == "eval"))).scalar_one()


async def settle() -> None:
    await asyncio.gather(*list(AUTO.waiting().values()), return_exceptions=True)
    await finished()


class TestAfterASwitch:
    async def test_a_switch_re_runs_the_sets_that_ran_on_the_new_index(self, jobs):
        AUTO.install()
        AUTO.install()  # idempotent
        assert events.knowledge_observers.count(AUTO._observer) == 1
        kb = await policies(jobs)
        ran_before = await question_set(kb)
        never = await question_set(kb)  # questions, but never run
        skipped = await question_set(kb)  # its only run never ran
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "6" * 32, kb_id=kb.id, set_id=skipped.id, status="skipped"))
        await ran(kb, ran_before)
        before, after = await edit(kb.id, chunking=Chunking(mode="fixed", target_tokens=64, max_tokens=64))
        assert await snapshots.after_save(before, after)
        await jobs.drain(run_ingest_job)  # builds, then switches: knowledge.base.reindexed
        await settle()
        new = await snapshots.serving_snapshot(after)
        runs = await runs_of(ran_before.id)
        latest = runs[-1]
        # Every run of the set, so a failure in a full suite says which way it went: this has failed
        # only there, under load, never alone or in 1,670 knowledge tests in order (BF11, 10-23).
        seen = [(r.started_by, r.status, r.error, [s["snapshot_id"] for s in (r.config or {}).get("snapshots", [])])
                for r in runs]
        assert (latest.started_by, latest.status) == ("settings", "completed"), (seen, new, AUTO.waiting())
        assert [s["snapshot_id"] for s in latest.config["snapshots"]] == [new], (seen, new)
        assert latest.config["judge"] is None
        assert await runs_of(never.id) == []
        assert [r.status for r in await runs_of(skipped.id)] == ["skipped"]

    async def test_a_base_that_never_settles_is_re_run_once_the_wait_is_over(self, jobs, monkeypatch):
        from core.knowledge.events import KnowledgeLifecycleEvent, emit
        from core.store.models import KnowledgeDocumentDB

        monkeypatch.setattr(AUTO, "SETTLE_MAX_S", 0.2)
        AUTO.install()
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        async with session() as s:
            await s.execute(update(KnowledgeDocumentDB).where(KnowledgeDocumentDB.path == "policies/hosting.md")
                            .values(status="queued"))  # a document that stays queued
        await emit(KnowledgeLifecycleEvent(id="kev_" + "1" * 32, type="knowledge.base.reindexed",
                                           created_at="2026-10-02T00:00:00+00:00", kb_id=kb.id))
        await settle()
        assert (await runs_of(made.id))[-1].started_by == "settings"

    async def test_without_the_observer_installed_a_switch_runs_nothing(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        before, after = await edit(kb.id, chunking=Chunking(mode="fixed", target_tokens=64, max_tokens=64))
        await snapshots.after_save(before, after)
        await jobs.drain(run_ingest_job)
        await settle()
        assert len(await runs_of(made.id)) == 1


class TestAfterASavedSetting:
    async def test_new_weights_are_scored_from_the_stored_searches(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        first = await ran(kb, made)
        searched = await eval_searches()
        before, after = await edit(kb.id, fusion=Fusion(weights={"lexical": 1.0, "vector": 0.0}))
        await AUTO.settings_changed(before, after)
        await settle()
        assert await eval_searches() == searched  # no search
        latest = (await runs_of(made.id))[-1]
        assert (latest.started_by, latest.cost_usd, latest.config["rescored_from"]) == ("settings", 0.0, first)
        run = await RUNS.get_run(kb.id, latest.id)
        snap = run.snapshots[0].snapshot_id
        hybrid = next(r for r in run.results if r.snapshot_id == snap and r.config == "hybrid")
        lexical = next(r for r in run.results if r.snapshot_id == snap and r.config == "lexical")
        assert hybrid.scores.hit_at == lexical.scores.hit_at and any("no search" in n for n in run.notes)

    async def test_a_reranked_set_is_searched_again(self, jobs):
        seams.set_reranker(FakeReranker())
        kb = await policies(jobs, rerank={"enabled": True})
        made = await question_set(kb)
        await ran(kb, made)
        searched = await eval_searches()
        before, after = await edit(kb.id, fusion=Fusion(rrf_k=20))
        await AUTO.settings_changed(before, after)
        await settle()
        assert await eval_searches() == searched + 3
        assert (await runs_of(made.id))[-1].config.get("rescored_from") is None

    async def test_a_retrieval_change_is_searched_again_and_a_name_change_is_not(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        before, after = await edit(kb.id, name="Renamed")
        await AUTO.settings_changed(before, after)
        await settle()
        assert len(await runs_of(made.id)) == 1
        before, after = await edit(kb.id, retrieval=Retrieval(top_k=12))
        await AUTO.settings_changed(before, after)
        await settle()
        latest = (await runs_of(made.id))[-1]
        assert len(await runs_of(made.id)) == 2 and latest.config["top_k"] == 12

    async def test_through_the_settings_route(self, jobs, client):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        current = await bases.require_base(kb.id)
        resp = await client.patch(f"/api/knowledge/bases/{kb.id}", json={
            "revision": current.revision, "retrieval": {"top_k": 9}})
        assert resp.status_code == 200 and resp.json()["reindex_started"] is False
        await settle()
        assert (await runs_of(made.id))[-1].config["top_k"] == 10  # at least 10, scored @10


class TestWhatIsNeverDone:
    async def test_a_persons_run_is_not_cancelled_the_re_run_waits_for_it(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "9" * 32, kb_id=kb.id, set_id=made.id, status="running",
                                     started_by="person"))
        before, after = await edit(kb.id, retrieval=Retrieval(top_k=11))
        await AUTO.settings_changed(before, after)
        assert len(await runs_of(made.id)) == 2  # the earlier run and the person's, nothing new
        async with session() as s:
            await s.execute(update(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.id == "krun_" + "9" * 32)
                            .values(status="completed"))
        await AUTO.after_run(kb.id, "krun_" + "9" * 32)
        await settle()
        assert (await runs_of(made.id))[-1].started_by == "settings"

    async def test_an_earlier_automatic_run_is_replaced(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "8" * 32, kb_id=kb.id, set_id=made.id, status="queued",
                                     started_by="settings"))
        before, after = await edit(kb.id, retrieval=Retrieval(top_k=11))
        await AUTO.settings_changed(before, after)
        await settle()
        statuses = {r.id: r.status for r in await runs_of(made.id)}
        assert statuses["krun_" + "8" * 32] == "cancelled"
        assert (await runs_of(made.id))[-1].status == "completed"

    async def test_a_base_at_its_budget_records_a_skipped_run(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)

        async def priced(model, texts):
            return 1.0

        monkeypatch.setattr("core.knowledge.budgets.embed_estimate_usd", priced)
        await edit(kb.id, budget_monthly_usd=0.5)
        before, after = await edit(kb.id, retrieval=Retrieval(top_k=11))
        searched = await eval_searches()
        await AUTO.settings_changed(before, after)
        await settle()
        latest = (await runs_of(made.id))[-1]
        assert latest.status == "skipped" and "monthly budget" in latest.error
        assert await eval_searches() == searched

    async def test_a_deleted_base_takes_its_sets_and_runs(self, jobs, client):
        kb = await policies(jobs)
        made = await question_set(kb)
        await ran(kb, made)
        assert (await client.delete(f"/api/knowledge/bases/{kb.id}")).status_code == 202
        assert await runs_of(made.id) == []
        assert await evals._rows(kb_id=kb.id) == []

    async def test_startup_marks_what_was_left_running(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "7" * 32, kb_id=kb.id, set_id=made.id, status="running"))
        await AUTO.startup()
        assert (await runs_of(made.id))[0].status == "interrupted"
        assert AUTO._observer in events.knowledge_observers
