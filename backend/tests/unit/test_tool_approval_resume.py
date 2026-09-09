"""
An orchestration that stops to ask about a tool call, and picks up where it
stopped.

The human step pauses at a graph node; this pauses *inside* an agent step, so
resume cannot do what it does for a human step and move on — the step's work
is unfinished and the transcript that got it that far has to come back with it.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

AGENT = "risky_agent"


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _orch():
    return S.make_orchestration(
        id="orch_approval", entry_step_id="work",
        steps=[
            {"id": "work", "name": "Do the work", "type": "agent", "agent_id": AGENT,
             "prompt_template": "go", "output_key": "result", "max_turns": 5,
             "next_step_id": "after"},
            {"id": "after", "name": "After", "type": "print", "print_content": "done",
             "output_key": "tail", "next_step_id": None},
        ],
    )


@pytest.fixture
async def seeded():
    orch = _orch()
    await S.seed_orchestrations([orch])
    await S.seed_agents([S.make_agent(
        id=AGENT, tools=["all"], skip_default_tools=True,
        tool_policy={"undeclared": "confirm"},
    )])
    return orch


async def _run(orch, run_id, fake_llm, script, executed):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    import core.react_engine as re_mod

    schema = {"type": "function", "function": {"name": "danger", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}
    real = re_mod.run_agent_step

    async def with_tools(**kwargs):
        async def executor(name, args):
            executed.append((name, args))
            return f"ran-{name}"
        kwargs.setdefault("tools_override", [schema])
        kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    fake_llm.script(script)
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    import unittest.mock as _m
    with _m.patch.object(re_mod, "run_agent_step", with_tools):
        with _m.patch("core.orchestration.steps.run_agent_step", with_tools, create=True):
            return [ev async for ev in engine.run("go", run_id=run_id)]


async def _resume(run_id, answer, fake_llm, script, executed):
    from core.orchestration.engine import OrchestrationEngine
    import core.react_engine as re_mod
    import unittest.mock as _m

    schema = {"type": "function", "function": {"name": "danger", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}
    real = re_mod.run_agent_step

    async def with_tools(**kwargs):
        async def executor(name, args):
            executed.append((name, args))
            return f"ran-{name}"
        kwargs.setdefault("tools_override", [schema])
        kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    fake_llm.script(script)
    with _m.patch.object(re_mod, "run_agent_step", with_tools):
        with _m.patch("core.orchestration.steps.run_agent_step", with_tools, create=True):
            return [ev async for ev in OrchestrationEngine.resume(run_id, answer, _server())]


class TestTheRunStopsAndAsks:
    async def test_it_pauses_with_the_call_named(self, seeded, fake_llm):
        executed = []
        events = await _run(seeded, "run_ap1", fake_llm, [tool_call("danger"), "done"], executed)

        pause = next(e for e in events if e["type"] == "human_input_required")
        assert pause["kind"] == "tool_approval"
        assert pause["tool_name"] == "danger"
        assert pause["orch_step_id"] == "work"
        assert executed == []                     # nothing ran

    async def test_the_pending_call_is_on_the_run(self, seeded, fake_llm):
        from core.orchestration.state import SharedState
        await _run(seeded, "run_ap2", fake_llm, [tool_call("danger"), "done"], [])
        run = (await SharedState.restore("run_ap2")).run
        assert run.status == "paused" and run.waiting_for_human
        assert run.current_step_id == "work"      # not advanced
        pending = run.step_progress["work"]["pending_approval"]
        assert pending["tool_name"] == "danger" and pending["approval_id"]


class TestApproving:
    async def test_the_call_runs_and_the_run_finishes(self, seeded, fake_llm):
        from core.orchestration.state import SharedState
        executed = []
        await _run(seeded, "run_ap3", fake_llm, [tool_call("danger"), "done"], executed)
        assert executed == []

        events = await _resume("run_ap3", {"approve": True}, fake_llm, ["all finished"], executed)

        assert executed == [("danger", {})]       # exactly once
        resolved = next(e for e in events if e["type"] == "tool_approval_resolved")
        assert resolved["approved"] is True and resolved["tool_name"] == "danger"
        assert any(e["type"] == "orchestration_complete" for e in events)

        run = (await SharedState.restore("run_ap3")).run
        assert run.status == "completed"
        assert run.shared_state["result"] == "all finished"
        assert run.shared_state["tail"] == "done"       # the step after it ran too
        assert "pending_approval" not in (run.step_progress.get("work") or {})

    async def test_the_step_is_re_entered_not_stepped_past(self, seeded, fake_llm):
        """A human step resumes by moving on. This one has unfinished work."""
        executed = []
        await _run(seeded, "run_ap4", fake_llm, [tool_call("danger"), "done"], executed)
        events = await _resume("run_ap4", {"approve": True}, fake_llm, ["finished"], executed)
        started = [e["orch_step_id"] for e in events if e.get("type") == "step_start"]
        assert "work" in started, "the agent step was skipped instead of continued"


class TestRefusing:
    async def test_the_model_is_told_and_the_run_still_completes(self, seeded, fake_llm):
        from core.orchestration.state import SharedState
        executed = []
        await _run(seeded, "run_ap5", fake_llm, [tool_call("danger"), "done"], executed)

        events = await _resume("run_ap5", {"approve": False, "reason": "not on prod"},
                               fake_llm, ["I could not run it"], executed)

        assert executed == []                     # still never ran
        resolved = next(e for e in events if e["type"] == "tool_approval_resolved")
        assert resolved["approved"] is False
        run = (await SharedState.restore("run_ap5")).run
        assert run.status == "completed"
        assert run.shared_state["result"] == "I could not run it"

    async def test_prose_is_read_as_an_answer(self, seeded, fake_llm):
        executed = []
        await _run(seeded, "run_ap6", fake_llm, [tool_call("danger"), "done"], executed)
        await _resume("run_ap6", {"response": "yes"}, fake_llm, ["ok"], executed)
        assert executed == [("danger", {})]

    async def test_an_unreadable_answer_is_a_refusal(self, seeded, fake_llm):
        """The pause exists because nobody wanted this to run unattended."""
        executed = []
        await _run(seeded, "run_ap7", fake_llm, [tool_call("danger"), "done"], executed)
        events = await _resume("run_ap7", {"response": "hmm not sure"}, fake_llm, ["ok"], executed)
        assert executed == []
        assert next(e for e in events if e["type"] == "tool_approval_resolved")["approved"] is False
