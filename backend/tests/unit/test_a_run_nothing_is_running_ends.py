"""A run nothing is running any more ends, and Cancel reaches a run pumped in a task of its own (F470).

A run job's run is the job's: ARQ hands the job out again after a crash, and
the job polls the cancel key. A run `runner.spawn_engine_run` pumps in a task
of its own — a workflow agent's turn, its resume, a workflow an agent hands
work to, a golden set's case — had neither. Only its own process's
`_active_tasks` knew it was alive, so a worker killed under it (SIGKILL, an OOM)
left it `running` for good, with its meter open on a host that meters runs; and
nothing read `synapse:cancel:<run_id>` for it, so Cancel answered `cancelling`
and the run went on — measured by C7 on the dev stack, a workflow agent's run
still `running` minutes after its Cancel (D270).

Now, where the host shares Redis (scale mode, cloud), a pumped run holds
`synapse:run-live:<run_id>` while its task lives, and the same beat reads the
cancel key, so the engine stops at its next step as a run job's does. A
`running` run with no live owner and no lease is one nothing is running: a
sweep fails it with the reason, and what it was running nested stops with it.
The boot sweep no longer fails a run a live worker's task still pumps.
"""
import asyncio
import types

import pytest

from _fakes import seed as S
from _fakes.wait import until


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _agent(sid, agent_id, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "agent", "agent_id": agent_id,
            "output_key": f"o_{sid}", "next_step_id": nxt, "max_turns": 3}


class _Agents:
    """run_agent_step, counting calls per agent; a gated agent waits for its gate."""

    def __init__(self):
        self.calls: dict[str, int] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.waiting = asyncio.Event()

    def gate(self, agent_id: str) -> asyncio.Event:
        self.gates[agent_id] = asyncio.Event()
        return self.gates[agent_id]

    async def __call__(self, **kwargs):
        agent_id = kwargs["agent_id"]
        self.calls[agent_id] = self.calls.get(agent_id, 0) + 1
        gate = self.gates.get(agent_id)
        if gate is not None:
            self.waiting.set()
            await gate.wait()
        yield {"type": "final", "response": f"{agent_id} answered"}


@pytest.fixture
def agents(monkeypatch):
    import core.react_engine as re_mod
    fake = _Agents()
    monkeypatch.setattr(re_mod, "run_agent_step", fake)
    return fake


@pytest.fixture
def lease(fake_redis, monkeypatch):
    """The lease as a worker installs it, polling the cancel key quickly."""
    from core.scale import run_lease
    monkeypatch.setattr(run_lease, "CANCEL_POLL_SECONDS", 0.05)
    run_lease.install(fake_redis, holder="worker-test")
    yield run_lease
    run_lease.install(None)


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _error(run):
    return ((run.step_progress or {}).get("__run__") or {}).get("error")


async def _row(run_id, status, worker_id=None):
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState
    from core.store import get_store, upsert
    from core.store.models import OrchestrationRunDB
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_or", status=status,
                                       current_step_id="s1")).checkpoint()
    if worker_id:
        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": run_id, "orchestration_id": "orch_or", "worker_id": worker_id,
            }, index_elements=["run_id"], update=["worker_id"])
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


async def _workflow_agent_turn(seed_orchestration, orch_id, run_id):
    """A workflow agent's turn, as a worker's chat job runs it: s1 (gated) then s2."""
    from core.react_engine import run_orchestrator_agent
    await seed_orchestration(**S.make_orchestration(id=orch_id, entry_step_id="s1", steps=[
        _agent("s1", "a1", "s2"), _agent("s2", "a2")]))
    agent = S.make_orchestrator_agent(orch_id, id=f"wf_{orch_id}")

    async def turn():
        return [ev async for ev in run_orchestrator_agent(agent, "go", _server(), run_id=run_id)]

    return asyncio.create_task(turn())


class TestAPumpedRunHoldsALease:
    async def test_while_its_task_lives_and_not_after(self, seed_orchestration, agents, fake_redis, lease):
        gate = agents.gate("a1")
        turn = await _workflow_agent_turn(seed_orchestration, "orch_l1", "run_l1")
        await asyncio.wait_for(agents.waiting.wait(), 5)

        assert await fake_redis.get("synapse:run-live:run_l1") in (b"worker-test", "worker-test")
        assert 0 < await fake_redis.ttl("synapse:run-live:run_l1") <= lease.LEASE_SECONDS

        gate.set()
        await asyncio.wait_for(turn, 5)
        assert (await _run("run_l1")).status == "completed"
        assert await fake_redis.get("synapse:run-live:run_l1") is None

    async def test_nothing_is_held_where_no_lease_is_installed(self, seed_orchestration, agents, fake_redis):
        """Standalone: every pumped run lives in the API process and dies with it."""
        gate = agents.gate("a1")
        turn = await _workflow_agent_turn(seed_orchestration, "orch_l2", "run_l2")
        await asyncio.wait_for(agents.waiting.wait(), 5)
        assert await fake_redis.get("synapse:run-live:run_l2") is None
        gate.set()
        await asyncio.wait_for(turn, 5)
        assert (await _run("run_l2")).status == "completed"


class TestCancelReachesAPumpedRun:
    async def test_it_stops_at_its_next_step(self, seed_orchestration, agents, fake_redis, lease):
        """What the run page's Cancel publishes, read by nothing for a pumped run until now."""
        from core.scale.pubsub import publish_cancellation
        gate = agents.gate("a1")
        turn = await _workflow_agent_turn(seed_orchestration, "orch_c1", "run_c1")
        await asyncio.wait_for(agents.waiting.wait(), 5)

        await publish_cancellation(fake_redis, "run_c1")
        from core.orchestration.state import _cancelled_run_ids
        # The lease's beat reads the key: wait for it to have, not for a time (F519's rule, BF10).
        await until(lambda: "run_c1" in _cancelled_run_ids, what="the lease's beat reading the Cancel")
        gate.set()
        await asyncio.wait_for(turn, 5)

        assert (await _run("run_c1")).status == "cancelled"
        assert "a2" not in agents.calls, "the step after the Cancel ran"


class TestTheSweepEndsARunNothingIsRunning:
    async def test_a_run_with_no_owner_and_no_lease_fails_on_the_second_look(self, fake_redis):
        from core.scale.heartbeat import RUN_ORPHANED, sweep_orphaned_runs
        await _row("run_or1", "running")
        await _row("run_or1__call_d1", "running")

        assert await sweep_orphaned_runs(fake_redis) == []          # a suspect, not yet failed
        assert (await _run("run_or1")).status == "running"
        assert await sweep_orphaned_runs(fake_redis) == ["run_or1"]

        run = await _run("run_or1")
        assert run.status == "failed" and run.ended_at
        assert _error(run) == RUN_ORPHANED
        child = await _run("run_or1__call_d1")
        assert child.status == "failed", "what it ran nested stops with it"

    async def test_a_run_whose_lease_ran_out_is_failed(self, fake_redis):
        """A worker killed under its pump: the lease outlives it by its TTL, then the run is over."""
        from core.scale.heartbeat import sweep_orphaned_runs
        await _row("run_or2", "running")
        key = "synapse:run-live:run_or2"
        await fake_redis.set(key, "worker-dead", ex=90)

        assert await sweep_orphaned_runs(fake_redis) == []
        assert await sweep_orphaned_runs(fake_redis) == []
        # Its TTL runs out: said by deleting the key, not by a 1 s TTL and a sleep, which a loaded box outran
        # before the two sweeps above had looked (BF10, F519's rule).
        await fake_redis.delete(key)
        assert await sweep_orphaned_runs(fake_redis) == []
        assert await sweep_orphaned_runs(fake_redis) == ["run_or2"]

    async def test_a_leased_run_is_left_alone(self, fake_redis):
        from core.scale.heartbeat import sweep_orphaned_runs
        await _row("run_or3", "running")
        await fake_redis.set("synapse:run-live:run_or3", "worker-live", ex=90)
        for _ in range(3):
            assert await sweep_orphaned_runs(fake_redis) == []
        assert (await _run("run_or3")).status == "running"

    async def test_a_suspect_seen_alive_again_starts_over(self, fake_redis):
        from core.scale.heartbeat import sweep_orphaned_runs
        await _row("run_or4", "running")
        assert await sweep_orphaned_runs(fake_redis) == []
        await fake_redis.set("synapse:run-live:run_or4", "worker-live", ex=90)
        assert await sweep_orphaned_runs(fake_redis) == []
        await fake_redis.delete("synapse:run-live:run_or4")
        assert await sweep_orphaned_runs(fake_redis) == []          # a suspect again, from the start
        assert (await _run("run_or4")).status == "running"

    async def test_a_run_this_process_pumps_is_left_alone(self, fake_redis, monkeypatch):
        from core.orchestration import runner
        from core.scale.heartbeat import sweep_orphaned_runs
        await _row("run_or5", "running")
        monkeypatch.setitem(runner._active_tasks, "run_or5", object())
        for _ in range(3):
            assert await sweep_orphaned_runs(fake_redis) == []

    async def test_a_run_a_live_worker_owns_is_left_alone(self, fake_redis):
        """A run job's run: its job is ARQ's to hand out again, not this sweep's to end."""
        from core.scale.heartbeat import sweep_orphaned_runs
        await _worker_row("worker-live", "online", seconds_ago=5)
        await _row("run_or6", "running", worker_id="worker-live")
        for _ in range(3):
            assert await sweep_orphaned_runs(fake_redis) == []

    async def test_a_run_whose_worker_went_silent_is_failed_as_the_boot_sweep_fails_it(self, fake_redis):
        from core.scale.heartbeat import sweep_orphaned_runs
        await _worker_row("worker-silent", "online", seconds_ago=600)
        await _row("run_or7", "running", worker_id="worker-silent")
        assert await sweep_orphaned_runs(fake_redis) == []
        assert await sweep_orphaned_runs(fake_redis) == ["run_or7"]

    async def test_a_paused_or_finished_run_is_left_alone(self, fake_redis):
        from core.scale.heartbeat import sweep_orphaned_runs
        await _row("run_or8", "paused")
        await _row("run_or9", "completed")
        for _ in range(3):
            assert await sweep_orphaned_runs(fake_redis) == []


class TestTheReaperSweepsEachRound:
    async def test_scale_modes_reaper_loop_ends_a_run_nothing_is_running(self, fake_redis):
        """Wired where scale mode already reaps silent workers, every minute (`server.py`)."""
        import contextlib
        from core.scale.heartbeat import reap_stale_workers
        from core.store import get_store
        await _row("run_rp1", "running")
        loop = asyncio.create_task(reap_stale_workers(await get_store(), interval=0.05, redis=fake_redis))
        try:
            for _ in range(100):
                await asyncio.sleep(0.05)
                if (await _run("run_rp1")).status == "failed":
                    break
        finally:
            loop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop
        assert (await _run("run_rp1")).status == "failed"


class TestTheBootSweepLeavesALeasedRun:
    async def test_a_run_a_live_workers_task_pumps_is_not_failed(self, fake_redis):
        """An API restart in scale mode failed every workflow agent's run a worker was pumping."""
        from core.scale.heartbeat import sweep_interrupted_runs
        await _row("run_bs1", "running")
        await _row("run_bs2", "running")
        await fake_redis.set("synapse:run-live:run_bs1", "worker-live", ex=90)

        assert await sweep_interrupted_runs(redis=fake_redis) == ["run_bs2"]
        assert (await _run("run_bs1")).status == "running"
        assert (await _run("run_bs2")).status == "failed"

    async def test_without_redis_it_sweeps_as_before(self):
        from core.scale.heartbeat import sweep_interrupted_runs
        await _row("run_bs3", "running")
        assert await sweep_interrupted_runs() == ["run_bs3"]
