"""An Agent step can finish by calling a tool, and its output is that tool's result, an object (E2, F772; SAI2-P1).

Structured output from an agent took two steps and a hand-off: the agent wrote prose, then a forced Tool step had a
model turn that prose into a call (Synapse AI's researcher → `report_findings`). `finish_tools` on an Agent step ends
its loop when one of them is called and succeeds: the step's output is the tool's result as an object, never prose.
A failed call, or one whose result is `{"error": …}`, goes back to the model; a reply in words gets one reminder,
inside the step's own turns; a second ends the step as failed. Every way a model's call arrives is read the same
(native calls and the text protocol through one parser, F758's flat arguments, the Codex bridge's recorded calls),
so the tests feed each shape.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _schema(name, props):
    return {"type": "function", "function": {"name": name, "description": f"{name}.", "parameters": {
        "type": "object", "properties": {p: {"type": "string"} for p in props}}}}


SUBMIT = _schema("submit_plan", ["title", "summary"])
LOOKUP = _schema("lookup", ["q"])
SEND = _schema("send", ["to"])


class _Tools:
    def __init__(self, submit_answers=None):
        self.ran: list[tuple[str, dict]] = []
        self.submit_answers = list(submit_answers or [])

    async def __call__(self, name, args):
        self.ran.append((name, args))
        if name == "submit_plan":
            if self.submit_answers:
                return self.submit_answers.pop(0)
            return json.dumps({"status": "accepted", "plan": args})
        return f"{name} ok"


async def _turn(tools, *, finish=("submit_plan",), max_turns=6, source="orchestration_step"):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    return [ev async for ev in run_agent_step(
        message="Plan the weekly report.", agent_id=agent["id"], session_id="s_e2",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[SUBMIT, LOOKUP, SEND], tool_executor=tools, max_turns=max_turns,
        source=source, finish_tools=list(finish))]


def _final(events):
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, "the turn ended with a final event"
    return finals[-1]


class TestItEndsOnTheFinishCall:
    async def test_the_finish_call_ends_the_turn_and_its_result_is_the_output(self, fake_llm):
        tools = _Tools()
        fake_llm.script([tool_call("submit_plan", title="Weekly", summary="Every Monday")])
        final = _final(await _turn(tools))
        assert final["finish"] == {"tool": "submit_plan",
                                   "result": {"status": "accepted", "plan": {"title": "Weekly", "summary": "Every Monday"}}}
        assert "no_answer" not in final
        assert fake_llm.call_count == 1, "no model call after the finish call"

    async def test_other_calls_before_it_run_as_usual(self, fake_llm):
        tools = _Tools()
        fake_llm.script([tool_call("lookup", q="reports"), tool_call("submit_plan", title="W", summary="S")])
        final = _final(await _turn(tools))
        assert [n for n, _ in tools.ran] == ["lookup", "submit_plan"]
        assert final["finish"]["tool"] == "submit_plan"

    async def test_a_call_after_it_in_the_same_reply_is_not_made(self, fake_llm):
        tools = _Tools()
        fake_llm.script([tool_call("submit_plan", title="W", summary="S") + "\n" + tool_call("send", to="ops")])
        final = _final(await _turn(tools))
        assert [n for n, _ in tools.ran] == ["submit_plan"]
        assert final["finish"]["tool"] == "submit_plan"

    async def test_a_result_that_is_not_an_object_is_wrapped(self, fake_llm):
        tools = _Tools(submit_answers=["Plan saved."])
        fake_llm.script([tool_call("submit_plan", title="W", summary="S")])
        assert _final(await _turn(tools))["finish"]["result"] == {"result": "Plan saved."}

    async def test_without_finish_tools_a_turn_is_as_before(self, fake_llm):
        tools = _Tools()
        fake_llm.script([tool_call("submit_plan", title="W", summary="S"), "Done: the plan is in."])
        final = _final(await _turn(tools, finish=()))
        assert "finish" not in final and final["response"] == "Done: the plan is in."
        assert fake_llm.call_count == 2


class TestAFailedFinishGoesBack:
    async def test_an_error_result_goes_back_to_the_model_and_counts_as_a_turn(self, fake_llm):
        tools = _Tools(submit_answers=[json.dumps({"error": "summary: required"})])
        fake_llm.script([tool_call("submit_plan", title="W"), tool_call("submit_plan", title="W", summary="S")])
        events = await _turn(tools)
        final = _final(events)
        assert final["finish"]["result"]["plan"] == {"title": "W", "summary": "S"}
        assert fake_llm.call_count == 2
        assert "summary: required" in str(fake_llm.calls[1])

    async def test_a_model_looping_on_a_bad_finish_ends_within_its_turns(self, fake_llm):
        tools = _Tools(submit_answers=[json.dumps({"error": "bad"})] * 10)
        fake_llm.script([tool_call("submit_plan", title="W")] * 10)
        final = _final(await _turn(tools, max_turns=3))
        assert "finish" not in final and final.get("no_answer")
        assert fake_llm.call_count <= 3


class TestAReplyInWords:
    async def test_it_gets_one_reminder_and_then_the_call(self, fake_llm):
        tools = _Tools()
        fake_llm.script(["Here is the plan: weekly report on Mondays.",
                         tool_call("submit_plan", title="W", summary="S")])
        final = _final(await _turn(tools))
        assert final["finish"]["tool"] == "submit_plan"
        assert fake_llm.call_count == 2
        assert "submit_plan" in str(fake_llm.calls[1]["prompt_msg"])

    async def test_a_second_reply_in_words_fails_the_step(self, fake_llm):
        tools = _Tools()
        fake_llm.script(["Here is the plan.", "As I said, the plan is above."])
        final = _final(await _turn(tools))
        assert "finish" not in final
        assert final["no_answer"]["why"] == "no_finish" and final["no_answer"]["tools"] == ["submit_plan"]
        assert fake_llm.call_count == 2

    async def test_the_reminder_is_one_of_the_steps_turns_not_an_extra(self, fake_llm):
        tools = _Tools()
        fake_llm.script(["Here is the plan.", tool_call("submit_plan", title="W", summary="S")])
        final = _final(await _turn(tools, max_turns=1))
        assert final["no_answer"]["why"] == "no_finish"
        assert fake_llm.call_count == 1

    async def test_json_alone_is_not_taken_as_the_answer(self, fake_llm):
        """F730 lets a workflow step answer with JSON alone; a step that finishes with a tool does not."""
        tools = _Tools()
        fake_llm.script(['{"title": "W", "summary": "S"}', tool_call("submit_plan", title="W", summary="S")])
        final = _final(await _turn(tools))
        assert final["finish"]["tool"] == "submit_plan"


class TestEveryWayACallArrives:
    async def test_flat_arguments_f758(self, fake_llm):
        tools = _Tools()
        fake_llm.script(['{"tool": "submit_plan", "title": "W", "summary": "S"}'])
        assert _final(await _turn(tools))["finish"]["result"]["plan"] == {"title": "W", "summary": "S"}

    async def test_the_text_protocols_tagged_call(self, fake_llm):
        tools = _Tools()
        fake_llm.script(['<tool_call>{"tool": "submit_plan", "arguments": {"title": "W", "summary": "S"}}</tool_call>'])
        assert _final(await _turn(tools))["finish"]["tool"] == "submit_plan"

    async def test_the_codex_bridges_recorded_calls(self, fake_llm):
        """The bridge records each call and hands them back as `<tool_call>` lines, in order (F753)."""
        tools = _Tools()
        fake_llm.script([
            '<tool_call>{"tool": "lookup", "arguments": {"q": "x"}}</tool_call>\n'
            '<tool_call>{"tool": "submit_plan", "arguments": {"title": "W", "summary": "S"}}</tool_call>\n'
            '<tool_call>{"tool": "send", "arguments": {"to": "ops"}}</tool_call>'])
        final = _final(await _turn(tools))
        assert [n for n, _ in tools.ran] == ["lookup", "submit_plan"]
        assert final["finish"]["tool"] == "submit_plan"


class TestInAWorkflow:
    async def test_the_steps_output_is_the_object(self, fake_llm, monkeypatch):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        import core.react_engine as re_mod

        real = re_mod.run_agent_step
        tools = _Tools()

        def with_tools(*args, **kwargs):
            kwargs.update(tools_override=[SUBMIT, LOOKUP], tool_executor=tools)
            return real(*args, **kwargs)

        monkeypatch.setattr(re_mod, "run_agent_step", with_tools)
        agent = S.make_agent(id="agent_e2", tools=["submit_plan", "lookup"], skip_default_tools=True)
        await S.seed_agents([agent])
        orch = S.make_orchestration(id="orch_e2", entry_step_id="plan", steps=[
            {"id": "plan", "name": "Plan", "type": "agent", "agent_id": "agent_e2", "prompt_template": "plan it",
             "finish_tools": ["submit_plan"], "output_key": "plan", "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.script([tool_call("submit_plan", title="W", summary="S")])

        events = [e async for e in OrchestrationEngine(
            Orchestration.model_validate(orch),
            types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})).run(
            "go", run_id="run_e2")]

        done = [e for e in events if e.get("type") == "orchestration_complete"]
        assert done[-1]["status"] == "completed"
        assert done[-1]["final_state"]["plan"] == {"status": "accepted", "plan": {"title": "W", "summary": "S"}}

    async def test_a_step_that_never_finishes_fails_saying_why(self, fake_llm, monkeypatch):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        import core.react_engine as re_mod

        real = re_mod.run_agent_step

        def with_tools(*args, **kwargs):
            kwargs.update(tools_override=[SUBMIT], tool_executor=_Tools())
            return real(*args, **kwargs)

        monkeypatch.setattr(re_mod, "run_agent_step", with_tools)
        agent = S.make_agent(id="agent_e2f", tools=["submit_plan"], skip_default_tools=True)
        await S.seed_agents([agent])
        orch = S.make_orchestration(id="orch_e2f", entry_step_id="plan", steps=[
            {"id": "plan", "name": "Plan", "type": "agent", "agent_id": "agent_e2f", "prompt_template": "plan it",
             "finish_tools": ["submit_plan"], "output_key": "plan", "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.script(["A plan in words.", "Still words."])

        events = [e async for e in OrchestrationEngine(
            Orchestration.model_validate(orch),
            types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})).run(
            "go", run_id="run_e2f")]

        done = [e for e in events if e.get("type") == "orchestration_complete"]
        assert done[-1]["status"] == "failed"
        said = " ".join(str(e.get("reason") or e.get("error") or "") for e in events)
        assert "submit_plan" in said


    async def test_a_fallback_is_the_output_when_it_still_answers_in_words(self, fake_llm, monkeypatch):
        """F958 (P2's dev after-pass, chg-05): a request outside what the change planner can propose had it answer in
        words twice, the step failed, and the person read the engine's sentence. A step may name the object it ends
        with then (`finish_fallback`, off by default): code's output, marked as the fallback, never the words."""
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        import core.react_engine as re_mod

        real = re_mod.run_agent_step

        def with_tools(*args, **kwargs):
            kwargs.update(tools_override=[SUBMIT], tool_executor=_Tools())
            return real(*args, **kwargs)

        monkeypatch.setattr(re_mod, "run_agent_step", with_tools)
        agent = S.make_agent(id="agent_e2b", tools=["submit_plan"], skip_default_tools=True)
        await S.seed_agents([agent])
        orch = S.make_orchestration(id="orch_e2b", entry_step_id="plan", steps=[
            {"id": "plan", "name": "Plan", "type": "agent", "agent_id": "agent_e2b", "prompt_template": "plan it",
             "finish_tools": ["submit_plan"], "finish_fallback": {"finish": "cannot"}, "output_key": "plan",
             "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.script(["A plan in words.", "Still words."])

        events = [e async for e in OrchestrationEngine(
            Orchestration.model_validate(orch),
            types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})).run(
            "go", run_id="run_e2b")]

        done = [e for e in events if e.get("type") == "orchestration_complete"]
        assert done[-1]["status"] == "completed"
        assert done[-1]["final_state"]["plan"] == {"finish": "cannot", "fallback": True}


class TestOnlyAnAgentStepTakesThem:
    def test_finish_tools_on_another_step_type_are_refused(self):
        from pydantic import ValidationError

        from core.models_orchestration import StepConfig

        with pytest.raises(ValidationError, match="finish_tools"):
            StepConfig(id="l", name="L", type="llm", finish_tools=["x"])
        assert StepConfig(id="a", name="A", type="agent", finish_tools=["x"]).finish_tools == ["x"]

    def test_a_fallback_needs_finish_tools(self):
        from pydantic import ValidationError

        from core.models_orchestration import StepConfig

        with pytest.raises(ValidationError, match="finish_fallback"):
            StepConfig(id="a", name="A", type="agent", finish_fallback={"finish": "cannot"})
        step = StepConfig(id="a", name="A", type="agent", finish_tools=["x"], finish_fallback={"finish": "x"})
        assert step.finish_fallback == {"finish": "x"}

    def test_the_validator_wants_them_among_the_agents_tools(self):
        from core.builder_tools import _validate_orchestration

        orch = {"id": "o", "name": "O", "entry_step_id": "a", "steps": [
            {"id": "a", "name": "A", "type": "agent", "agent_id": "ag", "prompt_template": "x", "output_key": "out",
             "finish_tools": ["submit_plan", "elsewhere"], "next_step_id": "end"},
            {"id": "end", "name": "End", "type": "end"}]}
        agents = {"ag": {"id": "ag", "name": "Planner", "tools": ["submit_plan", "lookup"]}}
        issues = _validate_orchestration(orch, agents=agents)["issues"]
        assert len(issues) == 1 and "elsewhere" in issues[0] and "Planner" in issues[0]
        agents["ag"]["tools"] = ["all"]
        assert _validate_orchestration(orch, agents=agents)["ok"]
