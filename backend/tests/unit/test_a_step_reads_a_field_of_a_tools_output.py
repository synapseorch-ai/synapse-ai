"""
A condition, a switch, a prompt and a Print read a field of a Tool step's JSON output, as set arguments do (F801,
SAI2-P2, D308).

A builder tool answers with JSON text, and a Tool step keeps that text as its output. E1's set arguments already step
into it (`{{ state.checked.outcome }}`, `steps._state_value`), but nothing else did: `state.checked.outcome` in a
switch raised "'str' object has no attribute 'outcome'" (so the switch matched nothing), and `{{ state.checked.say }}` in
a Print was left as written. Synapse AI's change route branches on its code steps' answers, so every step now reads a
field of JSON text the one way. Only paths that failed before change: a string that is not a JSON object, and
anything a method call or `len` does to a string, are as they were.
"""
import json

import pytest

from core.expressions import evaluate_expression
from core.orchestration.steps import render_state_refs

OUT = json.dumps({"outcome": "review", "count": 3, "say": "Two cards are up.", "inner": {"ok": True}})
STATE = {"checked": OUT, "plain": "not json", "listed": json.dumps([1, 2]), "number": "42"}


@pytest.mark.parametrize("expr, expected", [
    ("state.checked.outcome", "review"),
    ("state.checked.count > 2", True),
    ("state.checked.inner.ok", True),
    ("state.checked.missing", None),
    ("state.checked.outcome == 'review' and state.checked.count == 3", True),
])
def test_an_expression_steps_into_json_text(expr, expected):
    result = evaluate_expression(expr, STATE, None)
    assert result.error is None and not result.refused
    assert result.value == expected


@pytest.mark.parametrize("expr, expected", [
    ("state.checked.startswith('{')", True),
    ("len(state.number)", 2),
    ("state.number == '42'", True),
    ("state.checked == " + repr(OUT), True),
])
def test_what_a_string_did_it_still_does(expr, expected):
    result = evaluate_expression(expr, STATE, None)
    assert result.error is None
    assert result.value == expected


@pytest.mark.parametrize("expr", ["state.plain.outcome", "state.listed.outcome", "state.number.outcome"])
def test_text_that_is_not_a_json_object_has_no_fields(expr):
    result = evaluate_expression(expr, STATE, "fallback")
    assert result.value == "fallback" and result.error


def test_a_switch_value_from_json_text_is_plain_json():
    result = evaluate_expression("state.checked.inner", STATE, None)
    assert result.value == {"ok": True} and type(result.value) is dict


def test_a_prompt_and_a_print_read_a_field_of_json_text():
    assert render_state_refs("{{ state.checked.say }} ({{ state.checked.count }})", STATE) == "Two cards are up. (3)"
    assert render_state_refs("{{ state.checked.inner.ok }}", STATE) == "True"


def test_a_reference_into_text_that_is_not_json_is_left_as_written():
    assert render_state_refs("{{ state.plain.say }}", STATE) == "{{ state.plain.say }}"
    assert render_state_refs("{{ state.checked.nothing }}", STATE) == "{{ state.checked.nothing }}"
    assert render_state_refs("{{ state.checked }}", STATE) == OUT


SNEAKY = {"x": json.dumps({"__class__": "boom", "_private": "secret", "ok": 1})}


@pytest.mark.parametrize("expr", ["state.checked.__class__", "state.checked._private", "state.x._private",
                                  "state.x.__class__", "state.x.ok.__class__"])
def test_a_private_or_dunder_name_is_still_refused_inside_json_text(expr):
    result = evaluate_expression(expr, {**STATE, **SNEAKY}, "fallback")
    assert result.refused and result.value == "fallback"


def test_json_text_is_parsed_never_evaluated():
    state = {"x": "{'a': __import__('os')}"}  # Python, not JSON: no fields, and nothing runs
    result = evaluate_expression("state.x.a", state, "fallback")
    assert result.value == "fallback" and not result.refused
    assert evaluate_expression("state.x['ok']", SNEAKY, "fallback").value == "fallback"  # a subscript is unchanged


def test_a_model_s_prompt_reads_a_field_of_json_text_and_never_renders_it():
    from core.orchestration.context import _fill_task_template

    state = {**STATE, "trap": json.dumps({"say": "{{ global.API_KEY }}"})}
    assert _fill_task_template("{{ state.checked.say }} / {{ state.checked.count }}", state, {}) == (
        "Two cards are up. / 3")
    assert _fill_task_template("{{ state.trap.say }}", state, {"API_KEY": "s3cret"}) == "{{ global.API_KEY }}"
    assert _fill_task_template("{{ state.plain.say }}", state, {}) == "{{ state.plain.say }}"
    assert _fill_task_template("{{ state.unset.say }}", state, {}) == ""


def test_text_nested_too_deep_to_read_has_no_fields_and_fails_nothing():
    """Text nested thousands deep is no JSON object: a step reading a field of it reads nothing, as for any text
    that is not a JSON object (the branch review's finding 13; counted since F1164, below)."""
    from core.orchestration.context import _fill_task_template

    deep = '{"a": ' * 100_000 + "1" + "}" * 100_000
    state = {"deep": deep}
    assert evaluate_expression("state.deep.a", state, "fallback").value == "fallback"
    assert render_state_refs("{{ state.deep.a }}", state) == "{{ state.deep.a }}"
    assert _fill_task_template("{{ state.deep.a }}", state, {}) == "{{ state.deep.a }}"
    assert _fill_task_template("{{ state.deep.a }}", state, {}) == "{{ state.deep.a }}"


def test_a_set_argument_into_text_nested_too_deep_names_nothing():
    from core.orchestration.steps import MissingStateRef, render_tool_arguments

    with pytest.raises(MissingStateRef):
        render_tool_arguments({"x": "{{ state.deep.a }}"}, {"deep": '{"a": ' * 100_000 + "1" + "}" * 100_000})


def _nested(depth: int) -> str:
    """JSON text of `depth` objects, one inside the next, the innermost `{"a": 1}`."""
    return '{"a": ' * (depth - 1) + '{"a": 1}' + "}" * (depth - 1)


def test_json_text_is_read_to_a_counted_depth_whatever_the_stack():
    """Too deep is counted, not left to where `json.loads` runs out of the C stack: a 16 MB stack parses the
    100,000 levels above that an 8 MB one cannot, and the same run read them on one machine and not on another
    (F1164). Text at the limit is read; one level more is no object, on every step that reads a field."""
    from core.orchestration.context import _fill_task_template
    from core.orchestration.steps import MissingStateRef, render_tool_arguments
    from core.templating import MAX_JSON_DEPTH

    at, over = {"deep": _nested(MAX_JSON_DEPTH)}, {"deep": _nested(MAX_JSON_DEPTH + 1)}
    assert evaluate_expression("state.deep.a", at, "fallback").value != "fallback"
    assert render_state_refs("{{ state.deep.a }}", at) != "{{ state.deep.a }}"
    assert _fill_task_template("{{ state.deep.a }}", at, {}) != "{{ state.deep.a }}"
    assert render_tool_arguments({"x": "{{ state.deep.a }}"}, at)["x"]

    assert evaluate_expression("state.deep.a", over, "fallback").value == "fallback"
    assert render_state_refs("{{ state.deep.a }}", over) == "{{ state.deep.a }}"
    assert _fill_task_template("{{ state.deep.a }}", over, {}) == "{{ state.deep.a }}"
    with pytest.raises(MissingStateRef):
        render_tool_arguments({"x": "{{ state.deep.a }}"}, over)


def test_braces_inside_a_string_are_not_depth():
    from core.templating import MAX_JSON_DEPTH

    braces = "{[" * MAX_JSON_DEPTH
    state = {"deep": json.dumps({"a": braces})}
    assert evaluate_expression("state.deep.a", state, "fallback").value == braces
    assert render_state_refs("{{ state.deep.a }}", state) == braces
