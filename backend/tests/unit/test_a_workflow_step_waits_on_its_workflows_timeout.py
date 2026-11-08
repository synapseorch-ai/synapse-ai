"""
A step that runs a workflow waits on that workflow's own timeout (F291).

Every step was given `timeout_seconds or ORCH_STEP_TIMEOUT` (300 s); only Loop
and Parallel got the run's remaining budget. An Agent step whose agent is a
workflow agent runs a whole child orchestration inside that one step, so a child
doing more than five minutes of work failed its parent step ("timed out after
300s") while its own `timeout_minutes` (default 30) said it could go on.

Naveen's call (2026-09-28): the child's budget wins. `timeout_seconds` defaults
to 300, and every save path writes it — the editor's new step, the builder, the
API's dump — so "unset" cannot be told from 300. On a workflow step, 300 is read
as unset: the step waits on the child's `timeout_minutes`. Any other value caps
it, and so does what is left of the parent run's own timeout.

Measured through the limit each step's `fail_after` is handed; one test runs a
real 1 s cap to its end.
"""
import types

import anyio as _anyio
import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


class _Limits:
    """`anyio` as a module sees it, noting every `fail_after` it hands out."""

    def __init__(self):
        self.delays: list[float] = []

    def fail_after(self, delay, *a, **kw):
        self.delays.append(delay)
        return _anyio.fail_after(delay, *a, **kw)

    def __getattr__(self, name):
        return getattr(_anyio, name)


@pytest.fixture
def limits(monkeypatch):
    """The limits the engine (top-level steps) and the Loop / Parallel bodies use."""
    from core.orchestration import engine as engine_mod, steps as steps_mod
    top, body = _Limits(), _Limits()
    monkeypatch.setattr(engine_mod, "anyio", top)
    monkeypatch.setattr(steps_mod, "anyio", body)
    return types.SimpleNamespace(top=top, body=body)


async def _seed(child_minutes=45, child_steps=None):
    child = S.make_orchestration(
        id="orch_child", name="Research", entry_step_id="say", timeout_minutes=child_minutes,
        steps=child_steps or [{"id": "say", "name": "Say", "type": "print", "print_content": "found it",
                               "output_key": "found", "next_step_id": None}])
    await S.seed_orchestrations([child])
    await S.seed_agents([S.make_orchestrator_agent("orch_child", id="wf_agent"),
                         S.make_agent(id="plain_agent")])


def _nested(sid="call", timeout=300, nxt=None):
    return {"id": sid, "name": "Call research", "type": "agent", "agent_id": "wf_agent",
            "prompt_template": "go", "output_key": f"o_{sid}", "timeout_seconds": timeout,
            "next_step_id": nxt}


async def _run(steps, entry, minutes=60, run_id="run_f291"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = S.make_orchestration(id="orch_parent", entry_step_id=entry, steps=steps, timeout_minutes=minutes)
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), _server()).run(
        "go", run_id=run_id)]
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")


class TestAWorkflowStepAtTheDefault:
    async def test_waits_on_the_childs_timeout(self, limits):
        await _seed(child_minutes=45)
        done = await _run([_nested()], "call", minutes=60)

        assert done["status"] == "completed"
        assert done["final_state"]["o_call"] == "found it"
        assert limits.top.delays[0] == 45 * 60, limits.top.delays

    async def test_the_parents_own_budget_caps_it(self, limits):
        await _seed(child_minutes=45)
        await _run([_nested()], "call", minutes=4)

        assert 4 * 60 - 5 < limits.top.delays[0] <= 4 * 60, limits.top.delays

    async def test_inside_a_loop_body(self, limits):
        await _seed(child_minutes=45)
        loop = {"id": "lp", "name": "Loop", "type": "loop", "loop_count": 1, "loop_step_ids": ["call"],
                "output_key": "lo", "next_step_id": None}
        done = await _run([loop, _nested()], "lp", minutes=60)

        assert done["status"] == "completed"
        assert limits.body.delays[0] == 45 * 60, limits.body.delays

    async def test_inside_a_parallel_branch(self, limits):
        await _seed(child_minutes=45)
        fan = {"id": "fan", "name": "Fan", "type": "parallel", "parallel_branches": [["call"]],
               "next_step_id": None}
        done = await _run([fan, _nested()], "fan", minutes=60)

        assert done["status"] == "completed"
        assert limits.body.delays[0] == 45 * 60, limits.body.delays


class TestAnExplicitStepTimeout:
    async def test_still_caps_a_workflow_step(self, limits):
        await _seed(child_minutes=45)
        await _run([_nested(timeout=120)], "call", minutes=60)

        assert limits.top.delays[0] == 120

    async def test_a_real_one_second_cap_still_ends_the_step(self):
        """The child's step runs 3 s; the parent step's own 1 s cap fails it."""
        await _seed(child_minutes=45, child_steps=[{
            "id": "say", "name": "Slow", "type": "transform", "output_key": "found",
            "transform_code": "import time\ntime.sleep(3)\nresult = 1", "next_step_id": None}])
        done = await _run([_nested(timeout=1)], "call", minutes=60)

        assert done["status"] == "failed"


class TestEveryOtherStep:
    async def test_keeps_its_own_timeout(self, limits):
        await _seed()
        await S.seed_agents([S.make_orchestrator_agent("orch_child", id="wf_agent"),
                             S.make_agent(id="plain_agent")])
        step = {"id": "plain", "name": "Plain", "type": "print", "print_content": "x", "output_key": "p",
                "timeout_seconds": 300, "next_step_id": None}
        await _run([step], "plain", minutes=60)

        assert limits.top.delays[0] == 300
