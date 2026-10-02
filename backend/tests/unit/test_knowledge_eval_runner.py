"""Runs driven elsewhere (V1c, D174): with an `EvalRunner` set — cloud's worker jobs — a run is handed to the
runner, never spawned here; whether a run is alive is read from when it last reported; a cancel ends a run not yet
started at once and one under way at its next question; a switch and a re-run that must wait are the runner's; a
run's cost is the platform's settle plus bring-your-own estimates.

And what holds in both modes: a reader never overwrites a finished run, a run its driver left does not hold its
set, a run stops when its row is no longer running, a deadline ends a run keeping what it scored, and only the
one-active-run index reads as "busy". The real pipeline, SQL index and search, with a hashing embedder ($0).
SQLite always; Postgres with `SYNAPSE_TEST_POSTGRES_URL`."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from _fakes.knowledge_eval import Writer, finished, policies, question_set
from core.knowledge import dispatch, evals, evals_auto as AUTO, evals_generate as G, evals_runs as RUNS
from core.knowledge import bases, search, seams, snapshots
from core.knowledge.api_models import EvalDraftRequest, EvalRunRequest, KnowledgeBaseUpdate
from core.knowledge.evals import EvalBusy, EvalUnavailable
from core.knowledge.events import KnowledgeLifecycleEvent, emit
from core.knowledge.kb_settings import Retrieval
from core.knowledge.testing.fakes import HashingEmbedder, RecordingDispatcher
from core.store import session
from core.store.models import KnowledgeCostDB, KnowledgeEvalRunDB, KnowledgeQueryDB, UsageLogDB

pytestmark = pytest.mark.usefixtures("knowledge_store")


class Runner:
    """A worker queue that records what it was handed; `drive` is the worker."""

    def __init__(self):
        self.admitted: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []
        self.switched: list[str] = []
        self.later: list[tuple[str, str]] = []
        self.sessions: list[str] = []
        self.busy = False
        self.down = False
        self.platform = 0.0

    async def admit(self, kb_id, started_by):
        self.admitted.append((kb_id, started_by))
        if self.busy:
            raise EvalBusy("Your plan runs 1 evaluation at a time.")

    async def start(self, kb_id, run_id):
        if self.down:
            raise ConnectionError("the queue is unreachable")
        self.started.append((kb_id, run_id))

    async def after_switch(self, kb_id):
        self.switched.append(kb_id)

    async def rerun_later(self, kb_id, set_id):
        self.later.append((kb_id, set_id))

    async def platform_usd(self, session_id):
        self.sessions.append(session_id)
        return self.platform

    async def drive(self, **kw):
        while self.started:
            kb_id, run_id = self.started.pop(0)
            await RUNS.run(kb_id, run_id, **kw)


@pytest.fixture(autouse=True)
async def _on(monkeypatch):
    seams.reset_all()
    dispatch.reset()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    monkeypatch.setattr(snapshots, "BACKOFF_S", (0.0, 0.0))
    seams.set_knowledge_embedder(HashingEmbedder(dims=32))
    AUTO._pending.clear()
    yield
    RUNS.set_eval_runner(None)
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


@pytest.fixture
def runner():
    fake = Runner()
    RUNS.set_eval_runner(fake)
    return fake


async def runs_of(set_id: str) -> list[KnowledgeEvalRunDB]:
    async with session() as s:
        return list((await s.execute(select(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.set_id == set_id)
                                     .order_by(KnowledgeEvalRunDB.created_at))).scalars().all())


async def left(made, status: str, minutes: float, run_id: str = "krun_" + "1" * 32, **fields) -> str:
    """A run of `made` in `status`, last touched `minutes` ago."""
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with session() as s:
        s.add(KnowledgeEvalRunDB(id=run_id, kb_id=made.kb_id, set_id=made.id, status=status, updated_at=at,
                                 **fields))
    return run_id


async def eval_searches() -> int:
    async with session() as s:
        return len((await s.execute(select(KnowledgeQueryDB.id).where(KnowledgeQueryDB.surface == "eval"))).all())


class TestHandedToTheRunner:
    async def test_a_run_is_handed_to_the_runner_and_never_spawned_here(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        assert started.status == "queued" and RUNS.tasks() == {}
        assert runner.admitted == [(kb.id, "person")] and runner.started == [(kb.id, started.id)]
        await runner.drive()
        run = await RUNS.get_run(kb.id, started.id)
        assert (run.status, run.done_count, len(run.cases)) == ("completed", 3, 3)

    async def test_past_the_runners_limit_nothing_is_recorded(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        runner.busy = True
        with pytest.raises(EvalBusy, match="at a time"):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        assert await runs_of(made.id) == [] and runner.started == []

    async def test_a_run_the_runner_cannot_take_is_failed_and_the_set_is_free(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        runner.down = True
        with pytest.raises(EvalUnavailable):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        (row,) = await runs_of(made.id)
        assert (row.status, row.error) == ("failed", "The run could not be queued.")
        runner.down = False
        assert (await RUNS.start(kb, EvalRunRequest(set_id=made.id))).status == "queued"

    async def test_a_skipped_run_asks_nothing_of_the_runner(self, jobs, runner, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)

        async def priced(model, texts):
            return 1.0

        monkeypatch.setattr("core.knowledge.budgets.embed_estimate_usd", priced)
        current = await bases.require_base(kb.id)
        _, kb = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=current.revision, budget_monthly_usd=0.5))
        skipped = await RUNS.start(kb, EvalRunRequest(set_id=made.id), started_by="settings")
        assert skipped.status == "skipped" and runner.admitted == [] and runner.started == []


class TestCancel:
    async def test_a_run_not_yet_started_is_cancelled_at_once_and_its_job_does_nothing(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        assert (await RUNS.cancel(kb.id, started.id)).status == "cancelled"
        await runner.drive()
        run = await RUNS.get_run(kb.id, started.id)
        assert (run.status, run.done_count) == ("cancelled", 0) and await eval_searches() == 0

    async def test_a_run_under_way_stops_at_its_next_question(self, jobs, runner, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        real = search.search

        async def then_cancel(*a, **kw):
            answer = await real(*a, **kw)
            assert (await RUNS.cancel(kb.id, started.id)).status == "cancelling"  # its worker holds it
            return answer

        monkeypatch.setattr(search, "search", then_cancel)
        await runner.drive()
        run = await RUNS.get_run(kb.id, started.id)
        assert (run.status, run.done_count, len(run.cases)) == ("cancelled", 1, 1)


class TestAlive:
    @pytest.mark.parametrize("status,minutes,after", [
        ("running", 6, "running"),  # OSS would call this left: a worker may be on a judge's slow question
        ("running", 11, "interrupted"),
        ("cancelling", 11, "interrupted"),
        ("queued", 20, "queued"),  # waiting for a free worker
        ("queued", 26, "interrupted"),
    ])
    async def test_a_run_is_alive_while_it_reports(self, jobs, runner, status, minutes, after):
        kb = await policies(jobs)
        made = await question_set(kb)
        run_id = await left(made, status, minutes)
        run = await RUNS.get_run(kb.id, run_id)
        assert run.status == after
        if after == "interrupted":
            assert run.error == ("The run was not started in time." if status == "queued"
                                 else "The server stopped during the run.")

    async def test_startup_leaves_runs_to_their_workers(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        await left(made, "running", 0)
        assert await RUNS.mark_interrupted() == 0
        assert (await runs_of(made.id))[0].status == "running"


class TestBothModes:
    @pytest.mark.parametrize("driven", [False, True], ids=["oss", "runner"])
    async def test_a_run_its_driver_left_does_not_hold_its_set(self, jobs, driven):
        if driven:
            RUNS.set_eval_runner(Runner())
        kb = await policies(jobs)
        made = await question_set(kb)
        await left(made, "running", 11)
        await left(made, "completed", 60, run_id="krun_" + "2" * 32)  # history, never "interrupted"
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await finished()
        statuses = {r.id: r.status for r in await runs_of(made.id)}
        assert statuses["krun_" + "1" * 32] == "interrupted" and started.id in statuses
        assert statuses["krun_" + "2" * 32] == "completed"

    async def test_nor_can_it_stop_its_set_being_deleted(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        await left(made, "running", 11)
        await evals.delete_set(kb.id, made.id)
        assert await runs_of(made.id) == []

    async def test_a_readers_mark_never_overwrites_a_finished_run(self, jobs, monkeypatch):
        """The run finished between a reader's read of its stale row and the reader's mark."""
        kb = await policies(jobs)
        made = await question_set(kb)
        run_id = await left(made, "running", 11)
        real = RUNS.reap

        async def finishing_first(**kw):
            async with session() as s:  # the time it is stamped with is left as it was: only the status says
                await s.execute(update(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.id == run_id)
                                .values(status="completed",
                                        updated_at=datetime.now(timezone.utc) - timedelta(minutes=11)))
            return await real(**kw)

        monkeypatch.setattr(RUNS, "reap", finishing_first)
        await RUNS.get_run(kb.id, run_id)
        (row,) = await runs_of(made.id)
        assert row.status == "completed"  # what the run wrote, not the reader's "interrupted"

    async def test_a_run_whose_base_was_deleted_stops_searching(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        real = search.search

        async def then_forget(*a, **kw):
            answer = await real(*a, **kw)
            await evals.forget_base(kb.id)
            return answer

        monkeypatch.setattr(search, "search", then_forget)
        await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await finished()
        assert await runs_of(made.id) == [] and await eval_searches() == 1

    async def test_a_run_a_reader_marked_interrupted_stops_and_stays_so(self, jobs, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        real = search.search

        async def then_marked(*a, **kw):
            answer = await real(*a, **kw)
            async with session() as s:
                await s.execute(update(KnowledgeEvalRunDB).values(status="interrupted"))
            return answer

        monkeypatch.setattr(search, "search", then_marked)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await finished()
        (row,) = await runs_of(made.id)
        assert row.id == started.id and row.status == "interrupted" and row.results is None
        assert await eval_searches() == 1

    async def test_the_deadline_ends_a_run_keeping_what_it_scored(self, jobs, runner, monkeypatch):
        kb = await policies(jobs)
        made = await question_set(kb)
        ticks = iter([0.0])  # before the first question; past the deadline from the second on
        monkeypatch.setattr(RUNS, "_clock", lambda: next(ticks, 200.0))
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await runner.drive(deadline=100.0)
        run = await RUNS.get_run(kb.id, started.id)
        assert run.status == "failed" and "time limit at question 2 of 3" in run.error
        assert (run.done_count, len(run.cases)) == (1, 1) and run.results

    async def test_only_the_one_active_run_index_reads_as_busy(self, jobs):
        kb = await policies(jobs)
        made = await question_set(kb)
        with pytest.raises(IntegrityError):  # a check, not a run under way
            await RUNS.start(kb, EvalRunRequest(set_id=made.id), started_by="a cron job")
        assert await runs_of(made.id) == []
        await left(made, "running", 0)
        with pytest.raises(EvalBusy):
            await RUNS.start(kb, EvalRunRequest(set_id=made.id))


class TestReRuns:
    async def test_a_switch_is_handed_to_the_runner(self, jobs, runner):
        AUTO.install()
        kb = await policies(jobs)
        await emit(KnowledgeLifecycleEvent(id="kev_" + "1" * 32, type="knowledge.base.reindexed",
                                           created_at="2026-10-02T00:00:00+00:00", kb_id=kb.id))
        assert runner.switched == [kb.id] and AUTO.waiting() == {}

    async def test_a_re_run_that_must_wait_is_handed_to_the_runner(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await runner.drive()
        await left(made, "running", 0, run_id="krun_" + "9" * 32, started_by="person")
        current = await bases.require_base(kb.id)
        before, after = await bases.update_base(kb.id, KnowledgeBaseUpdate(revision=current.revision,
                                                                           retrieval=Retrieval(top_k=11)))
        await AUTO.settings_changed(before, after)
        assert runner.later == [(kb.id, made.id)] and AUTO._pending == {}

        async with session() as s:
            await s.execute(update(KnowledgeEvalRunDB).where(KnowledgeEvalRunDB.id == "krun_" + "9" * 32)
                            .values(status="completed"))
        made_now = await AUTO.rerun_set(after, made.id)  # what the runner does once a run ends
        assert made_now and runner.started == [(kb.id, made_now)]

    async def test_an_automatic_run_already_measuring_what_is_served_is_kept(self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await runner.drive()
        (first,) = await AUTO.rerun(kb.id)
        assert await AUTO.rerun(kb.id) == []  # a second trigger for the same index and settings
        statuses = {r.id: r.status for r in await runs_of(made.id)}
        assert statuses[first] == "queued" and len(statuses) == 2 and runner.later == []


class TestCost:
    async def test_the_platforms_settle_replaces_its_rows_and_bring_your_own_keeps_its_estimates(
            self, jobs, runner):
        kb = await policies(jobs)
        made = await question_set(kb)
        started = await RUNS.start(kb, EvalRunRequest(set_id=made.id))
        await runner.drive()
        own = RUNS.session_of(started.id)
        before = await RUNS.spent(started.id)
        assert runner.sessions[-1] == own
        async with session() as s:
            # a platform search whose charge the engine never heard: the settle is in the platform's ledger
            s.add(KnowledgeCostDB(kb_id=kb.id, session_id=own, stage="embed", cost_source="platform",
                                  model="synapse.titan-embed-v2", unit="tokens", est_usd=0.002))
            s.add(KnowledgeCostDB(kb_id=kb.id, session_id=own, stage="rerank", cost_source="platform",
                                  model="synapse.cohere-rerank-3-5", unit="search_units", units=1,
                                  est_usd=0.002, charged_microusd=2500))
            s.add(KnowledgeCostDB(kb_id=kb.id, session_id=own, stage="embed", cost_source="byok", model="fake",
                                  unit="tokens", est_usd=0.0001))
            s.add(UsageLogDB(session_id=own, estimated_cost=0.0, source="evals"))  # a platform judge's call
            s.add(UsageLogDB(session_id=own, estimated_cost=0.03, source="evals"))  # a bring-your-own one
        runner.platform = 0.0125  # what the platform settled for the session, searches and chat alike
        assert await RUNS.spent(started.id) == pytest.approx(before + 0.0125 + 0.0001 + 0.03)


class TestDraftDeadline:
    async def test_drafts_finished_by_the_deadline_are_kept_and_the_rest_timed_out(self, jobs, monkeypatch):
        import time

        kb = await policies(jobs)
        quick = Writer()

        async def writer(*args, **kwargs):
            if "Beacon" in (kwargs.get("prompt_msg") or ""):
                await asyncio.sleep(30)
            return await quick(*args, **kwargs)

        monkeypatch.setattr("core.llm_providers.generate_response", writer)
        monkeypatch.setattr(G, "CONCURRENCY", 10)
        drafts = await G.draft(kb, EvalDraftRequest(model="writer-1", direct=4, paraphrase=0, multi_hop=0, seed=7), {"model": "writer-1"},
                               deadline=time.monotonic() + 1.0)
        assert len(drafts.drafts) == 2 and drafts.dropped == {"timed_out": 2}
        assert all("Beacon" not in d.question + "".join(e.quote for e in d.evidence) for d in drafts.drafts)
