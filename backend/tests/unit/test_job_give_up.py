"""
A job cut off too many times gives up by itself, and a chat turn is not rerun (F89).

ARQ hands a cancelled job out again — a worker shutting down, a deploy — and F56
made each run job resume from its checkpoint. But once a job has been handed out
`max_tries` times, ARQ fails the next delivery **without calling the function**:
nothing of ours runs, so the run stayed `running` with no dead letter, and only
an API restart's sweep ended it. Now (Naveen, D112) the worker takes one more
delivery than the retry policy (`WORKER_MAX_RETRIES`), and on that extra one the
job does not do the work: it fails the run and dead-letters it, so Retry resumes
it on purpose.

A chat turn has no checkpoint, so one handed out again used to rerun from the
start — tools it had already called included. It is not rerun now: the turn is
marked interrupted, on the stream and in the conversation, and the person
decides whether to send it again.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def worker(monkeypatch):
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    async def acquire():
        return _server()

    async def release():
        return None

    async def no_settings():
        return None

    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", release)
    monkeypatch.setattr(worker_mod, "_load_job_settings", no_settings)
    monkeypatch.delenv("WORKER_MAX_RETRIES", raising=False)
    return worker_mod


async def _ctx(fake_redis, job_try: int) -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(),
            "job_try": job_try, "job_id": f"job-{job_try}"}


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


async def _dead_letters() -> list:
    from sqlalchemy import select
    from core.scale.models_db import DeadLetterQueueDB
    from core.store import get_store
    async with (await get_store())() as session:
        return list((await session.execute(select(DeadLetterQueueDB))).scalars())


async def _running(run_id, orch_id, step="s1"):
    from core.store import get_store, upsert
    from core.store.models import OrchestrationRunDB
    async with (await get_store())() as session:
        await upsert(session, OrchestrationRunDB, values={
            "run_id": run_id, "orchestration_id": orch_id, "status": "running",
            "current_step_id": step, "shared_state": {"user_input": "go"}, "step_history": [],
        }, index_elements=["run_id"])
        await session.commit()


@pytest.fixture
async def orch(seed_orchestration):
    return await seed_orchestration(id="orch_giveup", entry_step_id="s1", steps=[
        {"id": "s1", "name": "S1", "type": "print", "print_content": "ran", "output_key": "o_s1",
         "next_step_id": None}])


class TestTheWorkerTakesOneMoreDelivery:
    def test_than_the_retry_policy(self, worker, monkeypatch):
        """Its extra delivery is the one that records the job gave up."""
        monkeypatch.setenv("WORKER_MAX_RETRIES", "5")
        assert worker.job_retries({}) == 5
        assert worker.job_retries({"job_retries": 2}) == 2, "an embedder's own policy wins"
        monkeypatch.delenv("WORKER_MAX_RETRIES")
        assert worker.WorkerSettings.max_tries == worker.job_retries({}) + 1


class TestAJobPastTheRetryPolicyGivesUp:
    @pytest.mark.parametrize("job", ["run_orchestration_job", "resume_orchestration_job", "resume_failed_job"])
    async def test_the_run_is_failed_and_dead_lettered_not_run(self, worker, fake_redis, orch, job):
        await _running("run_giveup", "orch_giveup")
        ctx = await _ctx(fake_redis, worker.job_retries({}) + 1)
        kwargs = {"run_id": "run_giveup"}
        if job == "run_orchestration_job":
            kwargs.update(orch_id="orch_giveup", initial_input="go")
        elif job == "resume_orchestration_job":
            kwargs.update(human_response="yes")

        result = await getattr(worker, job)(ctx, **kwargs)

        assert result["status"] == "failed"
        run = await _run("run_giveup")
        assert run.status == "failed"
        assert "o_s1" not in run.shared_state, "the work was not run again"
        letters = [d for d in await _dead_letters() if d.run_id == "run_giveup"]
        assert len(letters) == 1
        assert letters[0].job_function == job
        assert "without finishing" in letters[0].error_message
        assert letters[0].attempt_count == worker.job_retries({}) + 1
        # And on the run itself, which every reader asks (F430): it read
        # `failed` with no reason, on the run page, v1 and v2 alike.
        from core.orchestration.run_error import failure_reason
        assert failure_reason(run.status, run.step_history, run.step_progress) == letters[0].error_message

    async def test_a_retry_clears_the_reason_it_gave_up_with(self, worker, fake_redis, orch):
        from core.orchestration.run_error import run_error

        await _running("run_retried", "orch_giveup")
        await worker.run_orchestration_job(await _ctx(fake_redis, worker.job_retries({}) + 1),
                                           run_id="run_retried", orch_id="orch_giveup", initial_input="go")
        assert "without finishing" in (run_error(await _run("run_retried")) or "")

        result = await worker.resume_failed_job(await _ctx(fake_redis, 1), run_id="run_retried")

        assert result["status"] == "completed"
        assert run_error(await _run("run_retried")) is None

    async def test_a_job_that_dies_outside_the_engine_leaves_its_reason(self, worker, fake_redis):
        """Not the give-up's words, but still on the run — and in a person's words."""
        from core.orchestration.run_error import failure_reason

        with pytest.raises(Exception):
            await worker.run_orchestration_job(await _ctx(fake_redis, 1), run_id="run_gone",
                                               orch_id="orch_gone", initial_input="go")

        run = await _run("run_gone")
        reason = failure_reason(run.status, run.step_history, run.step_progress)
        assert run.status == "failed"
        assert reason and "orch_gone" in reason and "no longer exists" in reason
        assert "Sync" not in reason and "Postgres" not in reason and "Error:" not in reason

    async def test_within_the_policy_the_run_still_resumes(self, worker, fake_redis, orch):
        await _running("run_within", "orch_giveup")
        result = await worker.run_orchestration_job(
            await _ctx(fake_redis, worker.job_retries({})),
            run_id="run_within", orch_id="orch_giveup", initial_input="go")

        assert result["status"] == "completed"
        assert (await _run("run_within")).shared_state["o_s1"] == "ran"


async def _progress(run_id, *, history: int = 0, turn: int | None = None, step="s1"):
    """The run as a stopped delivery left it: `history` steps ended, its current step on `turn`."""
    from core.store import get_store, upsert
    from core.store.models import OrchestrationRunDB
    async with (await get_store())() as session:
        await upsert(session, OrchestrationRunDB, values={
            "run_id": run_id, "orchestration_id": "orch_giveup", "status": "running", "current_step_id": step,
            "shared_state": {"user_input": "go"},
            "step_history": [{"step_id": f"done{i}", "status": "completed"} for i in range(history)],
            "step_progress": {step: {"turn": turn}} if turn is not None else {},
        }, index_elements=["run_id"])
        await session.commit()


class TestOnlyDeliveriesWithoutProgressCount:
    """F376: every deploy or crash hands a long run out again, and it resumed — but the fourth delivery gave up
    whatever the run had done in between, so a 6-hour run met its end at its third restart."""

    async def _deliver(self, worker, fake_redis, job_try: int):
        ctx = {**await _ctx(fake_redis, job_try), "job_id": "run_long"}
        await fake_redis.set("arq:retry:run_long", job_try)  # as ARQ counts it, before it calls the job
        return ctx, await worker._gave_up(ctx, "run_orchestration_job", "run_long", "orch_giveup")

    async def test_a_run_that_got_further_each_time_is_not_given_up(self, worker, fake_redis, orch):
        last = worker.job_retries({}) + 1
        for job_try, steps in enumerate(range(last), start=1):
            await _progress("run_long", history=steps)
            ctx, gave_up = await self._deliver(worker, fake_redis, job_try if job_try < 3 else 2)
            assert gave_up is False, f"delivery {job_try} after {steps} steps"
        assert [d for d in await _dead_letters() if d.run_id == "run_long"] == []
        assert int(await fake_redis.get("arq:retry:run_long")) == 1, "ARQ's count starts again from progress"
        assert ctx["job_try"] == 2, "the job still knows it was handed out again (it resumes, not reruns)"

    async def test_progress_inside_an_agent_step_counts(self, worker, fake_redis, orch):
        await _progress("run_long", history=2, turn=3)
        await self._deliver(worker, fake_redis, 1)
        await _progress("run_long", history=2, turn=5)
        _, gave_up = await self._deliver(worker, fake_redis, worker.job_retries({}) + 1)
        assert gave_up is False

    async def test_deliveries_in_a_row_without_progress_still_give_up(self, worker, fake_redis, orch):
        await _progress("run_long", history=4)
        for job_try in range(1, worker.job_retries({}) + 1):
            _, gave_up = await self._deliver(worker, fake_redis, job_try)
            assert gave_up is False
        _, gave_up = await self._deliver(worker, fake_redis, worker.job_retries({}) + 1)
        assert gave_up is True
        [letter] = [d for d in await _dead_letters() if d.run_id == "run_long"]
        assert "in a row without finishing" in letter.error_message

    async def test_a_shorter_history_or_an_earlier_turn_is_not_progress(self, worker, fake_redis, orch):
        """A resume drops a failed step's entry, and a step's turns start again after three attempts."""
        await _progress("run_long", history=4, turn=6)
        await self._deliver(worker, fake_redis, 1)
        await _progress("run_long", history=3, turn=7)
        await self._deliver(worker, fake_redis, 2)
        await _progress("run_long", history=3, turn=1)
        await self._deliver(worker, fake_redis, 3)
        _, gave_up = await self._deliver(worker, fake_redis, worker.job_retries({}) + 1)
        assert gave_up is True


class TestAChatTurnHandedOutAgain:
    async def test_is_not_rerun_and_says_so(self, worker, fake_redis, monkeypatch):
        import core.react_engine as re_mod
        ran = []

        async def loop(*args, **kwargs):
            ran.append(True)
            yield {"type": "final", "response": "answered"}

        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        await S.seed_agents([S.make_agent(id="a1")])

        result = await worker.run_agent_chat_job(
            await _ctx(fake_redis, 2), session_id="s_chat", agent_id="a1", message="email the team")

        assert result["status"] == "interrupted"
        assert ran == [], "the turn ran again from the start"
        events = [fields for _, fields in await fake_redis.xrange("synapse:chat:s_chat:events")]
        text = " ".join(str(v) for e in events for v in e.values())
        assert "interrupted" in text.lower()
        history = await worker._load_chat_history(
            (await _ctx(fake_redis, 2))["session_factory"], "s_chat", "a1")
        assert history[-2] == {"role": "user", "content": "email the team"}
        assert "interrupted" in history[-1]["content"].lower() and history[-1]["role"] == "assistant"
