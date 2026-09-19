"""`WorkerEngineAdapter.resume` — the path every fleet resume actually takes.

## Why this file exists

`WorkerEngineAdapter.resume()` was a near-copy of `OrchestrationEngine.resume()`
— the adapter uses the orchestration the worker already loaded from the store
instead of reloading a definition the process may not have — which made the two
a pair that had to change together. Since F109 both call `resume_paused`, and
`TestThereIsOneResume` below keeps it that way.

They drifted. `78076d3` taught the classmethod to close the `paused` entry a
human step leaves in `step_history`, and did not teach the adapter. Every test
of that fix drove the classmethod (`test_cross_process_resume.py:68`,
`test_human_step_history.py`), so the suite was green while **every resume on a
shared fleet** left the approval open forever:

* a finished run still showed its human step as awaiting input, with a duration
  that grew against wall-clock for as long as anyone looked at it;
* `_apply_loop_guard` never counted a human step inside a loop, so the guard
  that stops a runaway loop had a hole in it exactly where a human was involved.

This is the `mcp_pool` shape from `test_shared_fleet.py`: a whole code path no
test executed. So these tests drive the adapter, and only the adapter.
"""
import types

import pytest

from _fakes import seed as S

#: print -> human -> print. History on both sides of the pause, so "the entry
#: was closed" is distinguishable from "no entry was written".
PAUSING = dict(
    id="orch_adapter_resume",
    entry_step_id="before",
    steps=[
        {"id": "before", "name": "Log the request", "type": "print",
         "print_content": "Received {state.ticket}",
         "output_key": "logged", "next_step_id": "h"},
        {"id": "h", "name": "Approve", "type": "human",
         "human_prompt": "OK to proceed?",
         "human_fields": [{"name": "decision", "type": "text", "label": "Decision"}],
         "output_key": "approval", "input_keys": [], "next_step_id": "after"},
        {"id": "after", "name": "Show", "type": "print",
         "print_content": "Decision was: {state.approval}",
         "output_key": "final", "next_step_id": None},
    ],
)


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _adapter(run_id: str, fake_redis):
    """A `WorkerEngineAdapter` wired the way `core/scale/worker.py` wires one."""
    from core.models_orchestration import Orchestration
    from core.scale.pubsub import RunEventPublisher
    from core.scale.worker_engine_adapter import WorkerEngineAdapter
    from core.store import get_store

    return WorkerEngineAdapter(
        orch=Orchestration.model_validate(S.make_orchestration(**PAUSING)),
        run_id=run_id,
        worker_server_module=_server(),
        publisher=RunEventPublisher(fake_redis, run_id),
        session_factory=await get_store(),
        redis_client=fake_redis,
        worker_id="w-test",
        job_id="job-test",
    )


async def _pause(run_id: str, fake_redis) -> None:
    """Run until the human step, through the adapter, as a worker would."""
    await S.seed_orchestrations([S.make_orchestration(**PAUSING)])
    adapter = await _adapter(run_id, fake_redis)
    status = await adapter.run(initial_input="the task", initial_state={"ticket": "SYN-1"})
    assert status == "paused", f"expected the run to park on the human step, got {status!r}"


def _entry(history: list[dict], step_id: str) -> dict:
    matching = [e for e in history if e.get("step_id") == step_id]
    assert matching, f"no {step_id!r} entry in {[e.get('step_id') for e in history]}"
    return matching[-1]


class TestResumingThroughTheAdapter:
    async def test_the_human_step_entry_is_closed(self, fake_redis):
        """The regression. Without `_complete_human_step` the entry keeps
        `status: paused` and never gets an `ended_at`, so a completed run
        renders an approval that still looks outstanding."""
        from core.orchestration.state import SharedState

        run_id = "run_adapter_close"
        await _pause(run_id, fake_redis)

        paused = _entry((await SharedState.restore(run_id)).run.step_history, "h")
        assert paused["status"] == "paused", "precondition: the pause opens the entry"
        assert "ended_at" not in paused

        adapter = await _adapter(run_id, fake_redis)
        assert await adapter.resume({"decision": "yes"}) == "completed"

        run = (await SharedState.restore(run_id)).run
        closed = _entry(run.step_history, "h")
        assert closed["status"] == "completed", (
            "the human step is still open after a fleet resume — "
            "WorkerEngineAdapter.resume has drifted from OrchestrationEngine.resume"
        )
        assert closed.get("ended_at"), "a closed step needs an end, or duration is unbounded"
        assert closed.get("duration_seconds") is not None

    async def test_the_run_finishes_and_keeps_every_step(self, fake_redis):
        """The approval is one entry, not zero and not two — `_complete_human_step`
        closes the existing entry rather than appending a second."""
        from core.orchestration.state import SharedState

        run_id = "run_adapter_history"
        await _pause(run_id, fake_redis)
        adapter = await _adapter(run_id, fake_redis)
        await adapter.resume({"decision": "yes"})

        run = (await SharedState.restore(run_id)).run
        assert [e.get("step_id") for e in run.step_history] == ["before", "h", "after"]
        assert run.status == "completed"
        assert run.waiting_for_human is False

    async def test_the_answer_lands_under_the_steps_output_key(self, fake_redis):
        """`approval`, from the step's `output_key` — and `human_response` too,
        which evaluators written against the old name still read."""
        from core.orchestration.state import SharedState

        run_id = "run_adapter_output_key"
        await _pause(run_id, fake_redis)
        adapter = await _adapter(run_id, fake_redis)
        await adapter.resume({"decision": "yes"})

        state = (await SharedState.restore(run_id)).run.shared_state
        assert state["approval"] == {"decision": "yes"}
        assert state["human_response"] == {"decision": "yes"}
        # The step after the pause read it, which is the point of the key.
        assert "Decision was:" in str(state["final"])


class TestThereIsOneResume:
    def test_both_entry_points_hand_the_answer_to_the_same_method(self):
        """A source-level tripwire for the drift itself.

        The adapter builds its engine from the orchestration the worker already
        loaded, so the two entry points cannot be one function. They used to be
        two copies of the same logic, and the copy drifted three times — the
        last time it had no tool-approval branch at all (F109). Now each hands
        the answer to `resume_paused`, and neither may decide anything itself:
        a call to one of these below means the copy is growing back.
        """
        import ast
        import inspect
        import textwrap

        from core.orchestration.engine import OrchestrationEngine
        from core.scale.worker_engine_adapter import WorkerEngineAdapter

        def calls(fn) -> set[str]:
            """Attribute names actually invoked, from the AST.

            A substring search would pass on the *comment* that explains why the
            call is there — which is not hypothetical: deleting the call and
            leaving its comment is exactly what this test let through the first
            time it was written.
            """
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            return {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }

        deciding = {"_complete_human_step", "_resolve_next", "_execute_loop",
                    "_resume_nested", "_resume_tool_approval"}
        for name, fn in (("OrchestrationEngine.resume", OrchestrationEngine.resume),
                         ("WorkerEngineAdapter.resume", WorkerEngineAdapter.resume)):
            made = calls(fn)
            assert "resume_paused" in made, f"{name} no longer calls resume_paused"
            assert not made & deciding, f"{name} decides for itself again: {sorted(made & deciding)}"
