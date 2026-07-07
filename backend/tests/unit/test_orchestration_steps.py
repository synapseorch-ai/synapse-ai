"""
Orchestration engine — real execution of representative step types.

Unlike the route tests (which patch the engine), these drive the REAL
OrchestrationEngine so the step executors, shared-state plumbing, and — for the
LLM step — the fake-LLM interception are all exercised end to end. Checkpoints
and logs land in the sandbox DATA_DIR.
"""
import types

import pytest

from _fakes import seed as S


def _server_module():
    """A minimal stand-in for core.server that step executors can read."""
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, initial_input="hello"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = Orchestration.model_validate(orch_dict)
    engine = OrchestrationEngine(orch, _server_module())
    events = []
    async for ev in engine.run(initial_input, run_id=f"run_test_{orch.id}"):
        events.append(ev)
    return events


class TestPrintStep:
    async def test_print_step_runs_to_completion(self):
        # make_orchestration defaults to a single PRINT step (no LLM).
        orch = S.make_orchestration()
        events = await _run(orch)
        types_seen = [e.get("type") for e in events]
        assert "orchestration_start" in types_seen
        assert "orchestration_complete" in types_seen


class TestLLMStep:
    async def test_llm_step_uses_fake_llm(self, fake_llm):
        fake_llm.set_default("SUMMARY: all good")
        orch = S.make_orchestration(
            entry_step_id="llm1",
            steps=[{
                "id": "llm1",
                "name": "Summarize",
                "type": "llm",
                "prompt_template": "Summarize: {state.user_input}",
                "output_key": "summary",
                "model": "claude-test",
                "next_step_id": None,
            }],
        )
        events = await _run(orch, initial_input="a long document")
        types_seen = [e.get("type") for e in events]
        assert "orchestration_complete" in types_seen
        # The fake LLM was actually invoked for the LLM step.
        assert fake_llm.call_count >= 1


class TestTwoStepChain:
    async def test_llm_then_print_chain(self, fake_llm):
        fake_llm.set_default("draft text")
        orch = S.make_orchestration(
            entry_step_id="s_llm",
            steps=[
                {
                    "id": "s_llm", "name": "Draft", "type": "llm",
                    "prompt_template": "Write about {state.user_input}",
                    "output_key": "draft", "model": "claude-test",
                    "next_step_id": "s_print",
                },
                {
                    "id": "s_print", "name": "Show", "type": "print",
                    "print_content": "Result: {state.draft}",
                    "output_key": "shown", "next_step_id": None,
                },
            ],
        )
        events = await _run(orch, initial_input="cats")
        types_seen = [e.get("type") for e in events]
        assert types_seen.count("step_start") >= 2  # both steps ran
        assert "orchestration_complete" in types_seen
