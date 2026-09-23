"""
Two answers to one question that arrive at the same instant: one applies (F136).

F111 made an answer name its question, which closed the *sequential* case — a
redelivered job, or a second answer run after the first had finished. Two jobs
picked up in one poll were not sequential: both read the run while it was still
asking, both passed F111's check, and both drove the engine from the same
checkpoint. Measured live on the fleet: every step after the question ran
twice, and the stored answer was whichever job wrote last. The in-process
answer had the same window, across the `await` between its "is a pump already
running?" check and the spawn.

Each case asserts on what ran *after* the question, not on the final state,
which looks right either way.
"""
import asyncio
import json
import types

import pytest

from _fakes import seed as S
from test_human_in_containers import _ctx, _human, _loop, _print, _run, _server, worker  # noqa: F401


def _asking_loop(orch_id: str) -> dict:
    """A loop whose body asks, then does something that shows it ran."""
    return S.make_orchestration(id=orch_id, entry_step_id="loop1", steps=[
        _loop("loop1", ["ask", "after"], nxt="tail"),
        _human("ask"), _print("after"), _print("tail"),
    ])


async def _stream(fake_redis, run_id: str) -> list[dict]:
    entries = await fake_redis.xrange(f"synapse:run:{run_id}:events")
    return [json.loads(fields[b"data"]) for _, fields in entries]


def _ran(events: list[dict], step_id: str) -> int:
    return sum(1 for e in events if e.get("type") == "step_complete" and e.get("orch_step_id") == step_id)


def _together(monkeypatch, module, name: str, parties: int = 2):
    """Hold each caller of `module.name` until `parties` of them have arrived.

    Both answers then read the run before either has changed it — the one-poll
    pickup the live proof saw, made deterministic. A caller that never arrives
    (a refused answer returns before it) releases the others after a second.
    """
    real = getattr(module, name)
    arrived = 0
    everyone = asyncio.Event()

    async def held(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived >= parties:
            everyone.set()
        try:
            await asyncio.wait_for(everyone.wait(), 1)
        except asyncio.TimeoutError:
            everyone.set()
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, held)


class TestOnTheFleet:
    async def test_two_answer_jobs_picked_up_together_apply_one(self, worker, fake_redis, monkeypatch):
        orch = _asking_loop("orch_f136")
        await S.seed_orchestrations([orch])
        started = await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f136", orch_id="orch_f136", initial_input="go")
        assert started["status"] == "paused"
        before = await _stream(fake_redis, "run_f136")

        from core.orchestration.pauses import pause_ordinal
        asking = pause_ordinal((await _run("run_f136")).step_history)
        _together(monkeypatch, worker, "_load_orchestration")

        async def answer(text: str, job_id: str) -> dict:
            ctx = {**await _ctx(fake_redis), "job_id": job_id}
            return await worker.resume_orchestration_job(
                ctx, run_id="run_f136", human_response=text, pause_ordinal=asking)

        results = await asyncio.gather(answer("one", "resume_a"), answer("two", "resume_b"))

        events = (await _stream(fake_redis, "run_f136"))[len(before):]
        assert sum(1 for e in events if e.get("type") == "worker_picked_up") == 1, (
            "only the answer that claimed the run picks it up")
        assert _ran(events, "after") == 1, "the step after the question ran once"
        assert sum(1 for e in events if e.get("type") == "human_input_required") == 1
        applied = [r for r in results if r.get("applied", True)]
        assert len(applied) == 1, results
        assert applied[0]["status"] == "paused"

        run = await _run("run_f136")
        assert run.waiting_for_human and run.status == "paused", "iteration two asks"
        assert [h["status"] for h in run.step_history if h.get("step_id") == "ask"] == [
            "completed", "paused"]
        assert run.shared_state["answer"] in ("one", "two"), "one answer, mid-loop"


class TestInProcess:
    async def test_two_answers_at_once_resume_the_run_once(self, monkeypatch):
        from core.orchestration import answers
        from core.orchestration.runner import SENTINEL
        from core.orchestration.state import SharedState

        orch = _asking_loop("orch_f136_local")
        await S.seed_orchestrations([orch])
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        events = [e async for e in engine.run("go", run_id="run_f136_local")]
        assert events[-1]["type"] == "human_input_required"

        _together(monkeypatch, SharedState, "restore")
        app_state = types.SimpleNamespace(server_module=_server())

        async def answer(text: str):
            try:
                return await answers.answer_paused_run(app_state, "run_f136_local", text)
            except answers.RunAlreadyActive as refused:
                return refused

        outcomes = await asyncio.gather(answer("one"), answer("two"))

        resumed = [o for o in outcomes if isinstance(o, answers.Answered)]
        refused = [o for o in outcomes if isinstance(o, answers.RunAlreadyActive)]
        drained: list[dict] = []
        for outcome in resumed:
            while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
                drained.append(event)

        assert _ran(drained, "after") == 1, "the step after the question ran once"
        assert (len(resumed), len(refused)) == (1, 1), "the second answer is refused, not queued"
        run = await _run("run_f136_local")
        assert run.waiting_for_human and run.status == "paused"
        assert [h["status"] for h in run.step_history if h.get("step_id") == "ask"] == [
            "completed", "paused"]


class TestTheAppsEnqueue:
    async def test_an_answer_arq_dropped_is_refused_not_reported_as_queued(
        self, worker, fake_redis, monkeypatch,
    ):
        """ARQ keeps one job per id, and an answer's id was the run and the second
        it was given in: a second answer that second was dropped and its caller
        told it had been queued."""
        import unittest.mock as _m
        from core.orchestration import answers
        from core.store import get_store
        monkeypatch.setattr(worker, "_worker_id", "worker-f136")
        await S.seed_orchestrations([_asking_loop("orch_f136_enqueue")])
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f136_enqueue", orch_id="orch_f136_enqueue",
            initial_input="go")
        app_state = types.SimpleNamespace(
            redis=fake_redis, arq_redis=_m.AsyncMock(), pg_session_factory=await get_store())

        assert (await answers.answer_paused_run(app_state, "run_f136_enqueue", "one")).on_fleet
        _, kwargs = app_state.arq_redis.enqueue_job.call_args
        assert kwargs["_job_id"].startswith("resume_run_f136_enqueue_")

        app_state.arq_redis.enqueue_job.return_value = None
        with pytest.raises(answers.RunAlreadyActive):
            await answers.answer_paused_run(app_state, "run_f136_enqueue", "two")


class TestTheClaim:
    """`state_store.claim_answer` on the shapes a run can be in when an answer arrives."""

    @staticmethod
    async def _paused_loop(worker, fake_redis, run_id: str) -> int:
        from core.orchestration.pauses import pause_ordinal
        await S.seed_orchestrations([_asking_loop(f"orch_{run_id}")])
        started = await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id=run_id, orch_id=f"orch_{run_id}", initial_input="go")
        assert started["status"] == "paused"
        return pause_ordinal((await _run(run_id)).step_history)

    @staticmethod
    async def _claim(run_id: str, ordinal, job_id: str = "resume_x", redelivery: bool = False) -> bool:
        from core.orchestration.state_store import claim_answer
        from core.store import get_store
        return await claim_answer(await get_store(), run_id, pause_ordinal=ordinal,
                                  job_id=job_id, worker_id="w1", redelivery=redelivery)

    @staticmethod
    async def _set(run_id: str, **values) -> None:
        from sqlalchemy import update
        from core.store import get_store
        from core.store.models import OrchestrationRunDB as Run
        async with (await get_store())() as s:
            await s.execute(update(Run).where(Run.run_id == run_id).values(**values))
            await s.commit()

    async def test_one_claim_per_question(self, worker, fake_redis):
        asking = await self._paused_loop(worker, fake_redis, "run_claim1")
        assert await self._claim("run_claim1", asking)
        assert not await self._claim("run_claim1", asking, job_id="resume_y")
        run = await _run("run_claim1")
        assert run.status == "running" and not run.waiting_for_human

    async def test_a_claim_for_another_question_changes_nothing(self, worker, fake_redis):
        """The winner answered, carried on and paused at the next question before
        this answer's claim landed: the run is paused again, but not on its question."""
        asking = await self._paused_loop(worker, fake_redis, "run_claim2")
        assert not await self._claim("run_claim2", asking + 1)
        run = await _run("run_claim2")
        assert run.status == "paused" and run.waiting_for_human, "the refused claim rolled back"

    async def test_an_answer_that_names_no_question_is_applied(self, worker, fake_redis):
        await self._paused_loop(worker, fake_redis, "run_claim3")
        assert await self._claim("run_claim3", None)

    async def test_a_redelivered_try_takes_back_its_own_claim(self, worker, fake_redis):
        """Its first try claimed the run and stopped before the answer reached a
        checkpoint — or the API's startup sweep then failed the run."""
        asking = await self._paused_loop(worker, fake_redis, "run_claim4")
        assert await self._claim("run_claim4", asking, job_id="resume_first")
        assert not await self._claim("run_claim4", asking, job_id="resume_other", redelivery=True)
        assert await self._claim("run_claim4", asking, job_id="resume_first", redelivery=True)
        await self._set("run_claim4", status="failed")
        assert await self._claim("run_claim4", asking, job_id="resume_first", redelivery=True)
        assert not await self._claim("run_claim4", asking, job_id="resume_first"), (
            "a first try never matches a claim by job id")

    async def test_the_job_whose_first_try_claimed_and_stopped_applies_the_answer(
        self, worker, fake_redis,
    ):
        """Through the job: its first try's claim is on the row, the answer never
        reached a checkpoint. ARQ hands out the same job id again; another job's
        answer, arriving now, finds the run taken."""
        asking = await self._paused_loop(worker, fake_redis, "run_claim6")
        assert await self._claim("run_claim6", asking, job_id="resume_first")

        other = {**await _ctx(fake_redis, 1), "job_id": "resume_other"}
        refused = await worker.resume_orchestration_job(
            other, run_id="run_claim6", human_response="other", pause_ordinal=asking)
        assert refused["applied"] is False

        again = {**await _ctx(fake_redis, 2), "job_id": "resume_first"}
        result = await worker.resume_orchestration_job(
            again, run_id="run_claim6", human_response="first", pause_ordinal=asking)
        assert result["status"] == "paused" and result.get("applied", True)
        run = await _run("run_claim6")
        assert run.shared_state["answer"] == "first"
        assert [h["status"] for h in run.step_history if h.get("step_id") == "ask"] == [
            "completed", "paused"]

    async def test_a_cancelled_run_is_not_claimed(self, worker, fake_redis):
        """Cancelling leaves `waiting_for_human` set (core/orchestration/cancel.py)."""
        asking = await self._paused_loop(worker, fake_redis, "run_claim5")
        await self._set("run_claim5", status="cancelled")
        assert not await self._claim("run_claim5", asking)


class TestANestedRunsSecondQuestion:
    """Each question a nested run asks is its own question (F140).

    The parent waits on one step for the whole sub-conversation, and its pause
    entry used to be one entry for all of it — so `pause_ordinal` named the
    same index however many questions the child asked, and an answer queued
    behind the first was applied to the second. A Loop's second question has
    always opened an entry of its own, which is why F111 held there.
    """

    @staticmethod
    async def _seed(orch_id: str, agent_id: str) -> dict:
        from test_human_in_containers import _delegate
        inner = S.make_orchestration(id=f"{orch_id}_inner", entry_step_id="h1", steps=[
            _human("h1", key="first", nxt="h2"), _human("h2", key="second", nxt="p"),
            _print("p", "got {state.first} and {state.second}", key="final"),
        ])
        parent = S.make_orchestration(id=orch_id, entry_step_id="delegate",
                                      steps=[_delegate(agent_id, "tail"), _print("tail")])
        await S.seed_agents([S.make_orchestrator_agent(f"{orch_id}_inner", id=agent_id)])
        await S.seed_orchestrations([parent, inner])
        return parent

    async def test_a_queued_second_answer_does_not_answer_the_childs_next_question(
        self, worker, fake_redis,
    ):
        await self._seed("orch_f140", "agent_f140_inner")
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f140", orch_id="orch_f140", initial_input="go")
        from core.orchestration.pauses import pause_ordinal
        asking = pause_ordinal((await _run("run_f140")).step_history)

        first = {**await _ctx(fake_redis), "job_id": "resume_1"}
        second = {**await _ctx(fake_redis), "job_id": "resume_2"}
        assert (await worker.resume_orchestration_job(
            first, run_id="run_f140", human_response="a", pause_ordinal=asking))["status"] == "paused"
        await worker.resume_orchestration_job(
            second, run_id="run_f140", human_response="a again", pause_ordinal=asking)

        child = await _run("run_f140__delegate_d1")
        assert "second" not in child.shared_state, "the child's second question is still open"

    async def test_the_ordinal_moves_with_the_question(self, worker, fake_redis):
        """In-process too: the engine writes the entry, not the fleet."""
        from core.orchestration.engine import OrchestrationEngine
        from core.models_orchestration import Orchestration
        from core.orchestration.pauses import pause_ordinal

        parent = await self._seed("orch_f140b", "agent_f140b_inner")
        engine = OrchestrationEngine(Orchestration.model_validate(parent), _server())
        [e async for e in engine.run("go", run_id="run_f140b")]
        first = pause_ordinal((await _run("run_f140b")).step_history)

        [e async for e in OrchestrationEngine.resume("run_f140b", "a", _server())]
        history = (await _run("run_f140b")).step_history
        second = pause_ordinal(history)

        assert second is not None and second != first, "the next question is a new entry"
        assert history[first]["status"] == "completed", "the answered one is closed"

    async def test_the_delegate_is_still_one_priced_step(self, worker, fake_redis):
        """A person's wait is not another execution of the step it waits in.

        The entry is typed `human`, which is what every pricing and counting
        rule already treats as free — and what keeps the loop guard from
        spending a delegate's iterations on how talkative its sub-run was.
        """
        await self._seed("orch_f140c", "agent_f140c_inner")
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_f140c", orch_id="orch_f140c", initial_input="go")
        await worker.resume_orchestration_job(
            await _ctx(fake_redis), run_id="run_f140c", human_response="a")

        history = (await _run("run_f140c")).step_history
        delegate = [h for h in history if h["step_id"] == "delegate"]
        assert len(delegate) == 2, delegate
        assert [h["step_type"] for h in delegate] == ["agent", "human"]
