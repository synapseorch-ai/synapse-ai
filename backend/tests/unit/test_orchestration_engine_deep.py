"""
Deep orchestration engine coverage: human pause + resume (checkpoint restore),
multi-step loop bodies, parallel branches with a dict merge, and resume_failed.
Orchestrations are seeded to disk so the resume paths can reload them.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _collect(agen):
    return [ev async for ev in agen]


class TestHumanResume:
    async def test_pause_then_resume_completes(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch_dict = S.make_orchestration(
            id="orch_human",
            entry_step_id="h",
            steps=[
                {"id": "h", "name": "Approve", "type": "human",
                 "human_prompt": "OK to proceed with {state.user_input}?",
                 "human_fields": [{"name": "decision", "type": "text", "label": "Decision"}],
                 "output_key": "approval", "input_keys": [], "next_step_id": "p"},
                {"id": "p", "name": "Show", "type": "print",
                 "print_content": "Decision was: {state.approval}",
                 "output_key": "final", "next_step_id": None},
            ],
        )
        S.seed_orchestrations([orch_dict])  # so resume() can reload it from disk
        orch = Orchestration.model_validate(orch_dict)
        server = _server()
        run_id = "run_human_1"

        # 1) Run until it pauses for human input.
        events = await _collect(OrchestrationEngine(orch, server).run("the task", run_id=run_id))
        assert any(e.get("type") == "human_input_required" for e in events)
        assert all(e.get("type") != "orchestration_complete" for e in events)

        # 2) Resume with the human's answer — should finish the print step.
        resumed = await _collect(OrchestrationEngine.resume(run_id, {"decision": "yes"}, server))
        types_seen = [e.get("type") for e in resumed]
        assert "orchestration_complete" in types_seen
        # The print step rendered the human response.
        finals = [e for e in resumed if e.get("type") == "final"]
        assert any("yes" in str(e.get("response", "")) for e in finals)


class TestLoopBody:
    async def test_loop_with_multistep_body(self, fake_llm):
        fake_llm.set_default("step-out")
        orch = S.make_orchestration(
            entry_step_id="lp",
            steps=[
                {"id": "lp", "name": "Loop", "type": "loop",
                 "loop_step_ids": ["a", "b"], "loop_count": 2, "next_step_id": "done"},
                {"id": "a", "name": "A", "type": "print", "print_content": "a", "output_key": "ra", "next_step_id": None},
                {"id": "b", "name": "B", "type": "llm", "prompt_template": "b {state.user_input}",
                 "output_key": "rb", "model": "claude-x", "next_step_id": None},
                {"id": "done", "name": "Done", "type": "print", "print_content": "finished",
                 "output_key": "final", "next_step_id": None},
            ],
        )
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        events = await _collect(OrchestrationEngine(Orchestration.model_validate(orch), _server())
                                .run("go", run_id="run_loopbody"))
        assert "orchestration_complete" in [e.get("type") for e in events]


class TestParallelMergeDict:
    async def test_parallel_dict_merge(self, fake_llm):
        fake_llm.set_default("branch")
        orch = S.make_orchestration(
            entry_step_id="par",
            steps=[
                {"id": "par", "name": "Fan", "type": "parallel",
                 "parallel_branches": [["b1"], ["b2"]], "next_step_id": "mrg"},
                {"id": "b1", "name": "B1", "type": "print", "print_content": "one", "output_key": "r1", "next_step_id": None},
                {"id": "b2", "name": "B2", "type": "print", "print_content": "two", "output_key": "r2", "next_step_id": None},
                {"id": "mrg", "name": "Merge", "type": "merge", "merge_strategy": "dict",
                 "input_keys": ["r1", "r2"], "output_key": "merged", "next_step_id": None},
            ],
        )
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        events = await _collect(OrchestrationEngine(Orchestration.model_validate(orch), _server())
                                .run("go", run_id="run_parmerge"))
        assert "orchestration_complete" in [e.get("type") for e in events]


class TestResumeFailed:
    async def test_resume_failed_unknown_run_raises(self):
        # The engine surfaces a missing checkpoint as FileNotFoundError; the route
        # layer catches it and returns a 'Run not found' error event.
        from core.orchestration.engine import OrchestrationEngine
        with pytest.raises(FileNotFoundError):
            await _collect(OrchestrationEngine.resume_failed("no_such_run", _server()))
