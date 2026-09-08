"""
Execution memory keeps the last N executions with bounded output.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, run_id="run_honest", engine=None):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = engine or OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [ev async for ev in engine.run("go", run_id=run_id)]


def _print(sid, nxt=None):
    return {"id": sid, "name": sid.upper(), "type": "print", "print_content": sid,
            "output_key": f"o_{sid}", "next_step_id": nxt}



class TestExecutionMemoryIsBounded:
    def _run_obj(self):
        from core.models_orchestration import OrchestrationRun
        return OrchestrationRun(run_id="r", orchestration_id="o")

    def _step(self):
        from core.models_orchestration import StepConfig, StepType
        return StepConfig(id="s1", name="S", type=StepType.AGENT, output_key="out")

    def test_keeps_the_last_n_and_numbers_continue(self):
        from core.orchestration.context import (EXEC_MEMORY_MAX_ENTRIES, get_execution_memory,
                                                store_execution_memory)
        run, step = self._run_obj(), self._step()
        for i in range(15):
            store_execution_memory(run, step, {"final_output": f"v{i}", "tool_calls": []}, "agent")
        mem = get_execution_memory(run, "s1")
        assert len(mem) == EXEC_MEMORY_MAX_ENTRIES == 10
        assert [m["execution"] for m in mem] == list(range(6, 16))     # not renumbered
        assert mem[-1]["trace"]["final_output"] == "v14"

    def test_stored_output_is_capped_but_the_trace_is_otherwise_intact(self):
        from core.orchestration.context import EXEC_MEMORY_MAX_OUTPUT_CHARS, get_execution_memory, store_execution_memory
        run, step = self._run_obj(), self._step()
        store_execution_memory(run, step, {"final_output": "x" * 50_000, "tool_calls": [{"name": "t"}],
                                            "tools_used": ["t"], "turn_count": 3}, "agent")
        entry = get_execution_memory(run, "s1")[0]
        assert len(entry["trace"]["final_output"]) <= EXEC_MEMORY_MAX_OUTPUT_CHARS + 50
        assert entry["trace"]["tool_calls"] == [{"name": "t"}]
        assert entry["trace"]["turn_count"] == 3

    def test_rendering_under_the_cap_is_unchanged(self):
        from core.orchestration.context import _render_full_history, store_execution_memory
        run, step = self._run_obj(), self._step()
        for i in range(3):
            store_execution_memory(run, step, {"final_output": f"draft {i}", "tool_calls": []}, "agent")
        text = _render_full_history(run.shared_state["_exec_memory_s1"])
        assert "### Turn 1" in text and "### Turn 3" in text
        assert "draft 2" in text


