"""
Per-iteration / per-branch crash recovery for Loop and Parallel steps.

Both executors checkpoint their progress into private shared_state keys
(_loop_progress_*, _parallel_progress_*) as work completes, skip the completed
portion when re-entered after a crash, and leave no residue on clean
completion.
"""
import json
import types

import pytest

from _fakes import seed as S




def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _run(orch_dict, initial_state=None, run_id="run_lp"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [ev async for ev in engine.run("go", run_id=run_id, initial_state=initial_state)]


def _loop_orch(loop_count=3):
    return S.make_orchestration(
        id="orch_loop_ckpt",
        entry_step_id="loop1",
        steps=[
            {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": loop_count,
             "loop_step_ids": ["body"], "next_step_id": None},
            {"id": "body", "name": "Body", "type": "print", "print_content": "tick",
             "output_key": "tick_out", "next_step_id": None},
        ],
    )


def _parallel_orch():
    return S.make_orchestration(
        id="orch_par_ckpt",
        entry_step_id="par",
        steps=[
            {"id": "par", "name": "Par", "type": "parallel",
             "parallel_branches": [["b0"], ["b1"]], "next_step_id": None},
            {"id": "b0", "name": "B0", "type": "print", "print_content": "zero",
             "output_key": "r0", "next_step_id": None},
            {"id": "b1", "name": "B1", "type": "print", "print_content": "one",
             "output_key": "r1", "next_step_id": None},
        ],
    )


async def _final_checkpoint(run_id="run_lp"):
    import core.orchestration.state as state_mod
    return (await state_mod.SharedState.restore(run_id)).run.model_dump(mode="json")


class TestLoopCheckpoint:
    async def test_resume_skips_completed_iterations(self):
        events = await _run(_loop_orch(3), initial_state={
            "_loop_progress_loop1": {"completed_iterations": 1},
        })
        iters = [e["iteration"] for e in events if e.get("type") == "loop_iteration"]
        assert iters == [2, 3]  # iteration 1 not re-run
        assert any(e.get("type") == "orchestration_complete" for e in events)

    async def test_checkpoints_each_iteration_and_cleans_up(self, monkeypatch):
        import core.orchestration.state as state_mod
        checkpoints: list[int] = []
        real_checkpoint = state_mod.SharedState.checkpoint

        def spy(self):
            progress = self.run.shared_state.get("_loop_progress_loop1")
            if isinstance(progress, dict):
                checkpoints.append(progress["completed_iterations"])
            return real_checkpoint(self)

        monkeypatch.setattr(state_mod.SharedState, "checkpoint", spy)
        await _run(_loop_orch(3))
        assert checkpoints[:3] == [1, 2, 3]  # one checkpoint per iteration
        assert "_loop_progress_loop1" not in (await _final_checkpoint())["shared_state"]

    async def test_fully_completed_progress_short_circuits(self):
        events = await _run(_loop_orch(2), initial_state={
            "_loop_progress_loop1": {"completed_iterations": 2},
        })
        assert [e for e in events if e.get("type") == "loop_iteration"] == []
        assert any(e.get("type") == "loop_complete" for e in events)


class TestParallelCheckpoint:
    async def test_resume_skips_completed_branch(self):
        events = await _run(_parallel_orch(), initial_state={
            "_parallel_progress_par": {"completed_branches": [0], "completed_steps": ["b0"]},
        })
        started = [e["orch_step_id"] for e in events if e.get("type") == "step_start"]
        assert "b0" not in started   # completed branch skipped
        assert "b1" in started
        branch_starts = [e["branch_index"] for e in events if e.get("type") == "branch_start"]
        assert branch_starts == [1]
        assert any(e.get("type") == "parallel_complete" for e in events)

    async def test_resume_skips_completed_substep_within_branch(self):
        """A crash mid-branch: earlier sub-steps of that branch don't re-run."""
        orch = S.make_orchestration(
            id="orch_par_chain",
            entry_step_id="par",
            steps=[
                {"id": "par", "name": "Par", "type": "parallel",
                 "parallel_branches": [["c1", "c2"]], "next_step_id": None},
                {"id": "c1", "name": "C1", "type": "print", "print_content": "one",
                 "output_key": "o1", "next_step_id": None},
                {"id": "c2", "name": "C2", "type": "print", "print_content": "two",
                 "output_key": "o2", "next_step_id": None},
            ],
        )
        events = await _run(orch, initial_state={
            "_parallel_progress_par": {"completed_branches": [], "completed_steps": ["c1"]},
        })
        started = [e["orch_step_id"] for e in events
                   if e.get("type") == "step_start" and e["orch_step_id"] != "par"]
        assert started == ["c2"]

    async def test_clean_run_leaves_no_residue(self):
        events = await _run(_parallel_orch(), run_id="run_par_clean")
        started = [e["orch_step_id"] for e in events
                   if e.get("type") == "step_start" and e["orch_step_id"] != "par"]
        assert started == ["b0", "b1"]
        state = (await _final_checkpoint("run_par_clean"))["shared_state"]
        assert "_parallel_progress_par" not in state
        assert state["r0"] == "zero" and state["r1"] == "one"


class TestLoopHumanPause:
    async def test_human_pause_keeps_the_loops_place(self):
        """Answering re-enters the loop where it stopped (F109). This used to
        assert the opposite: the engine resumed *past* the loop, dropping the
        remaining iterations, so the progress was thrown away at the pause."""
        orch = S.make_orchestration(
            id="orch_loop_human",
            entry_step_id="loop1",
            steps=[
                {"id": "loop1", "name": "Loop", "type": "loop", "loop_count": 3,
                 "loop_step_ids": ["ask"], "next_step_id": None},
                {"id": "ask", "name": "Ask", "type": "human",
                 "human_prompt": "Approve?", "output_key": "answer", "next_step_id": None},
            ],
        )
        events = await _run(orch, run_id="run_loop_human")
        assert any(e.get("type") == "human_input_required" for e in events)
        state = (await _final_checkpoint("run_loop_human"))["shared_state"]
        assert state["_loop_progress_loop1"] == {
            "completed_iterations": 0,
            "paused": {"iteration": 1, "index": 0, "step_id": "ask", "completed": [],
                       "answered": False},
        }
