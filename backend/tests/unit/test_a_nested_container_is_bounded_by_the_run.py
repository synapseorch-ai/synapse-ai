"""A Loop or Parallel inside a Loop or a Parallel branch is bounded by the run (F411).

At the top level a container gets what is left of the run: its budget has to
cover every step it runs inside itself, and each of those has its own limit.
Inside a container it got one step's — `timeout_seconds or ORCH_STEP_TIMEOUT`,
300 s — for all of its iterations. Measured on the fleet (C2, `c2len`, Free's
30-minute length): Outer ×3 → Middle ×10 → Inner ×45 → one nova-micro call
failed after 301 s, "Step 'Middle' timed out after 300s (iteration 1)", nowhere
near the plan's 1,800 s.

Now a body step that is itself a Loop or a Parallel has no limit of its own: the
container around it holds it, and the one at the top holds what is left of the
run, so the run's budget bounds it all the way down. Its own body steps keep
their own limits. A Timeout set on a nested container by hand is not read, as
it is not at the top level.
"""
import time
import types

import anyio as _anyio
import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class _Limits:
    """`anyio` as a module sees it, noting every `fail_after` it hands out."""

    def __init__(self):
        self.delays: list = []

    def fail_after(self, delay, *a, **kw):
        self.delays.append(delay)
        return _anyio.fail_after(delay, *a, **kw)

    def __getattr__(self, name):
        return getattr(_anyio, name)


@pytest.fixture
def limits(monkeypatch):
    from core.orchestration import engine as engine_mod, steps as steps_mod
    top, body = _Limits(), _Limits()
    monkeypatch.setattr(engine_mod, "anyio", top)
    monkeypatch.setattr(steps_mod, "anyio", body)
    return types.SimpleNamespace(top=top, body=body)


@pytest.fixture(autouse=True)
def no_ceiling():
    from core.orchestration import time_limits
    yield
    time_limits.set_run_ceiling(None)


def _print(sid, timeout=300):
    return {"id": sid, "name": sid.title(), "type": "print", "print_content": "x",
            "output_key": f"o_{sid}", "timeout_seconds": timeout, "next_step_id": None}


def _loop(sid, body, count=1, timeout=300, nxt=None):
    return {"id": sid, "name": sid.title(), "type": "loop", "loop_count": count, "loop_step_ids": body,
            "timeout_seconds": timeout, "next_step_id": nxt}


def _fan(sid, branches, timeout=300):
    return {"id": sid, "name": sid.title(), "type": "parallel", "parallel_branches": branches,
            "timeout_seconds": timeout, "next_step_id": None}


async def _run(steps, entry, run_id, minutes=60):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id=entry, steps=steps, timeout_minutes=minutes)
    await S.seed_orchestrations([orch])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server()).run(
        "go", run_id=run_id)]
    return events, next(e for e in reversed(events) if e.get("type") == "orchestration_complete")


class TestTheLimitANestedContainerIsGiven:
    async def test_a_loop_in_a_loop_has_none_of_its_own(self, limits):
        _, done = await _run([_loop("outer", ["middle"]), _loop("middle", ["inner"], count=2),
                              _print("inner", timeout=45)], "outer", "run_f411_ll")
        assert done["status"] == "completed"
        # The top-level Loop: what is left of the run's hour.
        assert 3600 - 5 < limits.top.delays[0] <= 3600, limits.top.delays
        # Middle: none of its own. Inner, twice: its own 45 s.
        assert limits.body.delays == [None, 45, 45], limits.body.delays

    async def test_a_parallel_in_a_loop_has_none_of_its_own(self, limits):
        _, done = await _run([_loop("outer", ["fan"]), _fan("fan", [["a"], ["b"]]),
                              _print("a", timeout=30), _print("b", timeout=40)], "outer", "run_f411_lp")
        assert done["status"] == "completed"
        assert limits.body.delays[0] is None, limits.body.delays
        assert sorted(d for d in limits.body.delays[1:]) == [30, 40]

    async def test_a_loop_in_a_parallel_branch_has_none_of_its_own(self, limits):
        _, done = await _run([_fan("fan", [["lp"], ["b"]]), _loop("lp", ["a"], count=2),
                              _print("a", timeout=30), _print("b", timeout=40)], "fan", "run_f411_pl")
        assert done["status"] == "completed"
        assert None in limits.body.delays, limits.body.delays
        assert sorted(d for d in limits.body.delays if d is not None) == [30, 30, 40]

    async def test_a_plain_body_step_keeps_its_own(self, limits):
        await _run([_loop("outer", ["inner"], count=2), _print("inner", timeout=45)], "outer", "run_f411_p")
        assert limits.body.delays == [45, 45]


@pytest.fixture
def half_second_prints(monkeypatch):
    """A Print step that takes half a second of real time."""
    from core.models_orchestration import StepType
    from core.orchestration import steps as steps_mod

    class Slow:
        async def execute(self, step, run, engine):
            await _anyio.sleep(0.5)
            run.shared_state[step.output_key] = step.id
            return
            yield

    monkeypatch.setitem(steps_mod.STEP_EXECUTORS, StepType.PRINT, Slow())


class TestOnTheClock:
    async def test_a_nested_loop_outlasts_one_steps_limit(self, half_second_prints):
        """Three half-second iterations in a Loop whose own Timeout says 1 s: it
        used to fail at 1 s ("Step 'Middle' timed out after 1s (iteration 1)")."""
        events, done = await _run([_loop("outer", ["middle"]), _loop("middle", ["inner"], count=3, timeout=1),
                                   _print("inner")], "outer", "run_f411_clock")
        assert done["status"] == "completed", [e for e in events if e.get("type") == "step_error"]

    async def test_the_run_still_bounds_it(self, half_second_prints):
        """No limit of its own is not no limit: the run's half second holds it."""
        from core.orchestration import time_limits

        async def hook(run_id):
            return time_limits.RunCeiling(seconds=1.0, name="the test's one-second limit")

        time_limits.set_run_ceiling(hook)
        started = time.monotonic()
        _, done = await _run([_loop("outer", ["middle"]), _loop("middle", ["inner"], count=20),
                              _print("inner")], "outer", "run_f411_bound")
        assert done["status"] == "failed"
        assert time.monotonic() - started < 4, "cut at the run's limit, not after twenty iterations"
