"""A number field is answered with a number, and a condition comparing text with a number says so — F1562 (HUM1;
Naveen 11-12).

The run page sends a number field as a number; an API caller, MCP's answer_run and a channel send what was typed. F455
took a number field's answer as it came, so `{"count": "3"}` stayed the text "3", and a later `state.review.count > 2`
failed the If/Else with Python's "'>' not supported between instances of 'str' and 'int'" (the stream said only "An
internal error occurred while executing this step.").

Now a number field takes its answer at intake as a number, at every door, as F1552 takes a Yes / No's words: "3" is 3,
" 2.5 " is 2.5, "1,200" is 1200; a blank is still an answer, as it came (F232). An answer that is no number is refused,
naming the field, before anything is resumed ("How many: “three” is not a number."). A lone number field takes a
plain reply that is one (a channel's "3"), as a lone Yes / No takes yes or no.

And a condition comparing values that cannot be compared says so in words, on the step's line and the run's record:
"`state.review.count` is text ('3') and `2` is a number: `>` compares a number with a number, or text with text".
"""
from __future__ import annotations

import re

import pytest
from _fakes import seed as S

from core.orchestration.human_fields import BadAnswer, check_answer

COUNT = {"name": "count", "type": "number", "label": "How many"}
YES_NO = {"name": "approved", "type": "boolean", "label": "Approve?"}


class TestTheAnswer:
    @pytest.mark.parametrize("given, kept", [
        ("3", 3), (" 7 ", 7), ("-2", -2), ("+4", 4), ("2.5", 2.5), (".5", 0.5), ("1e3", 1000.0), ("1,200", 1200),
        ("12,345.5", 12345.5), (3, 3), (2.5, 2.5), (0, 0),
        ("", ""), ("  ", "  "), (None, None),             # a blank is still an answer (F232)
    ])
    def test_a_number_is_kept_as_one(self, given, kept):
        got = check_answer([COUNT], {"count": given})["count"]
        assert got == kept and type(got) is type(kept), (given, got)

    @pytest.mark.parametrize("given", ["three", "3 apples", "1,2", "12,34", "1.2.3", "nan", "inf", "1e999", True,
                                       False, [3], {"n": 3}])
    def test_what_is_no_number_is_refused_naming_the_field(self, given):
        with pytest.raises(BadAnswer, match=r"^How many: .* is not a number\.$"):
            check_answer([COUNT], {"count": given})

    def test_a_lone_number_field_takes_a_plain_reply(self):
        assert check_answer([COUNT], "3", form=True) == {"count": 3}
        with pytest.raises(BadAnswer, match="is not a number"):
            check_answer([COUNT], "a few", form=True)

    def test_a_yes_no_is_as_f1552_left_it(self):
        assert check_answer([YES_NO], {"approved": "maybe"}) == {"approved": "maybe"}


async def _ask_then(condition: str, orch_id: str, answer: dict):
    """A review with a number, its If/Else, answered through the doors' own path: (the resumed events, the run)."""
    import asyncio
    import types

    from core.models_orchestration import Orchestration
    from core.orchestration import answers
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import SENTINEL
    from core.orchestration.state import SharedState

    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    orch = S.make_orchestration(id=orch_id, entry_step_id="review", steps=[
        {"id": "review", "name": "Review", "type": "human", "human_prompt": "How many?", "output_key": "review",
         "human_fields": [COUNT, {"name": "note", "type": "string", "label": "Note"}], "next_step_id": "gate"},
        {"id": "gate", "name": "Many?", "type": "if_else", "if_condition": condition, "if_true_step_id": "yes",
         "if_false_step_id": "no"},
        {"id": "yes", "name": "YES", "type": "print", "print_content": "yes", "output_key": "o", "next_step_id": None},
        {"id": "no", "name": "NO", "type": "print", "print_content": "no", "output_key": "o", "next_step_id": None},
    ])
    await S.seed_orchestrations([orch])
    run_id = f"run_{orch_id}"
    [_ async for _ in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id=run_id)]
    outcome = await answers.answer_paused_run(types.SimpleNamespace(server_module=server), run_id, answer)
    resumed = []
    while (event := await asyncio.wait_for(outcome.queue.get(), 10)) is not SENTINEL:
        resumed.append(event)
    return resumed, (await SharedState.restore(run_id)).run


class TestThroughTheDoors:
    @pytest.mark.parametrize("count, arm", [("3", "true"), ("1", "false"), (" 2.5 ", "true")])
    async def test_an_api_answer_of_text_routes_as_its_number(self, count, arm):
        name = re.sub(r"\W", "", count)
        resumed, run = await _ask_then("state.review.count > 2", f"orch_f1562_{name}", {"count": count, "note": ""})
        assert [e["result"] for e in resumed if e.get("type") == "if_decision"] == [arm]
        assert run.shared_state["review"]["count"] == float(count) and not isinstance(run.shared_state["review"]["count"], str)

    async def test_no_number_is_refused_before_anything_resumes(self):
        from core.orchestration.state import SharedState

        with pytest.raises(BadAnswer, match="How many: “three” is not a number"):
            await _ask_then("state.review.count > 2", "orch_f1562_three", {"count": "three"})
        run = (await SharedState.restore("run_orch_f1562_three")).run
        assert run.status == "paused" and run.waiting_for_human


class TestAConditionThatComparesTextWithANumber:
    async def test_says_so_in_words(self):
        resumed, run = await _ask_then("state.review.note > 2", "orch_f1562_words", {"count": 3, "note": "3"})
        [failed] = [e for e in resumed if e.get("type") == "step_error"]
        said = failed["reason"]
        assert "`state.review.note` is text ('3') and `2` is a number: `>` compares a number with a number, or text " \
               "with text" in said, said
        assert "not supported between instances" not in said, said
        [entry] = [h for h in run.step_history if h.get("step_id") == "gate"]
        assert entry["error"] == said, "the run's record says the same"

    @pytest.mark.parametrize("expr, said", [
        ("state.n >= 'a'", "`state.n` is a number (3) and `'a'` is text: `>=` compares"),
        ("'x' in state.n", "`state.n` is a number (3): `in` looks inside text, a list or an object"),
        ("state.items < 2", "`state.items` is a list ([1, 2]) and `2` is a number: `<` compares"),
    ])
    def test_every_ordering_and_in(self, expr, said):
        from core.expressions import evaluate_expression

        got = evaluate_expression(expr, {"n": 3, "items": [1, 2]}, "fallback")
        assert got.value == "fallback" and said in (got.error or ""), got

    def test_an_unwritten_value_is_data_as_before(self):
        """F1433: a value the run has not written, compared, is the step's documented answer, not a failure."""
        from core.expressions import evaluate_expression

        got = evaluate_expression("state.nothing.count > 2", {}, False)
        assert got.unwritten and got.value is False
