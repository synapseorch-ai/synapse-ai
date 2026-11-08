"""A host's ceiling on how long a run works, and every step capped by it (PL1a).

Cloud's plans each allow a run a length — 30 minutes on Free, 6 hours on
Business — and nothing enforced it: the engine's budget was the orchestration's
own `timeout_minutes`, which its author sets. `time_limits.set_run_ceiling`
lets a host name a tighter one per run; the self-hosted product names none.

And what is left of the run's budget now caps every step. It used to cap only a
step that runs a workflow (F291), so a plain step could outlast its run by its
own `timeout_seconds`, which has no ceiling.

The clock is faked where work is counted (a Print step is 40 s of it); the cap
on a step is measured on the real clock, against a real `anyio.sleep`.
"""
import time
import types

import anyio
import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture(autouse=True)
def no_ceiling():
    from core.orchestration import time_limits

    yield
    time_limits.set_run_ceiling(None)


def ceiling(seconds: float, name: str = "the 1-minute run limit of this workspace's plan"):
    """Register a host ceiling for every run but a nested one."""
    from core.orchestration import time_limits

    asked: list[str] = []

    async def hook(run_id: str):
        asked.append(run_id)
        return None if "__" in run_id else time_limits.RunCeiling(seconds=seconds, name=name)

    time_limits.set_run_ceiling(hook)
    return asked


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


def _work(orch_id: str, timeout_minutes: int = 10):
    return S.make_orchestration(id=orch_id, entry_step_id="w1", timeout_minutes=timeout_minutes, steps=[
        {"id": "w1", "name": "W1", "type": "print", "print_content": "x", "output_key": "a", "next_step_id": "w2"},
        {"id": "w2", "name": "W2", "type": "print", "print_content": "x", "output_key": "b", "next_step_id": "w3"},
        {"id": "w3", "name": "W3", "type": "print", "print_content": "x", "output_key": "c", "next_step_id": None},
    ])


async def _run(orch, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("go", run_id=run_id)]


def _ending(events):
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


def _error(events):
    return next(e for e in events if e.get("type") == "orchestration_error")["error"]


async def _row(run_id):
    from core.orchestration.state import SharedState

    return (await SharedState.restore(run_id)).run


class TestTheHostsCeiling:
    async def test_below_the_runs_own_budget_it_stops_the_run_in_the_hosts_words(self, clock):
        asked = ceiling(60)
        events = await _run(_work("orch_pl1a_c"), "run_pl1a_c")

        assert asked == ["run_pl1a_c"]
        assert _ending(events) == "failed"
        # 40 s, then 80 s > 60 s: the third step never ran.
        assert "c" not in (await _row("run_pl1a_c")).shared_state
        error = _error(events)
        assert error.startswith("Stopped at the 1-minute run limit of this workspace's plan, after 1.3 min")
        assert "waiting for a person is not counted" in error
        # Kept on the run (F257): its page and v1 say it.
        from core.orchestration.run_error import run_error

        assert run_error(await _row("run_pl1a_c")) == error

    async def test_above_the_runs_own_budget_the_own_budget_holds(self, clock):
        ceiling(3600)
        events = await _run(_work("orch_pl1a_o", timeout_minutes=1), "run_pl1a_o")

        assert _ending(events) == "failed"
        assert _error(events).startswith("Global timeout (1 min) exceeded after 1.3 min")

    async def test_with_none_the_run_works_to_its_own_budget(self, clock):
        events = await _run(_work("orch_pl1a_n"), "run_pl1a_n")
        assert _ending(events) == "completed"

    async def test_a_hook_that_fails_sets_none(self, clock):
        from core.orchestration import time_limits

        async def broken(run_id):
            raise RuntimeError("the plan store is down")

        time_limits.set_run_ceiling(broken)
        assert _ending(await _run(_work("orch_pl1a_b"), "run_pl1a_b")) == "completed"


def _slow(orch_id: str, step_timeout: int = 600):
    return S.make_orchestration(id=orch_id, entry_step_id="slow", timeout_minutes=10, steps=[
        {"id": "slow", "name": "Slow", "type": "print", "print_content": "x", "output_key": "a",
         "timeout_seconds": step_timeout, "next_step_id": None},
    ])


@pytest.fixture
def slow_step(monkeypatch):
    """A Print step that takes five seconds of real time."""
    from core.models_orchestration import StepType
    from core.orchestration import steps as steps_mod

    class Slow:
        async def execute(self, step, run, engine):
            await anyio.sleep(5)
            return
            yield

    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, Slow())


class TestWhatIsLeftCapsEveryStep:
    async def test_a_plain_step_is_cut_at_what_is_left_and_its_time_is_kept(self, slow_step):
        """Its own timeout is ten minutes; the run has half a second left."""
        ceiling(0.5, name="the test's half-second limit")
        started = time.monotonic()

        events = await _run(_slow("orch_pl1a_s"), "run_pl1a_s")

        assert time.monotonic() - started < 4
        assert _ending(events) == "failed"
        step_error = next(e for e in events if e.get("type") == "step_error")
        assert "(what was left of the test's half-second limit)" in step_error["error"]
        [entry] = (await _row("run_pl1a_s")).step_history
        assert entry["status"] == "failed"
        # Its work is counted: the entry carries its times like any other ending.
        assert entry["duration_seconds"] == pytest.approx(1.0, abs=0.6)
        assert entry["started_at"] and entry["ended_at"]

    async def test_without_a_ceiling_the_runs_own_budget_caps_it_too(self, slow_step, monkeypatch):
        """No host: the step's ten minutes are still capped by its run's budget."""
        from core.orchestration import engine as engine_mod

        # A run whose own budget has all but run out when the step starts.
        monkeypatch.setattr(engine_mod.OrchestrationEngine, "_active_seconds",
                            staticmethod(lambda run: 10 * 60 - 0.5))
        events = await _run(_slow("orch_pl1a_o2"), "run_pl1a_o2")

        step_error = next(e for e in events if e.get("type") == "step_error")
        assert "(what was left of this run's own timeout)" in step_error["error"]
