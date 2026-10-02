"""Running a question set (V1, D168): one search per question per snapshot, scored keyword-only, vector-only,
fused and as served; snapshots compared on the questions both can answer; cancel, interruption, the budget.
The real pipeline, SQL index and search, with a hashing embedder ($0). SQLite always; Postgres with
`SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings, write_vault
from _fakes.knowledge_eval import POLICIES, QUESTIONS, finished, policies, question_set, write
from core.knowledge import bases, dispatch, documents, evals, evals_generate as G, evals_runs as RUNS, search
from core.knowledge import seams, snapshots
from core.knowledge.api_models import EvalCompareRequest, EvalRunRequest, EvalSetCreate, KnowledgeBaseUpdate
from core.knowledge.evals import EvalBusy, EvalNotFound
from core.knowledge.jobs import run_ingest_job
from core.knowledge.kb_settings import Chunking
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.store import session
from core.store.models import KnowledgeChunkDB, KnowledgeEvalRunDB, KnowledgeQueryDB, UsageLogDB

pytestmark = pytest.mark.usefixtures("knowledge_store")



@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    yield
    for task in list(RUNS.tasks().values()):
        task.cancel()
    seams.reset_all()
    dispatch.reset()


@pytest.fixture
def jobs():
    recorder = RecordingDispatcher()
    seams.set_ingest_dispatcher(recorder)
    return recorder


async def run_set(kb, made, **kw):
    started = await RUNS.start(kb, EvalRunRequest(set_id=made.id, **kw))
    await finished()
    return await RUNS.get_run(kb.id, started.id)


async def edit(kb_id: str, **fields):
    current = await bases.require_base(kb_id)
    before, after = await bases.update_base(kb_id, KnowledgeBaseUpdate(revision=current.revision, **fields))
    return after, await snapshots.after_save(before, after)


def row(run, snap, config):
    return next(r for r in run.results if r.snapshot_id == snap and r.config == config)


class TestARun:
    async def test_each_question_is_searched_once_per_snapshot_as_the_evals_own(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        assert (run.status, run.case_count, run.done_count, run.compared_cases) == ("completed", 3, 3, 3)
        async with session() as s:
            searched = (await s.execute(select(KnowledgeQueryDB).where(KnowledgeQueryDB.surface == "eval"))).scalars().all()
        assert len(searched) == 3 and {r.session_id for r in searched} == {f"knowledge-eval:{run.id}"}
        snap = run.snapshots[0].snapshot_id
        assert {r.config for r in run.results} == {"lexical", "vector", "hybrid", "served"}
        served = row(run, snap, "served")
        assert served.available and served.agent_sees == kb.retrieval.top_k
        assert served.scores.cases == 3 and served.scores.hit_at["10"] == 1.0
        assert set(served.by_kind) == {"direct"}

    async def test_each_question_says_where_its_answer_ranked(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        snap = run.snapshots[0].snapshot_id
        first = run.cases[0]
        assert first.question == QUESTIONS[0][1] and first.ranks[f"{snap}:served"] is not None
        assert first.query_ids[snap].startswith("kq_")
        assert any(t.relevant and t.path == "policies/refunds.md" for t in first.top)

    async def test_without_vectors_the_vector_rows_say_why(self, jobs):
        seams.set_knowledge_embedder(None)
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        snap = run.snapshots[0].snapshot_id
        assert not row(run, snap, "vector").available and "embedded" in row(run, snap, "vector").reason
        assert row(run, snap, "lexical").available and row(run, snap, "served").available

    async def test_custom_weights_are_scored_from_the_stored_searches(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        calls = []
        real = search.search

        async def counted(*a, **kw):
            calls.append(1)
            return await real(*a, **kw)

        monkeypatch.setattr(search, "search", counted)
        rows = await RUNS.compare(kb.id, run.id, EvalCompareRequest(weights={"lexical": 1.0, "vector": 0.0}))
        snap = run.snapshots[0].snapshot_id
        assert calls == [] and len(rows) == 1 and rows[0].config == "custom"
        assert rows[0].scores.hit_at == row(run, snap, "lexical").scores.hit_at


class TestSnapshots:
    async def test_a_building_snapshot_is_compared_only_on_what_it_holds(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        old = await snapshots.serving_snapshot(kb)
        kb, started = await edit(kb.id, chunking=Chunking(mode="fixed", target_tokens=64, max_tokens=64))
        assert started
        building = (await bases.states([kb]))[kb.id].build.snapshot_id
        run = await run_set(kb, made, snapshot_ids=[old, building])
        assert run.compared_cases == 0
        assert [s.comparable for s in run.snapshots] == [True, False]
        assert any("still building" in n for n in run.notes) and any("left out" in n for n in run.notes)

    async def test_two_snapshots_side_by_side_after_a_switch(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        old = await snapshots.serving_snapshot(kb)
        kb, _ = await edit(kb.id, chunking=Chunking(mode="fixed", target_tokens=64, max_tokens=64))
        await jobs.drain(run_ingest_job)
        new = await snapshots.serving_snapshot(kb)
        assert new != old
        run = await run_set(kb, made, snapshot_ids=[old, new])
        assert run.compared_cases == 3 and [s.status for s in run.snapshots] == ["retired", "active"]
        assert {r.snapshot_id for r in run.results} == {old, new}
        assert row(run, new, "served").scores.hit_at["10"] == 1.0  # the quotes still find their passages
        fp = run.model_dump()  # the settings each was built with travel with the run
        assert fp["snapshots"][0]["settings"]["chunking"]["mode"] == "structure"

    async def test_an_edited_document_keeps_its_question_while_the_words_are_there(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await write(kb, "policies/refunds.md", "# Refunds\n\nUpdated in October.\n\n" + POLICIES[
            "policies/refunds.md"].split("\n\n", 1)[1])
        await write(kb, "policies/security.md", "# Security\n\nKeys are now rotated every month.")
        await jobs.drain(run_ingest_job)
        run = await run_set(kb, made)
        snap = run.snapshots[0].snapshot_id
        assert (run.compared_cases, run.stale_cases) == (2, 1)
        assert [c.stale for c in run.cases] == [False, False, True]
        assert row(run, snap, "served").scores.cases == 2

    async def test_a_snapshot_of_another_base_or_a_failed_one_is_refused(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        with pytest.raises(EvalNotFound):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id, snapshot_ids=["ksnap_" + "f" * 32]))


class TestControl:
    async def test_a_cancelled_run_keeps_what_it_scored(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        real = search.search
        holder = {}

        async def then_cancel(*a, **kw):
            answer = await real(*a, **kw)
            if "run" in holder:
                await RUNS.cancel(kb.id, holder["run"])
            return answer

        monkeypatch.setattr(search, "search", then_cancel)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        holder["run"] = started.id
        await finished()
        run = await RUNS.get_run(kb.id, started.id)
        assert run.status == "cancelled" and run.done_count == 1 and len(run.cases) == 1

    async def test_a_run_cancelled_before_it_began_ends_cancelled(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await RUNS.cancel(kb.id, started.id)
        await finished()
        assert (await RUNS.get_run(kb.id, started.id)).status == "cancelled"

    async def test_a_cancel_between_a_runs_read_and_its_start_is_kept(self, jobs, monkeypatch):
        """Found on Postgres: the run read its row as queued, a cancel landed, then the run wrote "running" over
        it and completed. Its start is a compare-and-set now."""
        kb = await policies(jobs)
        made = await question_set(kb)
        real = RUNS._move

        async def cancel_first(run_id, from_statuses, **values):
            if values.get("status") == "running":
                async with session() as s:
                    await s.execute(update(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.id == run_id)
                                    .values(status="cancelling"))
            return await real(run_id, from_statuses, **values)

        monkeypatch.setattr(RUNS, "_move", cancel_first)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await finished()
        run = await RUNS.get_run(kb.id, started.id)
        assert run.status == "cancelled" and run.done_count == 0

    async def test_one_run_of_a_set_at_a_time(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "1" * 32, kb_id=kb.id, set_id=made.id, status="running"))
        with pytest.raises(EvalBusy):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))

    async def test_a_run_its_process_left_is_interrupted(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        async with session() as s:
            s.add(KnowledgeEvalRunDB(id="krun_" + "1" * 32, kb_id=kb.id, set_id=made.id, status="running",
                                     updated_at=long_ago))
            s.add(KnowledgeEvalRunDB(id="krun_" + "2" * 32, kb_id=kb.id, set_id="kset_other", status="queued"))
        assert (await RUNS.get_run(kb.id, "krun_" + "1" * 32)).status == "interrupted"
        assert await RUNS.mark_interrupted() == 1
        assert (await RUNS.get_run(kb.id, "krun_" + "2" * 32)).status == "interrupted"

    async def test_an_empty_set_is_refused(self, jobs):
        kb = await policies(jobs)
        made = await evals.create_set(kb.id, EvalSetCreate(name="Empty"))
        with pytest.raises(ValueError, match="no questions"):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))


class TestMoney:
    async def test_a_run_its_searches_would_cross_the_budget_is_refused_or_skipped(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)

        async def priced(model, texts):
            return 1.0

        monkeypatch.setattr("core.knowledge.budgets.embed_estimate_usd", priced)
        kb, _ = await edit(kb.id, budget_monthly_usd=0.5)
        with pytest.raises(ValueError, match="monthly budget"):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        skipped = await RUNS.start(kb, EvalRunRequest(set_id=made.id), started_by="settings")
        assert skipped.status == "skipped" and "monthly budget" in skipped.error
        assert RUNS.tasks() == {}

    async def test_a_search_at_the_budget_stops_the_run(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        real = search.search

        async def capped(*a, **kw):
            answer = await real(*a, **kw)
            return answer.model_copy(update={"degraded": [*answer.degraded, "budget_paused"]})

        monkeypatch.setattr(search, "search", capped)
        run = await run_set(kb, made)
        assert run.status == "failed" and "monthly budget" in run.error and run.done_count == 1

    async def test_the_runs_cost_is_what_its_session_logged(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        async with session() as s:
            s.add(UsageLogDB(session_id=f"knowledge-eval:{run.id}", estimated_cost=0.25, source="evals"))
            s.add(UsageLogDB(session_id="knowledge-eval:krun_other", estimated_cost=9.0, source="evals"))
        assert await RUNS.spent(run.id) == pytest.approx(0.25 + (run.cost_usd or 0.0))

    async def test_a_platform_search_costs_what_the_wallet_settled_once(self, jobs):
        """CCR-7: a platform search cost's usage_logs mirror reads 0; the run reads its search costs from the
        knowledge ledger (the settled charge, else the estimate), and only its chat calls from usage_logs."""
        from core.store.models import KnowledgeCostDB

        kb = await policies(jobs)
        made = await question_set(kb)
        run = await run_set(kb, made)
        before = await RUNS.spent(run.id)
        own = f"knowledge-eval:{run.id}"
        async with session() as s:
            s.add(KnowledgeCostDB(kb_id=kb.id, session_id=own, stage="rerank", cost_source="platform",
                                  model="synapse.cohere-rerank-3-5", unit="search_units", units=1, est_usd=0.002,
                                  charged_microusd=2100))
            s.add(UsageLogDB(session_id=own, estimated_cost=0.0, event_type="knowledge", source="evals"))  # its mirror
            s.add(KnowledgeCostDB(kb_id=kb.id, session_id=own, stage="embed", cost_source="byok", model="fake",
                                  unit="tokens", est_usd=0.0001))
            s.add(UsageLogDB(session_id=own, estimated_cost=0.0001, event_type="knowledge", source="evals"))
            s.add(UsageLogDB(session_id=own, estimated_cost=0.03, source="evals"))  # a judge's call
        assert await RUNS.spent(run.id) == pytest.approx(before + 0.0021 + 0.0001 + 0.03)

    async def test_an_estimate_names_what_counts_toward_the_budget(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        est = await RUNS.estimate(kb, EvalRunRequest(set_id=made.id))
        assert [line.part for line in est.lines] == ["search"] and est.lines[0].counts_toward_budget
        assert est.lines[0].calls == 3


class TestRoutes:
    async def test_a_run_through_the_api(self, jobs, client):
        kb = await policies(jobs)
        made = await question_set(kb)
        base = f"/api/knowledge/bases/{kb.id}/eval"
        est = await client.post(f"{base}/estimate", json={"run": {"set_id": made.id}})
        assert est.status_code == 200, est.text
        started = await client.post(f"{base}/runs", json={"set_id": made.id})
        assert started.status_code == 202, started.text
        await finished()
        run_id = started.json()["id"]
        got = await client.get(f"{base}/runs/{run_id}")
        assert got.status_code == 200 and got.json()["status"] == "completed" and len(got.json()["cases"]) == 3
        listed = await client.get(f"{base}/runs", params={"set_id": made.id})
        assert [r["id"] for r in listed.json()] == [run_id] and "cases" not in listed.json()[0]
        compared = await client.post(f"{base}/runs/{run_id}/compare", json={"weights": {"lexical": 2, "vector": 1}})
        assert compared.status_code == 200 and compared.json()[0]["config"] == "custom"
        assert (await client.post(f"{base}/runs/{run_id}/cancel")).status_code == 200
        assert (await client.get(f"{base}/runs/krun_{'f' * 32}")).status_code == 404
        sets = await client.get(f"{base}/sets")
        assert sets.json()[0]["last_run"]["id"] == run_id

    async def test_a_disabled_base_runs_nothing(self, jobs, client):
        kb = await policies(jobs)
        made = await question_set(kb)
        await edit(kb.id, status="disabled")
        resp = await client.post(f"/api/knowledge/bases/{kb.id}/eval/runs", json={"set_id": made.id})
        assert resp.status_code == 409
