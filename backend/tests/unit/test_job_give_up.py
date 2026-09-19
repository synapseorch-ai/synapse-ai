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

    async def test_within_the_policy_the_run_still_resumes(self, worker, fake_redis, orch):
        await _running("run_within", "orch_giveup")
        result = await worker.run_orchestration_job(
            await _ctx(fake_redis, worker.job_retries({})),
            run_id="run_within", orch_id="orch_giveup", initial_input="go")

        assert result["status"] == "completed"
        assert (await _run("run_within")).shared_state["o_s1"] == "ran"


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
