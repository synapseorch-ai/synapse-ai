"""
A failed step's history entry carries the real error, a timeline, and is
checkpointed before finalize.
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



class TestTheRealErrorReachesTheRecord:
    async def test_history_names_the_exception_and_the_stream_does_not(self, monkeypatch):
        """Operators read the run record; the SSE stream is user-facing and
        keeps its sanitised message."""
        from core.orchestration import steps as steps_mod
        from core.orchestration.state import SharedState

        class Boom:
            async def execute(self, step, run, engine):
                raise KeyError("missing_config_key")
                yield  # pragma: no cover — makes this an async generator

        orch = S.make_orchestration(id="orch_boom", entry_step_id="p",
                                    steps=[_print("p")])
        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.PRINT: Boom()}

        events = await _run(orch, run_id="run_boom", engine=engine)

        err = next(e for e in events if e.get("type") == "step_error")
        assert err["error"] == "An internal error occurred while executing this step."
        assert "KeyError" not in err["error"]

        run = (await SharedState.restore("run_boom")).run
        entry = run.step_history[-1]
        assert entry["status"] == "failed"
        assert entry["error"] == "KeyError: 'missing_config_key'"
        assert entry["started_at"] and entry["ended_at"] and "duration_seconds" in entry
        assert run.status == "failed"

    async def test_the_failure_is_checkpointed_before_finalize(self, monkeypatch):
        """A second crash between the failure and the finalize checkpoint used
        to lose the failed entry entirely."""
        import core.orchestration.state as state_mod
        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine

        class Boom:
            async def execute(self, step, run, engine):
                raise RuntimeError("x" * 1000)
                yield

        seen_errors: list[str] = []
        real = state_mod.SharedState.checkpoint

        def spy(self, columns=None):
            for h in self.run.step_history:
                if h.get("status") == "failed":
                    seen_errors.append(h["error"])
            return real(self, columns=columns)

        monkeypatch.setattr(state_mod.SharedState, "checkpoint", spy)
        orch = S.make_orchestration(id="orch_boom2", entry_step_id="p", steps=[_print("p")])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.PRINT: Boom()}
        await _run(orch, run_id="run_boom2", engine=engine)

        assert len(seen_errors) >= 2                       # the failure checkpoint, then finalize
        assert seen_errors[0].startswith("RuntimeError: ")
        assert len(seen_errors[0]) <= len("RuntimeError: ") + 300   # bounded

    async def test_resume_failed_still_pops_the_entry_and_reruns(self, seed_orchestration, monkeypatch):
        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        orch = S.make_orchestration(id="orch_boom3", entry_step_id="p", steps=[_print("p")])
        await seed_orchestration(**orch)

        class Boom:
            async def execute(self, step, run, engine):
                raise ValueError("first time")
                yield

        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.PRINT: Boom()}
        await _run(orch, run_id="run_boom3", engine=engine)
        assert (await SharedState.restore("run_boom3")).run.status == "failed"

        events = [e async for e in OrchestrationEngine.resume_failed("run_boom3", _server())]
        assert any(e.get("type") == "orchestration_complete" for e in events)
        run = (await SharedState.restore("run_boom3")).run
        assert run.status == "completed"
        assert [h["status"] for h in run.step_history] == ["completed"]




class TestTheStreamCarriesTheReasonBesideItsSentence:
    """A member watching a run is told why a step failed (F195).

    The stream's `error` stays the sanitised sentence, for whatever forwards it
    unread; `reason` is the history's own words, which the run log and the step
    card show. Every surface that reads this stream is a member's or an API
    key's, and both already read the same words in the run record; the widget
    drops `step_error` altogether.
    """

    async def test_a_raised_step_carries_the_history_error_as_its_reason(self):
        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        class Boom:
            async def execute(self, step, run, engine):
                raise KeyError("missing_config_key")
                yield  # pragma: no cover — makes this an async generator

        orch = S.make_orchestration(id="orch_reason", entry_step_id="p", steps=[_print("p")])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.PRINT: Boom()}
        events = await _run(orch, run_id="run_reason", engine=engine)

        err = next(e for e in events if e.get("type") == "step_error")
        entry = (await SharedState.restore("run_reason")).run.step_history[-1]
        assert err["reason"] == entry["error"] == "KeyError: 'missing_config_key'"
        assert err["error"] == "An internal error occurred while executing this step."

    async def test_a_timed_out_step_carries_its_reason_too(self):
        import asyncio

        from core.models_orchestration import Orchestration, StepType
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        class Slow:
            async def execute(self, step, run, engine):
                await asyncio.sleep(5)
                yield {"type": "never"}  # pragma: no cover

        orch = S.make_orchestration(id="orch_reason_slow", entry_step_id="p", steps=[
            {**_print("p"), "timeout_seconds": 1}])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        engine.executors = {**engine.executors, StepType.PRINT: Slow()}
        events = await _run(orch, run_id="run_reason_slow", engine=engine)

        err = next(e for e in events if e.get("type") == "step_error")
        entry = (await SharedState.restore("run_reason_slow")).run.step_history[-1]
        assert err["reason"] == entry["error"] == "Timed out after 1s"
