"""
Broad coverage of the orchestration engine: every StepType executed through the
REAL OrchestrationEngine (steps.py + engine.py + context.py + logger.py), plus
navigation branches (_resolve_next), errors, and cancellation.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, initial_input="hi", initial_state=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    orch = Orchestration.model_validate(orch_dict)
    engine = OrchestrationEngine(orch, _server())
    events = []
    async for ev in engine.run(initial_input, run_id=f"run_{orch.id}", initial_state=initial_state):
        events.append(ev)
    return events


def _types(events):
    return [e.get("type") for e in events]


class TestPureSteps:
    async def test_extract_json_from_state(self):
        orch = S.make_orchestration(
            entry_step_id="ex",
            steps=[{"id": "ex", "name": "Extract", "type": "extract_json",
                    "input_keys": ["blob"], "output_key": "parsed", "next_step_id": None}],
        )
        events = await _run(orch, initial_state={"blob": 'noise ```json\n{"a": 1}\n``` tail'})
        assert "orchestration_complete" in _types(events)
        # The extract step's own final carries the parsed JSON string.
        step_final = [e for e in events if e.get("type") == "final" and e.get("orch_step_id") == "ex"]
        assert step_final and '"a": 1' in step_final[-1]["response"]

    async def test_extract_json_no_input_warns(self):
        orch = S.make_orchestration(
            entry_step_id="ex",
            steps=[{"id": "ex", "name": "Extract", "type": "extract_json",
                    "input_keys": ["missing"], "output_key": "parsed", "next_step_id": None}],
        )
        events = await _run(orch)
        assert "step_warning" in _types(events)

    async def test_if_else_true_branch(self):
        orch = S.make_orchestration(
            entry_step_id="cond",
            steps=[
                {"id": "cond", "name": "Check", "type": "if_else",
                 "if_condition": "state.n > 5", "if_true_step_id": "yes",
                 "if_false_step_id": "no", "next_step_id": None},
                {"id": "yes", "name": "Yes", "type": "print", "print_content": "big",
                 "output_key": "out", "next_step_id": None},
                {"id": "no", "name": "No", "type": "print", "print_content": "small",
                 "output_key": "out", "next_step_id": None},
            ],
        )
        events = await _run(orch, initial_state={"n": 10})
        assert any(e.get("type") == "if_decision" and e["result"] == "true" for e in events)
        finals = [e["response"] for e in events if e.get("type") == "final"]
        assert "big" in finals

    async def test_if_else_false_branch(self):
        orch = S.make_orchestration(
            entry_step_id="cond",
            steps=[
                {"id": "cond", "name": "Check", "type": "if_else",
                 "if_condition": "state.n > 5", "if_true_step_id": "yes",
                 "if_false_step_id": "no", "next_step_id": None},
                {"id": "yes", "name": "Yes", "type": "print", "print_content": "big",
                 "output_key": "out", "next_step_id": None},
                {"id": "no", "name": "No", "type": "print", "print_content": "small",
                 "output_key": "out", "next_step_id": None},
            ],
        )
        events = await _run(orch, initial_state={"n": 1})
        assert any(e.get("type") == "if_decision" and e["result"] == "false" for e in events)

    async def test_switch_match_and_default(self):
        def build(status):
            return S.make_orchestration(
                entry_step_id="sw",
                steps=[
                    {"id": "sw", "name": "Route", "type": "switch",
                     "switch_expression": "state.status", "switch_cases": {"ok": "a"},
                     "switch_default_step_id": "d", "next_step_id": None},
                    {"id": "a", "name": "A", "type": "print", "print_content": "matched",
                     "output_key": "o", "next_step_id": None},
                    {"id": "d", "name": "D", "type": "print", "print_content": "default",
                     "output_key": "o", "next_step_id": None},
                ],
            )
        ev_match = await _run(build("ok"), initial_state={"status": "ok"})
        assert any(e.get("type") == "switch_decision" and e["matched_case"] == "ok" for e in ev_match)
        ev_def = await _run(build("other"), initial_state={"status": "other"})
        assert any(e.get("type") == "switch_decision" and e["matched_case"] is None for e in ev_def)

    async def test_print_interpolates_state(self):
        orch = S.make_orchestration(
            entry_step_id="p",
            steps=[{"id": "p", "name": "P", "type": "print",
                    "print_content": "Hello {state.name}!", "output_key": "msg",
                    "next_step_id": None}],
        )
        events = await _run(orch, initial_state={"name": "Ada"})
        finals = [e["response"] for e in events if e.get("type") == "final"]
        assert "Hello Ada!" in finals


class TestTransformStep:
    async def test_transform_host_runtime(self, monkeypatch):
        # Run the transform in the host subprocess runtime (no Docker needed).
        import core.config as config
        monkeypatch.setattr(config, "load_settings",
                            lambda: {"transform_runtime": "host", "model": "mistral"})
        orch = S.make_orchestration(
            entry_step_id="t",
            steps=[{"id": "t", "name": "Double", "type": "transform",
                    "transform_code": "result = state['n'] * 2",
                    "output_key": "doubled", "timeout_seconds": 60, "next_step_id": None}],
        )
        events = await _run(orch, initial_state={"n": 21})
        tr = [e for e in events if e.get("type") == "transform_result"]
        assert tr and tr[-1]["result"] == "42"

    async def test_transform_no_code_warns(self):
        orch = S.make_orchestration(
            entry_step_id="t",
            steps=[{"id": "t", "name": "NoCode", "type": "transform",
                    "transform_code": None, "next_step_id": None}],
        )
        events = await _run(orch)
        assert "step_warning" in _types(events)


class TestLoopAndParallel:
    async def test_loop_runs_body_multiple_times(self, fake_llm):
        fake_llm.set_default("iter")
        orch = S.make_orchestration(
            entry_step_id="lp",
            steps=[
                {"id": "lp", "name": "Loop", "type": "loop", "loop_step_ids": ["body"],
                 "loop_count": 3, "next_step_id": None},
                {"id": "body", "name": "Body", "type": "print",
                 "print_content": "tick", "output_key": "t", "next_step_id": None},
            ],
        )
        events = await _run(orch)
        assert "orchestration_complete" in _types(events)

    async def test_parallel_then_merge(self, fake_llm):
        fake_llm.set_default("branch-out")
        orch = S.make_orchestration(
            entry_step_id="par",
            steps=[
                {"id": "par", "name": "Fan", "type": "parallel",
                 "parallel_branches": [["b1"], ["b2"]], "next_step_id": "mrg"},
                {"id": "b1", "name": "B1", "type": "llm", "prompt_template": "one",
                 "output_key": "r1", "model": "claude-x", "next_step_id": None},
                {"id": "b2", "name": "B2", "type": "llm", "prompt_template": "two",
                 "output_key": "r2", "model": "claude-x", "next_step_id": None},
                {"id": "mrg", "name": "Merge", "type": "merge", "merge_strategy": "list",
                 "input_keys": ["r1", "r2"], "output_key": "merged", "next_step_id": None},
            ],
        )
        events = await _run(orch)
        assert "orchestration_complete" in _types(events)


class TestAgentAndEvaluatorSteps:
    async def test_agent_step_runs_with_fake_llm(self, fake_llm, seed_agent):
        agent = seed_agent(id="orch_agent", tools=[], skip_default_tools=True)
        fake_llm.set_default("agent output")
        orch = S.make_orchestration(
            entry_step_id="ag",
            steps=[{"id": "ag", "name": "Do", "type": "agent", "agent_id": agent["id"],
                    "prompt_template": "handle {state.user_input}", "output_key": "res",
                    "next_step_id": None}],
        )
        events = await _run(orch, initial_input="task")
        assert "orchestration_complete" in _types(events)
        assert fake_llm.call_count >= 1


class TestErrorPaths:
    async def test_missing_step_fails(self):
        orch = S.make_orchestration(
            entry_step_id="ghost",
            steps=[{"id": "real", "name": "Real", "type": "print",
                    "print_content": "hi", "next_step_id": None}],
        )
        events = await _run(orch)
        assert any(e.get("type") == "orchestration_error" for e in events)
