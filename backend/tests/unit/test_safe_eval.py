"""The expression dialect: what it refuses, and what it must keep evaluating.

`core.expressions` replaced a bare `eval()` that the builder model could reach
through `if_condition` (S3). Two halves matter equally and are kept apart below:
the escapes that must be refused, and the expressions people and templates have
actually written, which must still return the same values. A walker is only as
safe as its allow-list, so this file is the real deliverable.
"""

import time

import pytest

from core.expressions import (
    DotNavigableState,
    check_expression,
    evaluate_expression,
    unsafe_reason,
)

STATE = {
    "llm_json": {"category": "Sports"},
    "review_notes": "  Approve  ",
    "triage": {"urgency": "High"},
    "result": {"flag": True, "status": "Done"},
    "n": 10,
    "score": 9,
    "seen": "b",
    "items": ["a", "b", "c"],
    "rows": [{"k": 1}],
    "nested": {"x": [1, 2, 3]},
    "tags": ["red", "blue"],
    "note": "hello",
    "d": {"a": 1},
    "flag": True,
    "empty": {},
}


def ev(expr, state=None, default=None):
    return evaluate_expression(expr, STATE if state is None else state, default)


# ── Refused: the two known escapes, and everything shaped like them ──────────

REFUSED = [
    # The textbook traversal, from an inline literal.
    "().__class__.__bases__[0].__subclasses__()",
    "''.__class__.__mro__[1].__subclasses__()",
    "(1).__class__.__base__",
    # The second escape, which needed no literal: a value out of shared state.
    "state.note.__class__",
    "state.note.__class__.__mro__",
    "state.items.__class__.__bases__",
    "state.x.__globals__",
    "state.note.__doc__",
    # Names that were never reachable, asserted so they stay unreachable.
    "__import__('os').system('id')",
    "open('/etc/passwd').read()",
    "getattr(state.note, 'upper')()",
    "globals()",
    "eval('1')",
    # Constructs with no production use and an unbounded cost.
    "(lambda: 1)()",
    "[x for x in state.items]",
    "{x for x in state.items}",
    "{k: v for k, v in state.d.items()}",
    "(x for x in state.items)",
    'f"{state.n}"',
    "(y := state.n)",
    "1 << 10**9",
    "1 | 2",
    "~1",
    # An attribute path a syntax tree cannot see into.
    "str(state.note).format(state)",
    "str(state.note).format_map(state)",
    "str(state.note).encode()",
    # Mutating shared state from inside a routing condition.
    "state.items.append(1)",
    "state.d.update({'x': 1})",
    "state.items.pop()",
    # Shape checks.
    "str(state.note, errors='ignore')",
    "dict(**state.d)",
    "{**state.d}",
]


@pytest.mark.parametrize("expr", REFUSED)
def test_it_is_refused_and_says_so(expr):
    result = ev(expr, default="SENTINEL")
    assert result.refused is True, f"{expr!r} was not refused"
    assert result.value == "SENTINEL"
    assert result.error and len(result.error) > 10
    # A refusal is static, so it is answerable with no state at all — which is
    # what lets the builder gate and the validator use it at authoring time.
    assert unsafe_reason(expr) == result.error
    assert check_expression(expr) == result.error


def test_a_refusal_never_raises_and_never_reaches_a_dunder():
    # The point of parsing rather than evaluating: the traversal is refused
    # before any object exists, so there is nothing to have been reached.
    for expr in REFUSED:
        evaluate_expression(expr, STATE)


# ── Refused: expressions that would cost real time ──────────────────────────

def test_a_deeply_nested_expression_is_refused():
    assert ev("[" * 40 + "]" * 40).refused is True


def test_a_very_long_expression_is_refused():
    assert ev(" + ".join(["1"] * 300)).refused is True


@pytest.mark.parametrize("expr", [
    "10**10**10", "2 ** 10000", "'x' * 10**9", "state.note * 10**9",
    "'%1000000000d' % 1",
])
def test_a_memory_bomb_is_stopped_quickly(expr):
    started = time.monotonic()
    result = ev(expr, default=False)
    assert time.monotonic() - started < 1.0, f"{expr!r} took real time"
    assert result.error is not None
    assert result.value is False


def test_list_of_state_terminates():
    # `list(state)` used to hang: with no __iter__ the old sequence protocol
    # called __getitem__(0), __getitem__(1), … and each returned None instead of
    # raising IndexError, so the iteration never ended.
    started = time.monotonic()
    result = ev("list(state)")
    assert time.monotonic() - started < 1.0
    assert sorted(result.value) == sorted(STATE)


# ── Compatibility: what is actually written in the field ────────────────────

CASES = [
    # The only expression in the live self-hosted store.
    ("state.llm_json.category", "Sports"),
    # Both shapes used by the twelve shipped cloud onboarding templates.
    ("str(state.review_notes).strip().lower() in ('approve', 'approved', 'ok', 'yes', 'lgtm', 'looks good')", True),
    ("str(state.triage.urgency).lower() == 'high'", True),
    # Every example the builder tool schemas advertise to the model.
    ("state.result.flag == True", True),
    ("state.score > 5", True),
    ("(state.n or 0) < 5", False),
    ("state.category", None),
    ("state.result.status.lower()", "done"),
    # The expressions the engine's own tests drive.
    ("state.n > 5", True),
    ("state.seen == 'b'", True),
    ("(state.n or 0) < 3", False),
    ("True", True),
    ("False", False),
    ("state.items", ["a", "b", "c"]),
    # Indexing, which works today because a list comes back unwrapped.
    ("state.items[0]", "a"),
    ("state.nested.x[1]", 2),
    ("state.items[-1]", "c"),
    ("state.items[0:2]", ["a", "b"]),
    ("state.rows[0].keys()", ["k"]),
    ("state['note']", "hello"),
    # The rest of the dialect.
    ("len(state.items)", 3),
    ("len(state)", len(STATE)),
    ("int(state.score) + 1", 10),
    ("min(1, state.n)", 1),
    ("round(1.567, 1)", 1.6),
    ("abs(-state.n)", 10),
    ("'red' in state.tags", True),
    ("state.note is None", False),
    ("state.missing is None", True),
    ("not state.empty", True),
    ("state.n if state.flag else 0", 10),
    ("1 < state.n < 100", True),
    ("state.note.startswith('he')", True),
    ("state.note.replace('l', 'L')", "heLLo"),
    ("'-'.join(state.items)", "a-b-c"),
    ("state.items.index('b')", 1),
    ("str(state.n) + '!'", "10!"),
    ("state.n % 3", 1),
    ("2 ** 8", 256),
]


@pytest.mark.parametrize("expr,expected", CASES)
def test_it_still_evaluates(expr, expected):
    result = ev(expr)
    assert result.error is None, f"{expr!r} failed: {result.error}"
    value = list(result.value) if expr.endswith(".keys()") else result.value
    assert value == expected
    assert check_expression(expr) is None


# ── Failures that are failures, not refusals ────────────────────────────────

@pytest.mark.parametrize("expr,message", [
    ("state.missing.deep.attr == 1", "attribute"),   # the engine's own test case
    ("state.a.b.c ===", "syntax"),                   # test_loop_semantics drives this
    ("state.d.keys()", "keys"),                      # `keys` is a state key first — unchanged
    ("state.d.get('a')", "get"),                     # …and so is `get`; both unchanged
    ("state.items[9]", "range"),
    ("1 / 0", "division"),
])
def test_a_broken_expression_is_not_called_unsafe(expr, message):
    result = ev(expr, default=False)
    assert result.refused is False, f"{expr!r} should read as broken, not unsafe"
    assert result.error and message in result.error.lower()
    assert result.value is False
    # The hard gate lets a typo through; only the advisory reports it — and
    # only when it is visible without values, which a runtime failure is not.
    assert unsafe_reason(expr) is None


def test_state_subscript_of_a_missing_key_is_none():
    assert ev("state[0]").value is None
    assert ev("state['nope']").value is None


def test_an_empty_expression_is_neither():
    for expr in (None, "", "   "):
        assert unsafe_reason(expr) is None
        assert check_expression(expr) is None
        assert evaluate_expression(expr, STATE, default=False).value is False


# ── The wrapper's own contract, which every expression was written against ──

def test_missing_keys_are_none_and_nested_dicts_rewrap():
    state = DotNavigableState({"a": {"b": {"c": 1}}, "s": "x", "l": [1]})
    assert state.nope is None
    assert state.a.b.c == 1
    assert state.a.nope is None
    assert isinstance(state.a, DotNavigableState)
    assert not isinstance(state.l, DotNavigableState)   # lists come back raw
    assert state.s == "x"


def test_underscore_attributes_are_refused_by_the_wrapper_too():
    state = DotNavigableState({"_if_decision_x": "true"})
    with pytest.raises(AttributeError):
        state._if_decision_x
    # …but the engine's own decision keys stay reachable by subscript, which is
    # how they were reachable before.
    assert state["_if_decision_x"] == "true"


def test_the_dunder_protocols_behave_like_the_dict():
    state = DotNavigableState({"a": 1})
    assert bool(state) is True
    assert bool(DotNavigableState({})) is False
    assert len(state) == 1
    assert "a" in state
    assert state == {"a": 1}
    assert str(state) == str({"a": 1})


# ── Nothing may carry a wrapper back out ────────────────────────────────────

def test_a_result_is_always_json_shaped():
    import json

    # `switch_expression: "state.llm_json"` is one keystroke from the real one,
    # and switch writes its value into shared state, which is json.dumps-ed at
    # every checkpoint.
    result = ev("state.llm_json")
    assert type(result.value) is dict
    assert json.dumps(result.value) == '{"category": "Sports"}'
    assert json.dumps(ev("[state.llm_json]").value) == '[{"category": "Sports"}]'
    assert json.dumps(ev("{'k': state.llm_json}").value) == '{"k": {"category": "Sports"}}'


# ── A known method name on the wrong receiver is not a way in ───────────────

def test_a_method_name_is_not_enough():
    class Sneaky:
        def get(self, *a):
            raise AssertionError("must never be called")

    result = evaluate_expression("state.obj.get('x')", {"obj": Sneaky()})
    assert result.refused is False
    assert "no method" in result.error


def test_an_overridden_builtin_method_does_not_run():
    class Shouty(str):
        def lower(self):
            raise AssertionError("must never be called")

    # Exact-type lookup: a str subclass finds no entry at all.
    assert evaluate_expression("state.s.lower()", {"s": Shouty("HI")}).error is not None


def test_the_advisory_reports_a_typo_but_a_runtime_failure_is_invisible_to_it():
    # `check_expression` runs before there is a run, so it sees a syntax error
    # and a refusal and nothing else. That is the whole reason the validator's
    # report is advice rather than a gate.
    assert check_expression("state.a.b.c ===") is not None
    assert check_expression("state.note.__class__") is not None
    assert check_expression("state.items[9]") is None
