"""A nested orchestration hands its parent its work, not its last word (F249).

The parent got the child's final text, and that text was the value of the last
step with an output key — whatever kind of step it was. A child that ended in a
person's confirmation handed back the confirmation (`{'ok': True, …}`) three
times over, and none of its research reached the parent; a workflow deployed as
an agent answered its chat with an evaluator's route label ("pass"), the plan it
wrote sitting in a step card above.

Now a run's result is its `result_key` when the orchestration names one, and
otherwise the last step that produced content: a person's answer, an
evaluator's label and an End are not what a run made. The same rule answers a
chat with a workflow agent, v1's `response` and the judge. A nested parent gets
the child's value as it is — a list stays a list, where it used to arrive as
the text of a Python repr.
"""
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _delegate, _human, _print, _run, _server, driver, worker,
)


def _evaluator(sid, routes, key="grade"):
    return {"id": sid, "name": "Grade", "type": "evaluator", "evaluator_prompt": "good enough?",
            "route_map": routes, "route_descriptions": {k: k for k in routes},
            "model": "claude-x", "output_key": key, "next_step_id": None}


def _end(sid="end"):
    return {"id": sid, "name": "End", "type": "end", "next_step_id": None}


async def _nest(parent_id, child_steps, child_entry, *, result_key=""):
    """A parent whose one agent step runs a child orchestration."""
    parent = S.make_orchestration(id=parent_id, entry_step_id="delegate", steps=[
        _delegate(f"agent_{parent_id}", "tail"), _print("tail", "parent read {state.result}"),
    ])
    child = S.make_orchestration(id=f"{parent_id}_child", entry_step_id=child_entry,
                                 steps=child_steps, result_key=result_key)
    await S.seed_agents([S.make_orchestrator_agent(child["id"], id=f"agent_{parent_id}")])
    await S.seed_orchestrations([parent, child])
    return parent


class TestAChildReturnsItsWork:
    async def test_a_closing_question_is_not_what_it_returns(self, driver):
        parent = await _nest("orch_f249q", [
            _print("research", "Linear: 12 facts", nxt="confirm", key="facts"),
            _human("confirm", key="approval", nxt="end"),
            _end(),
        ], "research")
        assert await driver.start(parent, "run_f249q") == "paused"

        assert await driver.answer("run_f249q", {"ok": True, "correction": ""}) == "completed"

        run = await _run("run_f249q")
        assert run.shared_state["result"] == "Linear: 12 facts"
        assert run.shared_state["o_tail"] == "parent read Linear: 12 facts"

    async def test_a_routing_label_is_not_what_it_returns(self, driver, fake_llm):
        fake_llm.script([tool_call("route_pass", reasoning="good")])
        parent = await _nest("orch_f249e", [
            _print("write", "the plan", nxt="grade", key="plan"),
            _evaluator("grade", {"pass": None}),
        ], "write")

        assert await driver.start(parent, "run_f249e") == "completed"

        assert (await _run("run_f249e")).shared_state["result"] == "the plan"

    async def test_result_key_names_what_it_returns(self, driver):
        parent = await _nest("orch_f249k", [
            _print("facts", "the facts", nxt="summary", key="facts"),
            _print("summary", "a summary", key="summary"),
        ], "facts", result_key="facts")

        assert await driver.start(parent, "run_f249k") == "completed"

        assert (await _run("run_f249k")).shared_state["result"] == "the facts"

    async def test_a_list_arrives_as_a_list(self, driver):
        parent = await _nest("orch_f249l", [
            {"id": "t", "name": "Collect", "type": "transform", "transform_code": "result = ['a', 'b']",
             "output_key": "items", "next_step_id": None},
        ], "t")

        assert await driver.start(parent, "run_f249l") == "completed"

        assert (await _run("run_f249l")).shared_state["result"] == ["a", "b"]


async def _flat(orch_id, steps, entry, *, result_key=""):
    orch = S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps, result_key=result_key)
    await S.seed_orchestrations([orch])
    return orch


def _answer_of(events):
    return next(e["response"] for e in reversed(events)
                if e.get("type") == "final" and e.get("intent") == "orchestration")


class TestARunsAnswer:
    """What a chat with a workflow agent, v1 and the judge are told the run made."""

    async def test_a_closing_approval_is_not_the_answer(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = await _flat("orch_f249a", [
            _print("plan", "the 400-word plan", nxt="approve", key="plan"),
            _human("approve", key="approval", nxt="end"), _end(),
        ], "plan")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())
        [e async for e in engine.run("go", run_id="run_f249a")]

        events = [e async for e in OrchestrationEngine.resume("run_f249a", {"ok": True}, _server())]

        assert _answer_of(events) == "the 400-word plan"

    async def test_a_routing_label_is_not_the_answer(self, fake_llm):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        fake_llm.script([tool_call("route_pass", reasoning="good")])
        orch = await _flat("orch_f249b", [
            _print("plan", "the plan", nxt="grade", key="plan"), _evaluator("grade", {"pass": None}),
        ], "plan")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

        events = [e async for e in engine.run("go", run_id="run_f249b")]

        assert _answer_of(events) == "the plan"

    async def test_result_key_is_the_answer_and_v1_reads_it_back(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.run_view import read_run
        orch = await _flat("orch_f249c", [
            _print("facts", "the facts", nxt="note", key="facts"),
            _print("note", "a footnote", key="note"),
        ], "facts", result_key="facts")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

        events = [e async for e in engine.run("go", run_id="run_f249c")]

        assert _answer_of(events) == "the facts"
        assert (await read_run("run_f249c"))["response"] == "the facts"

    async def test_a_result_key_nothing_wrote_falls_back_to_the_last_content(self):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        orch = await _flat("orch_f249d", [_print("only", "made this", key="made")], "only",
                           result_key="never_written")
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server())

        events = [e async for e in engine.run("go", run_id="run_f249d")]

        assert _answer_of(events) == "made this"


class TestTheJudgeScoresTheSameAnswer:
    def _run(self, **kw):
        from core.models_orchestration import OrchestrationRun
        return OrchestrationRun(run_id="r", orchestration_id="o", **kw)

    def test_a_persons_answer_and_a_label_are_skipped(self):
        from core.evals import judge
        run = self._run(status="completed", shared_state={
            "plan": "the plan", "approval": {"ok": True}, "grade": "pass"}, step_history=[
            {"step_id": "p", "step_type": "print", "output_key": "plan"},
            {"step_id": "h", "step_type": "human", "output_key": "approval"},
            {"step_id": "g", "step_type": "evaluator", "output_key": "grade"},
        ])
        assert judge.final_output_of(run) == "the plan"

    def test_result_key_wins(self):
        from core.evals import judge
        run = self._run(status="completed", shared_state={"facts": "the facts", "note": "n"},
                        step_history=[{"step_id": "a", "step_type": "print", "output_key": "facts"},
                                      {"step_id": "b", "step_type": "print", "output_key": "note"}])
        assert judge.final_output_of(run, result_key="facts") == "the facts"


class TestTheBuilderCanNameIt:
    async def test_set_orchestration_meta_saves_it(self):
        from core.builder_tools import execute_builder_tool
        orch = await _flat("orch_f249m", [_print("only", key="made")], "only")

        await execute_builder_tool("set_orchestration_meta", {"orch_id": orch["id"], "result_key": "made"}, _server())

        from core.store.resources import get_orchestration
        assert (await get_orchestration(orch["id"]))["result_key"] == "made"

    def test_the_validator_names_a_result_key_no_step_writes(self):
        from core.builder_tools import _validate_orchestration
        doc = {"id": "o", "name": "O", "entry_step_id": "a", "result_key": "nothing_writes_this",
               "steps": [{"id": "a", "name": "A", "type": "print", "print_content": "x",
                          "output_key": "made", "next_step_id": "e"},
                         {"id": "e", "name": "End", "type": "end"}]}
        issues = _validate_orchestration(doc)["issues"]
        assert any("nothing_writes_this" in i for i in issues)
