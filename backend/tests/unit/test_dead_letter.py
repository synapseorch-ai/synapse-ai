"""
A job that dies is dead-lettered on its first try, and retrying it resumes the run (F55).

ARQ 0.28 retries a job only on `Retry` or a cancellation. An ordinary exception
fails the job for good on its first try — so the dead-letter write, which waited
for `job_try >= WORKER_MAX_RETRIES`, was reached only by a job that had also
been cancelled twice. And the retry route re-enqueued `run_orchestration_job`
with the run id, orchestration id and input alone, which restarted the run from
its entry step over its own checkpoint.

What reaches the dead-letter queue is a job that died *outside* the engine — a
missing orchestration, the store or Redis failing (D105). A run that fails at a
step is not a dead job: the engine records it `failed`, the job ends normally,
and a person retries it with Replay. For Replay to have anything to resume, a
failed fleet run keeps its position.
"""
import types
import uuid

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


class _Agents:
    """run_agent_step, counting calls; named agents raise on their first call."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.fail_once: set[str] = set()

    async def __call__(self, **kwargs):
        agent_id = kwargs["agent_id"]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        if agent_id in self.fail_once:
            self.fail_once.discard(agent_id)
            raise RuntimeError(f"{agent_id} provider down")
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


async def _ctx(fake_redis, job_try: int = 1) -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(),
            "job_try": job_try, "job_id": f"job-{job_try}"}


async def _dead_letters() -> list:
    from sqlalchemy import select
    from core.scale.models_db import DeadLetterQueueDB
    from core.store import get_store
    async with (await get_store())() as session:
        return list((await session.execute(select(DeadLetterQueueDB))).scalars())


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


class _StoreProvider:
    async def resolve_agent(self, agent_id):
        from core.store import resources
        return await resources.get_agent(agent_id)

    async def resolve_orchestration(self, orch_id):
        from core.store import resources
        return await resources.get_orchestration(orch_id)

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def tenants():
    from core.scale import context as scale_context
    scale_context.set_resource_provider(_StoreProvider())
    yield
    scale_context.set_resource_provider(None)


class TestAJobThatDiesIsDeadLettered:
    async def test_on_its_first_try_under_its_own_tenant(self, worker, fake_redis, tenants):
        """Cloud's table refuses a row whose tenant is not the org's; the write
        never set one, and its failure was swallowed."""
        from core.tenancy import tenant_scope

        with tenant_scope("acme"):
            with pytest.raises(ValueError):
                await worker.run_orchestration_job(
                    await _ctx(fake_redis), run_id="run_dead", orch_id="orch_gone", initial_input="secret input",
                )

        [row] = await _dead_letters()
        assert row.run_id == "run_dead"
        assert row.tenant_id == "acme"
        assert row.job_function == "run_orchestration_job"
        assert row.attempt_count == 1
        assert "orch_gone" in row.error_message
        # Retry needs the run, not its input: the input stays in the run row.
        assert row.job_payload == {"run_id": "run_dead", "orch_id": "orch_gone"}
        with tenant_scope("acme"):
            assert (await _run("run_dead")).status == "failed"

    async def test_a_replay_that_dies_is_dead_lettered(self, seed_orchestration, agents, worker, fake_redis):
        orch = S.make_orchestration(id="orch_replay_dies", entry_step_id="s1", steps=[_agent("s1", "a1")])
        await seed_orchestration(**orch)
        agents.fail_once.add("a1")
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_replay_dies", orch_id="orch_replay_dies", initial_input="go",
        )
        from core.store import resources
        await resources.delete_orchestration("orch_replay_dies")

        with pytest.raises(Exception):
            await worker.resume_failed_job(await _ctx(fake_redis), run_id="run_replay_dies")

        [row] = await _dead_letters()
        assert row.job_function == "resume_failed_job"
        assert row.run_id == "run_replay_dies"


class TestARunThatFailsAtAStep:
    async def test_is_not_dead_lettered(self, seed_orchestration, agents, worker, fake_redis):
        orch = S.make_orchestration(id="orch_step_fails", entry_step_id="s1", steps=[_agent("s1", "a1")])
        await seed_orchestration(**orch)
        agents.fail_once.add("a1")

        result = await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_step_fails", orch_id="orch_step_fails", initial_input="go",
        )

        assert result["status"] == "failed"
        assert await _dead_letters() == []

    async def test_keeps_its_position_so_replay_reruns_the_failed_step(
        self, seed_orchestration, agents, worker, fake_redis,
    ):
        orch = S.make_orchestration(id="orch_replayable", entry_step_id="s1", steps=[
            _print("s1", "s2"), _agent("s2", "a2", "s3"), _print("s3"),
        ])
        await seed_orchestration(**orch)
        agents.fail_once.add("a2")
        await worker.run_orchestration_job(
            await _ctx(fake_redis), run_id="run_replayable", orch_id="orch_replayable", initial_input="go",
        )
        failed = await _run("run_replayable")
        assert failed.status == "failed"
        assert failed.current_step_id == "s2"

        result = await worker.resume_failed_job(await _ctx(fake_redis), run_id="run_replayable")

        assert result["status"] == "completed"
        assert agents.calls == {"a2": 2}
        run = await _run("run_replayable")
        assert [(h["step_id"], h["status"]) for h in run.step_history] == [
            ("s1", "completed"), ("s2", "completed"), ("s3", "completed"),
        ]


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
async def dlq_app(scale_app):
    from core.store import get_store
    scale_app.state.pg_session_factory = await get_store()
    return scale_app


async def _dead_letter(run_id: str, tenant_id: str = "default") -> str:
    from core.scale.models_db import DeadLetterQueueDB
    from core.store import get_store
    row_id = uuid.uuid4()
    async with (await get_store())() as session:
        session.add(DeadLetterQueueDB(
            id=row_id, tenant_id=tenant_id, run_id=run_id, orchestration_id="orch_x",
            job_function="run_orchestration_job", job_payload={"run_id": run_id, "orch_id": "orch_x"},
            error_message="boom", attempt_count=1,
        ))
        await session.commit()
    return str(row_id)


async def _failed_run(run_id: str, current_step_id: str | None) -> None:
    from core.store import get_store, upsert
    from core.store.models import OrchestrationRunDB
    async with (await get_store())() as session:
        await upsert(session, OrchestrationRunDB, values={
            "run_id": run_id, "orchestration_id": "orch_x", "status": "failed",
            "current_step_id": current_step_id,
        }, index_elements=["run_id"])
        await session.commit()


async def _resolved(row_id: str) -> bool:
    return next(r for r in await _dead_letters() if str(r.id) == row_id).resolved


class TestRetryingADeadLetter:
    async def test_resumes_the_run_instead_of_starting_it_again(self, client, dlq_app):
        await _failed_run("run_retry", current_step_id="s2")
        row_id = await _dead_letter("run_retry")

        resp = await client.post(f"/api/scale/dlq/{row_id}/retry")

        assert resp.status_code == 200, resp.text
        args, kwargs = dlq_app.state.arq_redis.enqueue_job.call_args
        assert args[0] == "resume_failed_job"
        assert kwargs["run_id"] == "run_retry"
        assert kwargs["_job_id"].startswith("retry_run_retry_")
        assert "initial_input" not in kwargs
        assert await _resolved(row_id) is True

    async def test_a_run_that_never_reached_a_step_is_refused(self, client, dlq_app):
        """Nothing to resume, and restarting from a dead letter is what F55 removed."""
        await _failed_run("run_never", current_step_id=None)
        row_id = await _dead_letter("run_never")

        resp = await client.post(f"/api/scale/dlq/{row_id}/retry")

        assert resp.status_code == 409
        assert "start it again" in resp.json()["detail"]
        dlq_app.state.arq_redis.enqueue_job.assert_not_awaited()
        assert await _resolved(row_id) is False

    async def test_stays_open_when_nothing_was_enqueued(self, client, dlq_app):
        """arq answers None for a job id it already holds."""
        await _failed_run("run_dup", current_step_id="s1")
        row_id = await _dead_letter("run_dup")
        dlq_app.state.arq_redis.enqueue_job.return_value = None

        resp = await client.post(f"/api/scale/dlq/{row_id}/retry")

        assert resp.status_code == 409
        assert await _resolved(row_id) is False

    async def test_another_tenants_dead_letter_is_not_listed_or_retried(self, client, dlq_app):
        await _failed_run("run_theirs", current_step_id="s1")
        theirs = await _dead_letter("run_theirs", tenant_id="globex")
        ours = await _dead_letter("run_ours")

        listed = (await client.get("/api/scale/dlq")).json()

        assert [r["id"] for r in listed] == [ours]
        assert (await client.post(f"/api/scale/dlq/{theirs}/retry")).status_code == 404
