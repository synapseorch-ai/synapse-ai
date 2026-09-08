"""
Every step_history entry — loop and parallel bodies included — has a timeline.
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



class TestEveryHistoryEntryHasATimeline:
    @staticmethod
    def _entries_of(events_run):
        return events_run

    async def test_loop_and_parallel_body_entries_are_stamped(self):
        from core.orchestration.state import SharedState
        orch = S.make_orchestration(
            id="orch_stamps", entry_step_id="loop1",
            steps=[
                {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 2,
                 "loop_step_ids": ["body"], "next_step_id": "par"},
                _print("body"),
                {"id": "par", "name": "Par", "type": "parallel",
                 "parallel_branches": [["b1"], ["b2"]], "next_step_id": None},
                _print("b1"), _print("b2"),
            ],
        )
        await _run(orch, run_id="run_stamps")
        history = (await SharedState.restore("run_stamps")).run.step_history
        assert len(history) >= 6
        for entry in history:
            assert entry.get("started_at"), entry
            assert entry.get("ended_at"), entry
            assert entry["ended_at"] >= entry["started_at"], entry
            assert "duration_seconds" in entry, entry
