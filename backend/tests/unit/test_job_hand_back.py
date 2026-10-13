"""
A stopping worker hands its runs back between steps, and tells a chat turn it cut at the cut (F424).

ARQ gives a stopping worker's jobs `job_completion_wait` (45 s) and then cancels
them; a cancelled job goes back on the queue at once (`test_job_redelivery.py`).
What the wait could not do was stop a run where nothing is lost: a run of many
steps worked through the whole wait and was cut in the middle of a step, which
the next worker ran again from its start. A chat turn cut the same way said
nothing until its next delivery — measured on the dev stack, 29 s after the cut
and 74 s after the stop (C7, `drafts/c7/f424_chats_before.json`) — and the run a
workflow agent's turn was running stayed `running` for good, holding its slot.

These drive the real job functions and engine with fakeredis and the SQLite
store; only the agent loop and the MCP pool are faked. `drain.begin()` is what
the signal handler does; one test sends a real SIGTERM to an ARQ Worker.
"""
import asyncio
import os
import signal
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
def stopping():
    from core.scale import drain
    drain.reset()
    yield drain
    drain.reset()


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


async def _ctx(fake_redis, job_try: int, job_id: str = "job-1") -> dict:
    from core.store import get_store
    return {"redis": fake_redis, "session_factory": await get_store(), "job_try": job_try, "job_id": job_id}


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


def _history(run) -> list[tuple[str, str]]:
    return [(h["step_id"], h["status"]) for h in run.step_history]


async def _started(fake_redis, run_id) -> list[str]:
    """The steps whose start went out on the run's stream."""
    entries = await fake_redis.xrange(f"synapse:run:{run_id}:events")
    import json
    out = []
    for _, fields in entries:
        raw = fields.get(b"data") or fields.get("data")
        event = json.loads(raw) if raw else {}
        if event.get("type") == "step_start" and not event.get("nested_run_id"):
            out.append(event.get("orch_step_id"))
    return out


async def _dead_letters(run_id) -> list:
    from sqlalchemy import select
    from core.scale.models_db import DeadLetterQueueDB
    from core.store import get_store
    async with (await get_store())() as session:
        return [d for d in (await session.execute(select(DeadLetterQueueDB))).scalars() if d.run_id == run_id]


class TestTheDrainHearsTheSignal:
    async def test_a_real_sigterm_marks_it_and_still_reaches_arq(self, stopping):
        """ARQ's handler lives in the loop's table; ours is the Python-level one in front."""
        from arq.worker import Worker

        async def noop(ctx):
            return None

        loop = asyncio.get_running_loop()
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        worker = Worker(functions=[noop], handle_signals=True, job_completion_wait=1, burst=True)
        try:
            assert stopping.install() is True
            assert worker.allow_pick_jobs is True and not stopping.draining()
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(50):
                if not worker.allow_pick_jobs:
                    break
                await asyncio.sleep(0.02)
            assert stopping.draining(), "our handler did not run"
            assert worker.allow_pick_jobs is False, "ARQ's handler did not run"
        finally:
            for sig, handler in before.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)
            await asyncio.sleep(1.1)  # ARQ's wait-for-completion task ends on its own

    def test_installing_twice_wraps_once(self, stopping):
        before = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: None)
            assert stopping.install()
            first = signal.getsignal(signal.SIGTERM)
            assert stopping.install()
            assert signal.getsignal(signal.SIGTERM) is first
        finally:
            signal.signal(signal.SIGTERM, before)

    def test_nothing_is_put_in_front_of_the_default(self, stopping):
        """Nothing asked this process to wind down, so SIGTERM must still end it."""
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)
            assert stopping.install() is False
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        finally:
            for sig, handler in before.items():
                signal.signal(sig, handler)


class TestARunHandsBackBetweenSteps:
    async def test_after_the_step_it_was_in_and_before_the_next(
        self, seed_orchestration, agents, worker, fake_redis, stopping,
    ):
        await seed_orchestration(**S.make_orchestration(id="orch_hb", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2", "s3"), _agent("s3", "a3")]))
        gate = agents.gate("a2")
        args = dict(run_id="run_hb", orch_id="orch_hb", initial_input="go")
        job = asyncio.create_task(worker.run_orchestration_job(await _ctx(fake_redis, 1), **args))
        await asyncio.wait_for(agents.waiting.wait(), 5)

        stopping.begin()          # the stop arrives while s2 runs: s2 finishes, s3 does not start
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 5)

        run = await _run("run_hb")
        assert run.status == "running" and run.current_step_id == "s3"
        assert _history(run) == [("s1", "completed"), ("s2", "completed")]
        assert agents.calls == {"a1": 1, "a2": 1}
        assert await _started(fake_redis, "run_hb") == ["s1", "s2"], "s3's start went out"

        stopping.reset()          # the next worker
        result = await worker.run_orchestration_job(await _ctx(fake_redis, 2), **args)

        assert result["status"] == "completed"
        assert agents.calls == {"a1": 1, "a2": 1, "a3": 1}, "a step ran twice"
        assert _history(await _run("run_hb")) == [("s1", "completed"), ("s2", "completed"), ("s3", "completed")]

    async def test_a_job_stopping_before_its_first_step_still_runs_it(
        self, seed_orchestration, agents, worker, fake_redis, stopping,
    ):
        """A hand-back with nothing done would count toward giving the run up (F376)."""
        await seed_orchestration(**S.make_orchestration(id="orch_first", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2")]))
        stopping.begin()
        with pytest.raises(asyncio.CancelledError):
            await worker.run_orchestration_job(
                await _ctx(fake_redis, 1), run_id="run_first", orch_id="orch_first", initial_input="go")

        run = await _run("run_first")
        assert run.current_step_id == "s2" and _history(run) == [("s1", "completed")]

    async def test_a_loop_body_is_not_a_boundary(self, seed_orchestration, agents, worker, fake_redis, stopping):
        """The run's position is the Loop's; it hands back after the Loop, not inside it."""
        await seed_orchestration(**S.make_orchestration(id="orch_loop", entry_step_id="loop1", steps=[
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
             "loop_step_ids": ["b1", "b2"], "next_step_id": "tail"},
            _print("b1"), _print("b2"), _agent("tail", "a_tail")]))
        stopping.begin()
        with pytest.raises(asyncio.CancelledError):
            await worker.run_orchestration_job(
                await _ctx(fake_redis, 1), run_id="run_loop", orch_id="orch_loop", initial_input="go")

        run = await _run("run_loop")
        assert run.current_step_id == "tail"
        assert [h for h in _history(run) if h[0] in ("b1", "b2")] == [("b1", "completed"), ("b2", "completed")] * 2
        assert ("loop1", "completed") in _history(run)
        assert "a_tail" not in agents.calls

    async def test_a_nested_workflows_steps_are_not_boundaries(
        self, seed_orchestration, agents, worker, fake_redis, stopping,
    ):
        """A child's events carry the parent's step id: a hand-back there would cut the child."""
        await S.seed_orchestrations([
            S.make_orchestration(id="orch_child", entry_step_id="c1", steps=[_print("c1", "c2"), _print("c2")]),
            S.make_orchestration(id="orch_parent", entry_step_id="p1", steps=[
                _agent("p1", "wf_child", "p2"), _agent("p2", "a_p2")]),
        ])
        await S.seed_agents([S.make_agent(id="wf_child", type="orchestrator", orchestration_id="orch_child")])
        stopping.begin()
        with pytest.raises(asyncio.CancelledError):
            await worker.run_orchestration_job(
                await _ctx(fake_redis, 1), run_id="run_parent", orch_id="orch_parent", initial_input="go")

        run = await _run("run_parent")
        assert run.current_step_id == "p2" and _history(run)[-1] == ("p1", "completed")
        assert "a_p2" not in agents.calls

    async def test_a_run_that_pauses_returns_paused(self, seed_orchestration, agents, worker, fake_redis, stopping):
        await seed_orchestration(**S.make_orchestration(id="orch_ask", entry_step_id="h", steps=[
            {"id": "h", "name": "H", "type": "human", "human_prompt": "ok?", "output_key": "answer",
             "next_step_id": "s2"}, _agent("s2", "a2")]))
        stopping.begin()
        result = await worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_ask", orch_id="orch_ask", initial_input="go")
        assert result["status"] == "paused"

    async def test_a_persons_cancel_wins(self, seed_orchestration, agents, worker, fake_redis, stopping):
        from core.scale.pubsub import publish_cancellation

        await seed_orchestration(**S.make_orchestration(id="orch_stop", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2")]))
        gate = agents.gate("a1")
        job = asyncio.create_task(worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_stop", orch_id="orch_stop", initial_input="go"))
        await asyncio.wait_for(agents.waiting.wait(), 5)
        await publish_cancellation(fake_redis, "run_stop")
        stopping.begin()
        gate.set()

        result = await asyncio.wait_for(job, 10)
        assert result["status"] == "cancelled"
        assert "a2" not in agents.calls

    async def test_five_stops_never_give_a_stepping_run_up(
        self, seed_orchestration, agents, worker, fake_redis, stopping,
    ):
        """Each delivery ends a step, which is progress, so F376 counts it as the first again."""
        ids = [f"s{i}" for i in range(1, 6)]
        await seed_orchestration(**S.make_orchestration(id="orch_five", entry_step_id="s1", steps=[
            _agent(sid, f"a{i}", ids[i] if i < 5 else None) for i, sid in enumerate(ids, start=1)]))
        args = dict(run_id="run_five", orch_id="orch_five", initial_input="go")
        for delivery in range(1, 5):
            stopping.begin()
            with pytest.raises(asyncio.CancelledError):
                await worker.run_orchestration_job(await _ctx(fake_redis, delivery, "job-five"), **args)
            stopping.reset()
        result = await worker.run_orchestration_job(await _ctx(fake_redis, 5, "job-five"), **args)

        assert result["status"] == "completed"
        assert agents.calls == {f"a{i}": 1 for i in range(1, 6)}
        assert await _dead_letters("run_five") == []

    async def test_without_a_stop_nothing_changes(self, seed_orchestration, agents, worker, fake_redis, stopping):
        await seed_orchestration(**S.make_orchestration(id="orch_plain", entry_step_id="s1", steps=[
            _agent("s1", "a1", "s2"), _agent("s2", "a2")]))
        result = await worker.run_orchestration_job(
            await _ctx(fake_redis, 1), run_id="run_plain", orch_id="orch_plain", initial_input="go")
        assert result["status"] == "completed" and agents.calls == {"a1": 1, "a2": 1}


class _Turn:
    """run_react_loop that says a little, then waits until it is cut."""

    def __init__(self, events=()):
        self.events = list(events)
        self.ran = 0
        self.waiting = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.ran += 1
        for event in self.events:
            yield event
        self.waiting.set()
        await asyncio.sleep(60)
        yield {"type": "final", "response": "too late"}


async def _chat_events(fake_redis, session_id) -> list[dict]:
    import json
    out = []
    for _, fields in await fake_redis.xrange(f"synapse:chat:{session_id}:events"):
        raw = fields.get(b"data") or fields.get("data")
        out.append(json.loads(raw) if raw else {})
    return out


class TestAChatTurnCutByAStop:
    async def _cut(self, worker, fake_redis, turn, monkeypatch, *, stop=True, session="s_cut"):
        import core.react_engine as re_mod
        monkeypatch.setattr(re_mod, "run_react_loop", turn)
        await S.seed_agents([S.make_agent(id="a1")])
        job = asyncio.create_task(worker.run_agent_chat_job(
            await _ctx(fake_redis, 1, f"job-{session}"), session_id=session, agent_id="a1",
            message="email the team"))
        await asyncio.wait_for(turn.waiting.wait(), 5)
        if stop:
            from core.scale import drain
            drain.begin()
        job.cancel()             # ARQ's cut, at the end of its wait
        with pytest.raises(asyncio.CancelledError):
            await job

    async def test_is_told_and_saved_at_the_cut(self, worker, fake_redis, monkeypatch, stopping):
        turn = _Turn([{"type": "tool_execution", "tool_name": "send_email", "args": {"to": "team"}},
                      {"type": "tool_result", "tool_name": "send_email", "result": "sent"}])
        await self._cut(worker, fake_redis, turn, monkeypatch)

        events = await _chat_events(fake_redis, "s_cut")
        assert [e.get("type") for e in events][-2:] == ["error", "done"]
        assert worker.INTERRUPTED_TURN == events[-2]["message"]
        history = await worker._load_chat_history((await _ctx(fake_redis, 1))["session_factory"], "s_cut", "a1")
        assert history[-2] == {"role": "user", "content": "email the team"}
        assert history[-1]["content"] == worker.INTERRUPTED_TURN
        assert [c.get("name") or c.get("tool") or c.get("tool_name") for c in history[-1].get("calls", [])] \
            == ["send_email"], "the call that ran is kept, as a failed turn keeps it (F426)"

    async def test_its_next_delivery_adds_nothing(self, worker, fake_redis, monkeypatch, stopping):
        turn = _Turn()
        await self._cut(worker, fake_redis, turn, monkeypatch)
        stopping.reset()
        before = await worker._load_chat_history((await _ctx(fake_redis, 1))["session_factory"], "s_cut", "a1")
        told = len(await _chat_events(fake_redis, "s_cut"))

        result = await worker.run_agent_chat_job(
            await _ctx(fake_redis, 2, "job-s_cut"), session_id="s_cut", agent_id="a1", message="email the team")

        assert result == {"status": "interrupted", "session_id": "s_cut", "told_at_the_cut": True}
        assert turn.ran == 1, "the turn ran again"
        after = await worker._load_chat_history((await _ctx(fake_redis, 1))["session_factory"], "s_cut", "a1")
        assert after == before
        assert len(await _chat_events(fake_redis, "s_cut")) == told

    async def test_a_cancel_that_is_not_a_stop_is_as_before(self, worker, fake_redis, monkeypatch, stopping):
        """A job's own timeout is not a stop: its next delivery says it, as F89 has it."""
        turn = _Turn()
        await self._cut(worker, fake_redis, turn, monkeypatch, stop=False, session="s_timeout")
        assert [e.get("type") for e in await _chat_events(fake_redis, "s_timeout")
                if e.get("type") in ("error", "done")] == []
        assert not await fake_redis.get("synapse:chat-told:job-s_timeout")

    async def test_the_workflow_it_was_running_stops_with_it(self, worker, fake_redis, monkeypatch, stopping):
        """A workflow agent's run is pumped on its own; cut with the turn, it was left running."""
        from core.orchestration import runner
        from core.store import get_store, upsert
        from core.store.models import OrchestrationRunDB

        async with (await get_store())() as session:
            await upsert(session, OrchestrationRunDB, values={
                "run_id": "run_turns", "orchestration_id": "orch_x", "status": "running", "current_step_id": "s2",
                "shared_state": {}, "step_history": [{"step_id": "s1", "status": "completed"}],
            }, index_elements=["run_id"])
            await session.commit()
        pump = asyncio.create_task(asyncio.sleep(60))
        runner._active_tasks["run_turns"] = pump
        try:
            turn = _Turn([{"type": "orchestration_start", "run_id": "run_turns"}])
            await self._cut(worker, fake_redis, turn, monkeypatch, session="s_flow")
        finally:
            runner._active_tasks.pop("run_turns", None)

        assert pump.cancelled(), "its pump would have written over the stop"
        run = await _run("run_turns")
        assert run.status == "failed"
        from core.orchestration.run_error import failure_reason
        assert failure_reason(run.status, run.step_history, run.step_progress) == worker.TURN_RUN_STOPPED
        assert _history(run) == [("s1", "completed")], "the step it was in has no entry"
