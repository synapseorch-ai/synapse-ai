"""
Coverage for the remaining orchestration executors and engine control paths:
the evaluator (LLM routing decision), the tool step, and run cancellation.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, fake_llm=None, script=None, initial_state=None, run_id=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    if fake_llm is not None and script is not None:
        fake_llm.script(script)
    orch = Orchestration.model_validate(orch_dict)
    engine = OrchestrationEngine(orch, _server())
    rid = run_id or f"run_{orch.id}"
    return [ev async for ev in engine.run("go", run_id=rid, initial_state=initial_state)], engine


class TestEvaluatorStep:
    async def test_evaluator_routes_to_end(self, fake_llm):
        orch = S.make_orchestration(
            entry_step_id="ev",
            steps=[
                {"id": "ev", "name": "Decide", "type": "evaluator",
                 "evaluator_prompt": "pick a route",
                 "route_map": {"done": None, "again": "body"},
                 "route_descriptions": {"done": "finish", "again": "retry"},
                 "input_keys": [], "model": "claude-x", "next_step_id": None},
                {"id": "body", "name": "Body", "type": "print",
                 "print_content": "looping", "output_key": "b", "next_step_id": None},
            ],
        )
        events, _ = await _run(orch, fake_llm, [tool_call("route_done", reasoning="all good")])
        types_seen = [e.get("type") for e in events]
        assert "orchestration_complete" in types_seen
        # A routing decision was emitted.
        assert any(e.get("type") in ("routing_decision", "thinking") for e in events)

    async def test_evaluator_no_routes_warns(self, fake_llm):
        orch = S.make_orchestration(
            entry_step_id="ev",
            steps=[{"id": "ev", "name": "Decide", "type": "evaluator",
                    "route_map": {}, "next_step_id": None}],
        )
        events, _ = await _run(orch, fake_llm, ["irrelevant"])
        assert any(e.get("type") == "step_warning" for e in events)


class TestToolStep:
    async def test_tool_step_missing_tool_is_handled(self, fake_llm):
        # No MCP tools are registered, so a forced tool resolves to an error/warning
        # path rather than crashing the run.
        orch = S.make_orchestration(
            entry_step_id="tl",
            steps=[{"id": "tl", "name": "Call", "type": "tool",
                    "forced_tool": "nonexistent_tool",
                    "prompt_template": "use it", "output_key": "r",
                    "model": "claude-x", "next_step_id": None}],
        )
        events, _ = await _run(orch, fake_llm, [tool_call("nonexistent_tool", x=1), "done"])
        types_seen = [e.get("type") for e in events]
        # The run terminates (complete or error) without an unhandled crash.
        assert "orchestration_complete" in types_seen or "orchestration_error" in types_seen

    async def test_tool_step_without_forced_tool_warns(self, fake_llm):
        orch = S.make_orchestration(
            entry_step_id="tl",
            steps=[{"id": "tl", "name": "Call", "type": "tool", "forced_tool": None,
                    "next_step_id": None}],
        )
        events, _ = await _run(orch, fake_llm, ["x"])
        assert any(e.get("type") == "step_warning" for e in events)


class TestCancellation:
    async def test_cancelled_run_stops(self):
        from core.orchestration.state import _cancelled_run_ids
        orch = S.make_orchestration()  # single print step
        rid = "run_to_cancel"
        _cancelled_run_ids.add(rid)  # mark cancelled before it runs
        events, _ = await _run(orch, run_id=rid)
        # The engine observes the cancel flag and completes with 'cancelled' status
        # without ever running the print step.
        assert all(e.get("type") != "step_complete" for e in events)
        complete = [e for e in events if e.get("type") == "orchestration_complete"]
        assert complete and complete[-1].get("status") == "cancelled"
