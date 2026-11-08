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
        await S.seed_orchestrations([orch_dict])  # so resume() can reload it from disk
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


class TestModelOverride:
    """One model for a whole run, chosen by the caller, carried in the state.

    It rides in shared_state rather than a `run()` argument because the state
    is checkpointed: a run that pauses on a human step and resumes on another
    worker has to come back on the model the user picked, not on whatever each
    step happens to name.
    """

    def _orch(self):
        from core.models_orchestration import Orchestration
        return Orchestration.model_validate(S.make_orchestration(steps=[
            {"id": "step_1", "name": "Think", "type": "llm", "model": "its-own-model",
             "prompt_template": "hi", "output_key": "out", "next_step_id": "step_2"},
            {"id": "step_2", "name": "Ask", "type": "human", "human_prompt": "?",
             "human_fields": [{"name": "answer", "type": "textarea", "label": "A"}],
             "output_key": "answer", "next_step_id": "step_3"},
            {"id": "step_3", "name": "End", "type": "end"},
        ], entry_step_id="step_1"))

    def test_it_reaches_every_llm_backed_step_and_skips_the_rest(self):
        from core.orchestration.engine import apply_model_override

        orch = self._orch()
        apply_model_override(orch, "chosen-model")

        by_id = {s.id: s for s in orch.steps}
        assert by_id["step_1"].model == "chosen-model"   # overrides the step's own
        assert by_id["step_2"].model is None             # human step runs no LLM
        assert by_id["step_3"].model is None             # nor does an end step

    def test_no_choice_leaves_every_step_as_authored(self):
        from core.orchestration.engine import apply_model_override

        orch = self._orch()
        apply_model_override(orch, None)
        assert {s.id: s.model for s in orch.steps}["step_1"] == "its-own-model"

    async def test_a_run_stamps_it_from_the_initial_state(self, tmp_path):
        import core.server as server
        from core.orchestration.engine import OrchestrationEngine

        engine = OrchestrationEngine(self._orch(), server)
        events = [
            e async for e in engine.run(
                initial_input="go", run_id="run_override",
                initial_state={OrchestrationEngine.MODEL_OVERRIDE_KEY: "chosen-model"},
            )
        ]
        assert any(e["type"] == "human_input_required" for e in events)
        assert {s.id: s.model for s in engine.orch.steps}["step_1"] == "chosen-model"

    async def test_a_resume_reapplies_it_from_the_checkpoint(self):
        """The failure this prevents: the second half of a builder conversation
        silently running on a different model from the first."""
        import core.server as server
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        orch = self._orch()
        engine = OrchestrationEngine(orch, server)
        async for _ in engine.run(
            initial_input="go", run_id="run_override2",
            initial_state={OrchestrationEngine.MODEL_OVERRIDE_KEY: "chosen-model"},
        ):
            pass

        run = (await SharedState.restore("run_override2")).run
        assert run.shared_state[OrchestrationEngine.MODEL_OVERRIDE_KEY] == "chosen-model"

        # Resume loads the definition fresh from the store, so every step comes
        # back with its authored model unless the override is re-applied.
        await S.seed_orchestrations([S.make_orchestration(id=orch.id, steps=[
            s.model_dump(mode="json") for s in self._orch().steps
        ], entry_step_id="step_1")])

        async for event in OrchestrationEngine.resume("run_override2", {"answer": "yes"}, server):
            if event.get("type") in ("orchestration_complete", "orchestration_error"):
                break

        # The definition the resumed engine ran, not the one we started with.
        from core.scale.context import resolve_orchestration
        from core.models_orchestration import Orchestration

        reloaded = Orchestration.model_validate(await resolve_orchestration(orch.id))
        assert {s.id: s.model for s in reloaded.steps}["step_1"] == "its-own-model"

        engines: list = []
        real_init = OrchestrationEngine.__init__

        def _spy(self, orchestration, *args, **kwargs):
            engines.append(orchestration)
            return real_init(self, orchestration, *args, **kwargs)

        import pytest as _pytest
        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(OrchestrationEngine, "__init__", _spy)
            async for event in OrchestrationEngine.resume("run_override2", {"answer": "yes"}, server):
                if event.get("type") in ("orchestration_complete", "orchestration_error"):
                    break

        assert engines, "resume built no engine"
        assert {s.id: s.model for s in engines[0].steps}["step_1"] == "chosen-model"
