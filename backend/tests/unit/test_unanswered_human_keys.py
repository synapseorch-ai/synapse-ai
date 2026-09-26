"""
A model step is told a person answered only once a person has (F194).

The prompt's CONTEXT FROM PREVIOUS STEPS injected the output key of every human
step in the definition, labelled "Source: human response", whenever the key was
in state — and a schema default puts it there from the first step. So every
model step before the question read `### answer / Source: human response / {}`:
noise in every prompt, and a false claim to the model. Now a key joins that
block only when this run's history records a person's answer to it
(`human_response` after any answer); a step that lists the key in its
`input_keys` still gets it, as before.
"""
import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _llm(sid, nxt=None, **extra):
    return {"id": sid, "name": sid.upper(), "type": "llm", "prompt_template": f"work {sid}",
            "output_key": f"o_{sid}", "next_step_id": nxt, **extra}


def _orch(oid, **extra):
    return S.make_orchestration(
        id=oid, entry_step_id="before",
        state_schema={"answer": {"type": "dict", "default": {}}},
        steps=[
            _llm("before", nxt="ask"),
            {"id": "ask", "name": "ASK", "type": "human", "human_prompt": "ok?",
             "output_key": "answer", "next_step_id": "after"},
            _llm("after"),
        ],
        **extra,
    )


def _prompt(call: dict) -> str:
    return str(call.get("prompt_msg") or call.get("_args") or "")


async def _run_and_answer(orch, run_id, answer):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
    started = [e async for e in engine.run("go", run_id=run_id)]
    assert started[-1]["type"] == "human_input_required", [e["type"] for e in started]
    return [e async for e in OrchestrationEngine.resume(run_id, answer, _server())]


class TestTheHumanBlockWaitsForAnAnswer:
    async def test_a_step_before_the_question_is_not_told_of_an_answer(self, fake_llm):
        orch = _orch("orch_unanswered")
        await S.seed_orchestrations([orch])
        await _run_and_answer(orch, "run_unanswered", "ship it")

        before, after = (_prompt(c) for c in fake_llm.calls)
        assert "Source: human response" not in before, before
        assert "### answer" not in before and "### human_response" not in before, before
        assert "### answer\nSource: human response\nship it" in after, after

    async def test_a_declared_input_key_is_still_given_before_the_answer(self, fake_llm):
        orch = _orch("orch_declared")
        orch["steps"][0]["input_keys"] = ["answer"]
        await S.seed_orchestrations([orch])
        await _run_and_answer(orch, "run_declared", "ship it")

        before = _prompt(fake_llm.calls[0])
        assert "answer" in before and "Source: human response" not in before, before

    async def test_the_revision_snapshot_follows_the_same_rule(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.context import snapshot_inputs
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.state import SharedState

        orch = _orch("orch_snapshot")
        await S.seed_orchestrations([orch])
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_snapshot")]
        run = (await SharedState.restore("run_snapshot")).run
        before = snapshot_inputs(engine.step_map["before"], run, engine)
        assert "answer" not in before and "human_response" not in before, before

        [e async for e in OrchestrationEngine.resume("run_snapshot", "ship it", _server())]
        run = (await SharedState.restore("run_snapshot")).run
        after = snapshot_inputs(engine.step_map["after"], run, engine)
        assert after["answer"] == "ship it" and after["human_response"] == "ship it", after
