"""A loop given more work than its cap fails before it starts (F254).

A for-each over 60 items with the default cap of 50 ran 50 iterations, dropped
the last ten and reported `stopped_reason: "collection_exhausted"`, with the run
`completed`: `total = min(len(items), cap)` was checked before the cap, so the
"hit its ceiling" warning could never fire for a for-each. A fixed count above
the cap was clamped the same way. Naveen's call: both fail before the first
iteration — nothing is half-processed — and the sentence says what to raise.
A while loop keeps its ceiling as its stop; that is what the cap is for there.
"""
import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _orch(loop_fields):
    return S.make_orchestration(id="orch_f254", entry_step_id="loop1", steps=[
        {"id": "loop1", "name": "Each item", "type": "loop", "loop_step_ids": ["body"],
         "next_step_id": "after", **loop_fields},
        {"id": "body", "name": "Body", "type": "print", "print_content": "{state.loop_index}",
         "output_key": "seen", "next_step_id": None},
        {"id": "after", "name": "After", "type": "print", "print_content": "done",
         "output_key": "tail", "next_step_id": None},
    ])


async def _run(orch, initial_state=None, run_id="run_f254"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    return [e async for e in engine.run("go", run_id=run_id, initial_state=initial_state)]


def _status(events):
    return next(e for e in reversed(events) if e.get("type") == "orchestration_complete")["status"]


def _iterations(events):
    return [e["iteration"] for e in events if e.get("type") == "loop_iteration"]


def _reason(events):
    return next(e for e in events if e.get("type") == "step_error" and e.get("reason"))["reason"]


class TestAForEachOverItsCap:
    async def test_fails_before_the_first_item(self):
        events = await _run(_orch({"loop_over": "state.items"}), initial_state={"items": list(range(60))})

        assert _status(events) == "failed"
        assert _iterations(events) == []
        assert "60 items exceed the 50-iteration cap" in _reason(events)
        assert "Max iterations" in _reason(events)
        assert not any(e.get("orch_step_id") == "after" for e in events)

    async def test_at_the_cap_runs_every_item(self):
        events = await _run(_orch({"loop_over": "state.items", "loop_max_iterations": 3}),
                            initial_state={"items": ["a", "b", "c"]})

        assert _status(events) == "completed"
        assert _iterations(events) == [1, 2, 3]

    async def test_a_resumed_snapshot_is_not_judged_again(self):
        """A run paused inside such a loop before this change resumes as it
        would have: its snapshot is the collection it was already iterating."""
        events = await _run(
            _orch({"loop_over": "state.items", "loop_max_iterations": 2}),
            initial_state={"items": [1, 2, 3, 4, 5],
                           "_loop_progress_loop1": {"completed_iterations": 1, "items": [1, 2, 3, 4, 5]}},
        )

        assert _status(events) == "completed"
        assert _iterations(events) == [2]


class TestACountOverItsCap:
    async def test_fails_before_the_first_iteration(self):
        events = await _run(_orch({"loop_count": 60}))

        assert _status(events) == "failed"
        assert _iterations(events) == []
        assert "count of 60 exceeds the 50-iteration cap" in _reason(events)


class TestAWhileLoopKeepsItsCeiling:
    async def test_it_stops_there_and_the_run_carries_on(self):
        events = await _run(_orch({"loop_while_condition": "True", "loop_max_iterations": 3}))

        assert _status(events) == "completed"
        assert _iterations(events) == [1, 2, 3]
        complete = next(e for e in events if e.get("type") == "loop_complete")
        assert complete["stopped_reason"] == "max_iterations"
