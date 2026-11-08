"""`timeout_minutes` counts the time a run worked, across its pauses (F258).

The budget was measured from the start of each `_execute_loop` call, and a
resume is a new call — so a run with a person's pause in it started its budget
again at every answer. Measured: `timeout_minutes: 1`, 40 s of work, a pause,
40 s of work → completed, 80 s of work under a 60 s budget. The budget is a
guardrail against runaway cost, and a pause silently reset it.

Now the run keeps its active seconds in `step_progress["__run__"]` (mirrored by
the OSS store and cloud's engine table alike: no column, no migration). Time
waiting for a person is not counted. A person's Replay of a failed run starts a
fresh budget; a fleet redelivery (`interrupted`) does not.

The clock is faked: each "work" step moves it 40 s.
"""
import time
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def clock(monkeypatch):
    """`time.time` under the test's control; a Print step is 40 s of work."""
    from core.models_orchestration import StepType
    from core.orchestration import steps as steps_mod

    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])

    class Work:
        async def execute(self, step, run, engine):
            now[0] += 40
            run.shared_state[step.output_key] = step.id
            return
            yield  # an async generator, like every executor

    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, Work())
    return now


def _orch(orch_id="orch_f258"):
    return S.make_orchestration(id=orch_id, entry_step_id="w1", timeout_minutes=1, steps=[
        {"id": "w1", "name": "W1", "type": "print", "print_content": "x", "output_key": "a", "next_step_id": "ask"},
        {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?", "output_key": "h", "next_step_id": "w2"},
        {"id": "w2", "name": "W2", "type": "print", "print_content": "x", "output_key": "b", "next_step_id": "w3"},
        {"id": "w3", "name": "W3", "type": "print", "print_content": "x", "output_key": "c", "next_step_id": None},
    ])


def _status(events):
    if events and events[-1].get("type") == "human_input_required":
        return "paused"
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


async def _start(orch, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("go", run_id=run_id)]


async def _answer(run_id):
    from core.orchestration.engine import OrchestrationEngine
    return [e async for e in OrchestrationEngine.resume(run_id, "yes", _server())]


async def _run(run_id):
    from core.orchestration.state import SharedState
    return (await SharedState.restore(run_id)).run


class TestAPauseDoesNotResetTheBudget:
    async def test_work_on_both_sides_of_a_pause_adds_up(self, clock):
        assert _status(await _start(_orch(), "run_f258")) == "paused"
        # 40 s of work, then the person took an hour: only the work counts.
        assert (await _run("run_f258")).step_progress["__run__"]["active_seconds"] == pytest.approx(40, abs=1)
        clock[0] += 3600

        events = await _answer("run_f258")

        assert _status(events) == "failed"
        run = await _run("run_f258")
        assert "c" not in run.shared_state                  # W3 never ran: 80 s > 60 s
        error = next(e for e in events if e.get("type") == "orchestration_error")["error"]
        assert "1 min" in error and "waiting for a person" in error

    async def test_under_the_budget_the_run_completes(self, clock):
        orch = _orch("orch_f258_ok")
        orch["timeout_minutes"] = 2
        assert _status(await _start(orch, "run_f258_ok")) == "paused"
        clock[0] += 3600
        assert _status(await _answer("run_f258_ok")) == "completed"
        # A completed run leaves no progress behind, the count included.
        assert (await _run("run_f258_ok")).step_progress == {}


class TestAReplayOfAFailedRun:
    async def test_a_persons_replay_starts_a_fresh_budget(self, clock):
        from core.orchestration.engine import OrchestrationEngine

        await _start(_orch("orch_f258_r"), "run_f258_r")
        assert _status(await _answer("run_f258_r")) == "failed"

        events = [e async for e in OrchestrationEngine.resume_failed("run_f258_r", _server())]

        assert _status(events) == "completed"
        assert (await _run("run_f258_r")).shared_state["c"] == "w3"

    async def test_a_redelivered_job_keeps_counting(self, clock):
        from core.orchestration.engine import OrchestrationEngine

        await _start(_orch("orch_f258_i"), "run_f258_i")
        assert _status(await _answer("run_f258_i")) == "failed"

        events = [e async for e in OrchestrationEngine.resume_failed("run_f258_i", _server(), interrupted=True)]

        assert _status(events) == "failed"
        assert "c" not in (await _run("run_f258_i")).shared_state


def test_the_count_is_not_in_the_public_run_view():
    from core.models_orchestration import OrchestrationRun
    from core.routes.orchestrations import _public_run_view

    run = OrchestrationRun(run_id="r", orchestration_id="o", step_progress={"__run__": {"active_seconds": 12}})
    assert "step_progress" not in _public_run_view(run)
