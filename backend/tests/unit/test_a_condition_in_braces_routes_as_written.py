"""A condition written in a prompt's braces routes as written; one that cannot run says so (F1433, Naveen 11-06).

Found by BLD1 in cap-05's saved workflow (orch_xrbh58g): an If/Else on `{{ state.request_approval.approved }}` with
the request approved took its "no" branch. The text parses as Python's set of a set, which only fails when it runs
("cannot use 'set' as a set element"), and an If/Else read any failure as False. So it passed check_build, saved,
and a yes became a no with nothing said.

Naveen's calls:
1. An expression field whose whole text is one `{{ … }}` is read as the expression inside it (all five fields).
2. One that still cannot be parsed (braces inside a longer expression, a typo) is refused when the plan is checked
   and when a workflow is saved, naming the step and field (`save_rules.refusal`, which check_build's dry_issues
   and every save door ask).
3. At run time an expression that errors fails its step with the reason, never a quiet false branch or default.
   A value the run has not written is data (Naveen 10-23, widened 11-06): read through, compared or combined, it
   keeps the field's documented answer and its warning (`TestAnExpressionThatCannotReadTheState` in
   test_a_refused_expression_fails_its_step, unchanged), and Synapse AI's shipped workflow keeps routing as it does.
"""
import json
from pathlib import Path

import pytest
from _fakes import seed as S

from test_a_refused_expression_fails_its_step import _if_else, _print, _reached, _reason, _run, _status, _switch

BRACED = "{{ state.request_approval.approved }}"


def _routed(events) -> list[str]:
    return [e["result"] for e in events if e.get("type") == "if_decision"]


class TestCapFivesGate:
    async def test_an_approved_request_takes_the_yes_branch(self):
        events = await _run(_if_else(BRACED), initial_state={"request_approval": {"approved": True}})

        assert _status(events) == "completed"
        assert _routed(events) == ["true"]
        assert _reached(events, "yes") and not _reached(events, "no")

    async def test_a_refused_request_takes_the_no_branch(self):
        events = await _run(_if_else(BRACED), initial_state={"request_approval": {"approved": False}})

        assert _routed(events) == ["false"] and _reached(events, "no")


class TestTheWholeTextInBraces:
    @pytest.mark.parametrize("text", [BRACED, "{{state.request_approval.approved}}", "  {{  state.request_approval.approved  }} "])
    def test_is_the_expression_inside(self, text):
        from core.expressions import check_expression, evaluate_expression

        assert check_expression(text) is None
        got = evaluate_expression(text, {"request_approval": {"approved": True}})
        assert (got.value, got.error) == (True, None)

    async def test_a_switch_routes_on_it(self):
        events = await _run(_switch("{{ state.classification }}"), initial_state={"classification": "urgent"})

        assert _status(events) == "completed" and _reached(events, "urgent") and not _reached(events, "routine")

    async def test_a_loop_goes_through_it(self):
        loop = {"id": "loop1", "name": "Each", "type": "loop", "loop_step_ids": ["body"], "next_step_id": "after",
                "loop_max_iterations": 5, "loop_over": "{{ state.items }}"}
        orch = S.make_orchestration(id="orch_f1433_loop", entry_step_id="loop1", steps=[
            loop, {**_print("body"), "output_key": "seen"}, _print("after")])
        events = await _run(orch, initial_state={"items": ["a", "b"]})

        assert _status(events) == "completed"
        assert sum(1 for e in events if e.get("type") == "step_start" and e.get("orch_step_id") == "body") == 2

    @pytest.mark.parametrize("text", ["{{ state.a }} and {{ state.b }}", "{{ state.a }} == 1", "not {{ state.a }}"])
    def test_braces_inside_a_longer_expression_are_an_error_that_says_so(self, text):
        from core.expressions import check_expression

        said = check_expression(text)
        assert said and "{{" in said and "state." in said, said


class TestRefusedWhenChecked:
    """check_build's dry_issues and every save door ask `save_rules.refusal` (build_plan.dry_issues, F850)."""

    @pytest.mark.parametrize(("field", "text"), [
        ("if_condition", "{{ state.a }} and state.b"),
        ("switch_expression", "{{ state.a }} or 'x'"),
        ("loop_while_condition", "state.x =="),
    ])
    def test_a_condition_that_cannot_be_parsed_is_refused_naming_step_and_field(self, field, text):
        from core.orchestration.save_rules import refusal

        said = refusal({"id": "o1", "steps": [{"id": "gate", "type": "if_else", field: text}]}, {})
        assert said and said.startswith("Refusing to save:") and f"step 'gate' {field}:" in said, said

    def test_the_whole_text_in_braces_saves(self):
        from core.orchestration.save_rules import refusal

        assert refusal({"id": "o1", "steps": [{"id": "gate", "type": "if_else", "if_condition": BRACED}]}, {}) is None


class TestAnErrorFailsTheStep:
    """An expression that runs into a real error fails its step with the reason, every field."""

    async def test_an_if_else_fails_and_takes_neither_branch(self):
        events = await _run(_if_else("state.count + 'x'"), initial_state={"count": 3})

        assert _status(events) == "failed"
        assert "If/Else condition" in _reason(events) and "unsupported operand" in _reason(events)
        assert not _reached(events, "yes") and not _reached(events, "no")

    async def test_a_condition_stored_before_the_save_rule_fails_rather_than_routing_false(self):
        events = await _run(_if_else("state.x =="))

        assert _status(events) == "failed" and "If/Else condition" in _reason(events)
        assert not _reached(events, "no")

    async def test_a_none_the_run_wrote_is_a_value_not_a_missing_one(self):
        events = await _run(_if_else("state.score > 5"), initial_state={"score": None})

        assert _status(events) == "failed" and "If/Else condition" in _reason(events)

    async def test_a_switch_fails_and_never_takes_its_default(self):
        events = await _run(_switch("state.count + 'x'"), initial_state={"count": 3})

        assert _status(events) == "failed" and "Switch expression" in _reason(events)
        assert not _reached(events, "routine")

    @pytest.mark.parametrize(("field", "expr", "words"), [
        ("loop_over", "state.count + 'x'", "Loop's list"),
        ("loop_while_condition", "state.count + 'x'", "Loop's while condition"),
        ("loop_break_condition", "state.count + 'x'", "Loop's break condition"),
    ])
    async def test_a_loop_fails_on_any_of_its_three(self, field, expr, words):
        loop = {"id": "loop1", "name": "Each", "type": "loop", "loop_step_ids": ["body"], "next_step_id": "after",
                "loop_max_iterations": 3, field: expr}
        if field == "loop_break_condition":
            loop["loop_count"] = 2
        orch = S.make_orchestration(id=f"orch_f1433_{field}", entry_step_id="loop1", steps=[
            loop, {**_print("body"), "output_key": "seen"}, _print("after")])
        events = await _run(orch, initial_state={"count": 3})

        assert _status(events) == "failed" and words in _reason(events), _reason(events)
        assert not _reached(events, "after")


class TestAValueTheRunHasNotWritten:
    """Data, as Naveen's 10-23 rule says, whether read through, compared or combined (widened 11-06)."""

    @pytest.mark.parametrize("expr", ["state.score > 5", "state.n + 1 > 2", "len(state.items) > 0", "state.missing.deep"])
    async def test_an_if_else_routes_false_as_before(self, expr):
        events = await _run(_if_else(expr))

        assert _status(events) == "completed" and _routed(events) == ["false"] and _reached(events, "no")

    @pytest.mark.parametrize(("expr", "key"), [
        ('state.plan_answer.outcome or "revise"', "plan_answer"),
        ('state.plan_checked.outcome or "error"', "plan_checked"),
        ("state.plan_answer.outcome", "plan_answer"),
        ("state.build_saved.outcome", "build_saved"),
        ("state.tools_made.outcome", "tools_made"),
        ("state.rows[3]", "rows"),
        ("state.reply['outcome']", "reply"),
    ])
    def test_a_field_the_written_value_does_not_have_is_data(self, expr, key):
        """CI2's FF25 run (6b7c3178): Synapse AI's own switches read `.outcome` off a step that wrote a str; the read
        finds nothing, whatever the value it read from, and the switch keeps its documented answer (eb 11-06)."""
        from core.orchestration.steps import _route_expr

        state = {key: ["a"] if key == "rows" else "the step wrote words, not a dict"}
        value, err = _route_expr(expr, state, "A shipped switch", default="the default")
        assert value == "the default" and err, (value, err)

    async def test_a_switch_on_a_str_where_a_dict_was_read_takes_its_default(self):
        events = await _run(_switch("state.classification.label"), initial_state={"classification": "urgent"})

        assert _status(events) == "completed" and _reached(events, "routine")

    def test_synapse_ais_own_conditions_on_an_empty_state_route_as_they_do(self):
        """Every expression in the shipped Synapse AI workflow, on an empty state: no step fails, and each gives
        the same answer it gave before the rule (its value; an unwritten read's error kept as data)."""
        from core.expressions import evaluate_expression
        from core.orchestration.save_rules import EXPRESSION_FIELDS
        from core.orchestration.steps import _route_expr

        doc = json.loads(Path(__file__).resolve().parents[2].joinpath(
            "core/native_builder/orchestration.json").read_text())
        seen = []

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in EXPRESSION_FIELDS and isinstance(v, str) and v.strip():
                        seen.append(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(doc)
        assert len(seen) >= 22, len(seen)
        for expr in seen:
            value, _err = _route_expr(expr, {}, "A shipped expression")  # raises if it would fail the step
            assert value == evaluate_expression(expr, {}).value, expr
