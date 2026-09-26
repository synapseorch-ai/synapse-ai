"""
A job ARQ hands out again resumes the run from its checkpoint (F56).

ARQ 0.28 runs a job a second time only after the first attempt was cancelled —
a worker shutting down, a deploy, a scale-in — or killed. `ctx["job_try"]` is
then 2 or more. Until F56 every job function ignored it: `run_orchestration_job`
started the run from its entry step again, overwriting the checkpoint and
repeating whatever the finished steps had already done (a sent email, a
charge), and `resume_orchestration_job` wrote the person's answer into whatever
step the run had reached by then.

These drive the real job functions with the real engine, fakeredis and the
SQLite store; only the agent loop and the MCP pool are faked. The cancel is a
plain `task.cancel()`, which is what ARQ's shutdown does to a job it is not
going to wait for.
"""
import asyncio
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent(sid, agent_id, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "output_key": f"o_{sid}", "next_step_id": nxt, "max_turns": 3}


def _print(sid, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid,
            "output_key": f"o_{sid}", "next_step_id": nxt}


def _human(sid, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "human", "human_prompt": "ok?",
            "output_key": "answer", "next_step_id": nxt}


class _Agents:
    """run_agent_step, counting calls per agent; named agents block on their first call."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.block_once: set[str] = set()
        self.started = asyncio.Event()

    async def __call__(self, **kwargs):
        agent_id = kwargs["agent_id"]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        if agent_id in self.block_once:
            self.block_once.discard(agent_id)
            self.started.set()
            await asyncio.sleep(30)
        yield {"type": "final", "response": f"{agent_id} answered"}


@pytest.fixture
def agents(monkeypatch):
    import core.react_engine as re_mod
    fake = _Agents()
    monkeypatch.setattr(re_mod, "run_agent_step", fake)
    return fake


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
    return worker_mod


async def _ctx(fake_redis, job_try: int) -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(),
            "job_try": job_try, "job_id": f"job-{job_try}"}


async def _interrupted(coro, agents) -> None:
    """Start a job, wait until the blocking step is running, cancel it as a shutdown would."""
    job = asyncio.create_task(coro)
    await asyncio.wait_for(agents.started.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    agents.started.clear()


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _history(run) -> list[tuple[str, str]]:
    return [(h["step_id"], h["status"]) for h in run.step_history]


class TestARedeliveredRunResumes:
    async def test_the_steps_that_finished_are_not_run_again(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        orch = S.make_orchestration(id="orch_redeliver", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2", "s3"), _print("s3"),
        ])
        await seed_orchestration(**orch)
        agents.block_once.add("a2")
        args = dict(run_id="run_redeliver", orch_id="orch_redeliver", initial_input="go")

        await _interrupted(worker.run_orchestration_job(await _ctx(fake_redis, 1), **args), agents)
        interrupted = await _run("run_redeliver")
        assert interrupted.status == "running"
        assert interrupted.current_step_id == "s2"

        result = await worker.run_orchestration_job(await _ctx(fake_redis, 2), **args)

        assert result["status"] == "completed"
        assert agents.calls == {"a1": 1, "a2": 2}          # s1 once; s2 re-entered
        run = await _run("run_redeliver")
        assert run.status == "completed"
        assert _history(run) == [("s1", "completed"), ("s2", "completed"), ("s3", "completed")]
        assert run.shared_state["o_s1"] == "a1 answered"

    async def test_a_run_interrupted_before_its_first_checkpoint_starts_fresh(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        """Nothing ran, so there is nothing to resume — a second try is just the run."""
        orch = S.make_orchestration(id="orch_fresh", entry_step_id="p", steps=[_print("p")])
        await seed_orchestration(**orch)

        result = await worker.run_orchestration_job(
            await _ctx(fake_redis, 2), run_id="run_fresh", orch_id="orch_fresh", initial_input="go",
        )

        assert result["status"] == "completed"
        assert _history(await _run("run_fresh")) == [("p", "completed")]

    async def test_a_run_that_already_finished_is_not_run_again(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        """Cancelled after the run completed but before the job returned."""
        orch = S.make_orchestration(id="orch_done", entry_step_id="s1", steps=[_agent("s1", "a1")])
        await seed_orchestration(**orch)
        args = dict(run_id="run_done", orch_id="orch_done", initial_input="go")

        assert (await worker.run_orchestration_job(await _ctx(fake_redis, 1), **args))["status"] == "completed"
        result = await worker.run_orchestration_job(await _ctx(fake_redis, 2), **args)

        assert result["status"] == "completed"
        assert agents.calls == {"a1": 1}

    async def test_a_redelivered_replay_runs_the_rest(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        """`resume_failed_job` sets the run `running` before its first step, so a
        second try found a run the engine refused to resume — and wrote it
        `completed` with nothing run."""
        orch = S.make_orchestration(id="orch_replay", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2", "s3"), _print("s3"),
        ])
        await seed_orchestration(**orch)
        agents.block_once.add("a2")
        await _interrupted(worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_replay", orch_id="orch_replay", initial_input="go",
        ), agents)

        result = await worker.resume_failed_job(await _ctx(fake_redis, 2), run_id="run_replay")

        assert result["status"] == "completed"
        assert agents.calls == {"a1": 1, "a2": 2}
        run = await _run("run_replay")
        assert _history(run) == [("s1", "completed"), ("s2", "completed"), ("s3", "completed")]


class TestARedeliveredAnswerIsNotAppliedTwice:
    async def test_once_the_run_has_moved_on_it_resumes_where_it_is(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        orch = S.make_orchestration(id="orch_answer", entry_step_id="h", steps=[
            _human("h", "s2"), _agent("s2", "a2", "s3"), _agent("s3", "a3", "s4"), _print("s4"),
        ])
        await seed_orchestration(**orch)
        started = await worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_answer", orch_id="orch_answer", initial_input="go",
        )
        assert started["status"] == "paused"

        agents.block_once.add("a3")
        answer = {"approved": True}
        await _interrupted(worker.resume_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_answer", human_response=answer,
        ), agents)
        assert (await _run("run_answer")).current_step_id == "s3"

        result = await worker.resume_orchestration_job(
            await _ctx(fake_redis, 2), run_id="run_answer", human_response=answer,
        )

        assert result["status"] == "completed"
        assert agents.calls == {"a2": 1, "a3": 2}
        run = await _run("run_answer")
        assert run.shared_state["o_s3"] == "a3 answered"     # not the answer
        assert [h["step_id"] for h in run.step_history] == ["h", "s2", "s3", "s4"]

    async def test_an_answer_that_never_reached_a_checkpoint_is_applied(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        orch = S.make_orchestration(id="orch_answer2", entry_step_id="h", steps=[
            _human("h", "s2"), _agent("s2", "a2", "s3"), _print("s3"),
        ])
        await seed_orchestration(**orch)
        await worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_answer2", orch_id="orch_answer2", initial_input="go",
        )
        agents.block_once.add("a2")
        answer = {"approved": True}
        await _interrupted(worker.resume_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_answer2", human_response=answer,
        ), agents)

        result = await worker.resume_orchestration_job(
            await _ctx(fake_redis, 2), run_id="run_answer2", human_response=answer,
        )

        assert result["status"] == "completed"
        run = await _run("run_answer2")
        assert run.shared_state["answer"] == answer
        assert [h["step_id"] for h in run.step_history] == ["h", "s2", "s3"]


async def _running_run(run_id: str, worker_id: str | None) -> None:
    from core.store import get_store, upsert
    from core.store.models import OrchestrationRunDB
    async with (await get_store())() as session:
        await upsert(session, OrchestrationRunDB, values={
            "run_id": run_id, "orchestration_id": "orch_x", "status": "running",
            "current_step_id": "s1", "worker_id": worker_id,
        }, index_elements=["run_id"])
        await session.commit()


async def _worker_row(worker_id: str, status: str, seconds_ago: float) -> None:
    from datetime import datetime, timedelta, timezone
    from core.store import get_store, upsert
    from core.store.models import WorkerDB
    async with (await get_store())() as session:
        await upsert(session, WorkerDB, values={
            "worker_id": worker_id, "hostname": "h", "address": "", "status": status,
            "last_heartbeat": datetime.now(timezone.utc) - timedelta(seconds=seconds_ago),
        }, index_elements=["worker_id"])
        await session.commit()


class TestTheBootSweep:
    """The API's startup sweep fails only the runs nobody is still running (F85).

    It used to fail every `running` run, in every mode. In scale mode a worker
    can outlive an API restart and still be executing its run; the sweep marked
    that run failed under it, and a Replay pressed on the failed run would then
    run it a second time.
    """

    async def test_a_run_a_live_worker_owns_is_left_running(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await _worker_row("worker-live", "online", seconds_ago=5)
        await _running_run("run_owned", "worker-live")

        swept = await sweep_interrupted_runs()

        assert swept == []
        assert (await _run("run_owned")).status == "running"

    async def test_a_run_whose_worker_went_silent_is_failed(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await _worker_row("worker-silent", "online", seconds_ago=600)
        await _running_run("run_silent", "worker-silent")

        assert await sweep_interrupted_runs() == ["run_silent"]
        assert (await _run("run_silent")).status == "failed"

    async def test_a_run_whose_worker_shut_down_is_failed(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await _worker_row("worker-gone", "offline", seconds_ago=5)
        await _running_run("run_gone", "worker-gone")

        assert await sweep_interrupted_runs() == ["run_gone"]

    async def test_a_run_the_previous_server_ran_itself_is_failed(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await _running_run("run_inproc", None)

        assert await sweep_interrupted_runs() == ["run_inproc"]
        assert (await _run("run_inproc")).status == "failed"

    async def test_a_builder_run_the_previous_server_ran_is_failed(self):
        """F230: builder runs were skipped as "structural", so one interrupted by
        a restart said `running` for ever with no pump behind it. The builder
        panel ignores such a row (F214), but nothing ever closed it."""
        from core.scale.heartbeat import sweep_interrupted_runs
        await _running_run("builder_abc123", None)

        assert await sweep_interrupted_runs() == ["builder_abc123"]
        assert (await _run("builder_abc123")).status == "failed"

    async def test_a_nested_run_is_left_to_its_parent(self):
        """A nested run (`parent__step`) is resumed through its parent, which the
        sweep fails; the child is not the sweep's to close."""
        from core.scale.heartbeat import sweep_interrupted_runs
        await _running_run("run_parent", None)
        await _running_run("run_parent__delegate", None)
        await _running_run("builder_abc__saver", None)

        assert await sweep_interrupted_runs() == ["run_parent"]
        assert (await _run("run_parent__delegate")).status == "running"

    async def test_a_replay_names_the_worker_running_it(
        self, seed_orchestration, agents, worker, fake_redis, monkeypatch,
    ):
        """`resume_failed_job` wrote `worker_id` only when it finished, so a
        replay in progress still named the worker of the attempt that failed —
        and the sweep would fail it under the worker actually running it."""
        orch = S.make_orchestration(id="orch_owner", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2"),
        ])
        await seed_orchestration(**orch)
        monkeypatch.setattr(worker, "_worker_id", "worker-a")
        agents.block_once.add("a2")
        await _interrupted(worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_owner", orch_id="orch_owner", initial_input="go",
        ), agents)

        monkeypatch.setattr(worker, "_worker_id", "worker-b")
        agents.block_once.add("a2")
        job = asyncio.create_task(worker.resume_failed_job(await _ctx(fake_redis, 2), run_id="run_owner"))
        try:
            await asyncio.wait_for(agents.started.wait(), 5)
            from core.store import get_store
            from core.store.models import OrchestrationRunDB
            async with (await get_store())() as session:
                row = await session.get(OrchestrationRunDB, "run_owner")
            assert row.worker_id == "worker-b"
            assert row.job_id == "job-2"
        finally:
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job


class TestShutdownWaitsForRunningJobs:
    def test_the_worker_waits_before_cancelling(self):
        """Without it SIGTERM cancels every job at once. It has to stay under
        the orchestrator's grace period (k8s 60 s here) or SIGKILL lands first,
        and a killed job is not handed out again for an hour."""
        from core.scale.worker import WorkerSettings
        assert 0 < WorkerSettings.job_completion_wait < 60
