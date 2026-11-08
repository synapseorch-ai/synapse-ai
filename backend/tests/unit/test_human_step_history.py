"""
A human step is a step, and it appears in `step_history` like one.

It used to be the single kind that never did. `engine.py` breaks out of the step
loop at `human_input_required` *before* the "record step completion" block, and
`resume()` moved past the step without recording it — so an approval was
invisible three times over:

* to anything rendering a run's timeline, which showed the workflow skipping
  straight from the step before the approval to the step after it;
* to billing, which counts `len(step_history)` — every human-in-the-loop run
  under-counted by one step per approval;
* to `_apply_loop_guard`, which counts entries per step id — `exec_count` for a
  human step was permanently 0, so `max_iterations` was unreachable for it. That
  one is a behaviour change rather than a reporting fix; see the class below for
  what it does and does not amount to.

The entry is written at pause with status `paused` and closed on resume, so
history means "steps that started" whether or not the run is still waiting.
"""
import types

import pytest

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _collect(agen):
    return [ev async for ev in agen]


def _orch(**overrides):
    return S.make_orchestration(
        entry_step_id="h",
        steps=[
            {"id": "h", "name": "Approve", "type": "human",
             "human_prompt": "OK?",
             "human_fields": [{"name": "decision", "type": "text", "label": "Decision"}],
             "output_key": "approval", "input_keys": [], "next_step_id": "p"},
            {"id": "p", "name": "Show", "type": "print",
             "print_content": "Decision was: {state.approval}",
             "output_key": "final", "next_step_id": None},
        ],
        **overrides,
    )


async def _pause(orch_dict, run_id):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    await S.seed_orchestrations([orch_dict])
    orch = Orchestration.model_validate(orch_dict)
    events = await _collect(OrchestrationEngine(orch, _server()).run("go", run_id=run_id))
    assert any(e.get("type") == "human_input_required" for e in events)


async def _history(run_id):
    from core.orchestration.state import SharedState

    return (await SharedState.restore(run_id)).run.step_history


class TestWhilePaused:
    async def test_the_step_is_recorded_as_paused(self, fake_llm):
        await _pause(_orch(id="orch_paused"), "run_paused")

        history = await _history("run_paused")

        assert [h["step_id"] for h in history] == ["h"]
        assert history[0]["status"] == "paused"
        assert history[0]["step_type"] == "human"
        assert history[0]["step_name"] == "Approve"
        assert history[0]["started_at"]
        assert "ended_at" not in history[0], "a paused step has not ended"


class TestAfterResume:
    async def test_the_entry_is_closed_rather_than_duplicated(self, fake_llm):
        from core.orchestration.engine import OrchestrationEngine

        await _pause(_orch(id="orch_resumed"), "run_resumed")
        await _collect(OrchestrationEngine.resume("run_resumed", {"decision": "yes"}, _server()))

        history = await _history("run_resumed")

        assert [h["step_id"] for h in history] == ["h", "p"], "the human step was duplicated"
        assert history[0]["status"] == "completed"
        assert history[0]["ended_at"]
        assert history[0]["duration_seconds"] is not None

    async def test_a_run_paused_before_this_existed_still_gets_its_entry(self, fake_llm):
        """In-flight runs have a checkpoint with no `paused` entry to close.

        Anyone upgrading mid-run would otherwise resume and lose the approval
        from history entirely — the same silent-loss shape as a reader left
        behind by a moved collection.
        """
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        await _pause(_orch(id="orch_legacy"), "run_legacy")

        # Rewind to what a pre-change checkpoint looked like.
        restored = await SharedState.restore("run_legacy")
        restored.run.step_history = []
        await restored.checkpoint()

        await _collect(OrchestrationEngine.resume("run_legacy", {"decision": "yes"}, _server()))

        history = await _history("run_legacy")
        assert [h["step_id"] for h in history] == ["h", "p"]
        assert history[0]["status"] == "completed"


class TestTheLoopGuardNowSeesIt:
    """`_apply_loop_guard` counts entries per step id, so a human step's
    `exec_count` was permanently 0 and `max_iterations` was unreachable for it.

    Asserted against the guard directly rather than by running a cycle, because
    two *pre-existing* things would make an end-to-end version prove something
    other than what it claims, and both are worth writing down:

    * the guard's fallback for an over-run step X was X's own `next_step_id`, so
      it could not break a cycle — X -> Y -> X exceeded, routed to Y, and came
      back. (Since F248 an over-run step fails the run, or goes to the step it
      names in `on_exhausted_step_id`.);
    * `resume()` calls `_resolve_next` as `next_id, _ = ...`, discarding the
      event, so `loop_limit_reached` and routing decisions are never emitted
      when they happen at a resume boundary.

    Neither is touched here. This pins the half that did change.
    """

    def _engine(self, max_iterations):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch_dict = S.make_orchestration(
            id="orch_guard",
            entry_step_id="h",
            steps=[
                {"id": "h", "name": "Approve", "type": "human",
                 "human_prompt": "Again?",
                 "human_fields": [{"name": "decision", "type": "text", "label": "Decision"}],
                 "output_key": "approval", "input_keys": [],
                 "max_iterations": max_iterations, "next_step_id": "exit"},
                {"id": "exit", "name": "Done", "type": "print",
                 "print_content": "done", "output_key": "final", "next_step_id": None},
            ],
        )
        return OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())

    def _run(self, history):
        from core.models_orchestration import OrchestrationRun

        return OrchestrationRun(
            run_id="run_guard",
            orchestration_id="orch_guard",
            status="running",
            step_history=history,
        )

    def test_a_human_steps_executions_are_counted(self):
        from core.orchestration.engine import RouteFailed

        engine = self._engine(max_iterations=2)
        history = [
            {"step_id": "h", "step_name": "Approve", "step_type": "human", "status": "completed"},
            {"step_id": "h", "step_name": "Approve", "step_type": "human", "status": "paused"},
        ]

        # Two executions reach the cap of two: the guard fires (F248: with
        # nowhere named for the limit, the route fails the run).
        with pytest.raises(RouteFailed) as stop:
            engine._apply_loop_guard("h", self._run(history))

        event = stop.value.events[0]
        assert event["type"] == "loop_limit_reached"
        assert event["iterations"] == 2
        assert "'Approve' gave up after 2 attempts" in stop.value.message

    def test_counted_the_same_way_it_takes_the_step_named_for_the_limit(self):
        engine = self._engine(max_iterations=2)
        engine.step_map["h"].on_exhausted_step_id = "exit"
        history = [
            {"step_id": "h", "step_name": "Approve", "step_type": "human", "status": "completed"},
            {"step_id": "h", "step_name": "Approve", "step_type": "human", "status": "paused"},
        ]

        next_id, event = engine._apply_loop_guard("h", self._run(history))

        assert event["type"] == "loop_limit_reached" and event["iterations"] == 2
        assert next_id == "exit"

    def test_under_the_limit_it_still_routes_to_the_step(self):
        engine = self._engine(max_iterations=2)
        history = [
            {"step_id": "h", "step_name": "Approve", "step_type": "human", "status": "completed"},
        ]

        next_id, event = engine._apply_loop_guard("h", self._run(history))

        assert next_id == "h"
        assert event is None

    def test_before_this_change_the_count_was_always_zero(self):
        """The old shape, kept as the contrast: no entries, no guard, ever."""
        engine = self._engine(max_iterations=1)

        next_id, event = engine._apply_loop_guard("h", self._run([]))

        assert next_id == "h" and event is None


class TestWhatItCountsFor:
    async def test_the_step_count_includes_the_approval(self, fake_llm):
        """What an embedder meters. Cloud's finalize reads len(step_history)."""
        from core.orchestration.engine import OrchestrationEngine

        await _pause(_orch(id="orch_billed"), "run_billed")
        await _collect(OrchestrationEngine.resume("run_billed", {"decision": "yes"}, _server()))

        history = await _history("run_billed")
        assert len(history) == 2, (
            "a human-in-the-loop run must not bill as though the approval never happened"
        )
