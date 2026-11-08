"""An expression offers more of what people write, and says what it offers — F957 (BF11; Naveen 2026-10-23, REL2's
review 10-24).

QA1b's Switch used `.splitlines()[-1]` (F850): natural, and not in the dialect, so save refused it. The dialect gains
read-only names, each one reviewed by REL2 before code: it returns plain data only, has no side effects, and its
work and size are bounded (F965's caps hold for every new name). What stays out stays out, each with a refusal:
`format` reaches attributes, a mutator changes the run's state, and a padding method's size is the caller's to pick.
`vocabulary()` lists what is offered from the same tables, so the editor and the builder never say otherwise.
"""
from __future__ import annotations

import time

import pytest

from core import expressions
from core.expressions import evaluate_expression


def _ev(expr: str, state: dict | None = None):
    started = time.monotonic()
    result = evaluate_expression(expr, state or {}, default=None)
    return result, time.monotonic() - started


def _value(expr: str, state: dict | None = None):
    result, _ = _ev(expr, state)
    assert result.error is None, (expr, result)
    return result.value


PLAIN = (str, int, float, bool, type(None))


def _plain(value) -> bool:
    if isinstance(value, PLAIN):
        return True
    if isinstance(value, (list, tuple)):
        return type(value) in (list, tuple) and all(_plain(v) for v in value)
    if isinstance(value, dict):
        return type(value) is dict and all(_plain(k) and _plain(v) for k, v in value.items())
    return False


NEW = [
    ("state.s.splitlines()", {"s": "one\ntwo\r\nthree"}, ["one", "two", "three"]),
    ("state.s.splitlines()[-1]", {"s": "Sort it\nurgent"}, "urgent"),
    ("state.s.partition(':')", {"s": "key: value"}, ("key", ":", " value")),
    ("state.s.rpartition('/')", {"s": "a/b/c"}, ("a/b", "/", "c")),
    ("state.s.isupper()", {"s": "OK"}, True),
    ("state.s.islower()", {"s": "ok"}, True),
    ("state.s.isspace()", {"s": "  "}, True),
    ("state.s.isalnum()", {"s": "a1"}, True),
    ("state.s.index('b')", {"s": "abc"}, 1),
    ("state.s.rfind('a')", {"s": "abca"}, 3),
    ("state.s.rindex('a')", {"s": "abca"}, 3),
    ("sorted(state.l)", {"l": [3, 1, 2]}, [1, 2, 3]),
    ("sum(state.l)", {"l": [1, 2.5, 3]}, 6.5),
    ("any(state.l)", {"l": [0, "", "x"]}, True),
    ("all(state.l)", {"l": [1, "x"]}, True),
    ("reversed(state.l)", {"l": [1, 2, 3]}, [3, 2, 1]),
    ("enumerate(state.l)", {"l": ["a", "b"]}, [(0, "a"), (1, "b")]),
]


class TestTheNewNames:
    @pytest.mark.parametrize(("expr", "state", "expected"), NEW)
    def test_each_works_and_returns_plain_data(self, expr, state, expected):
        value = _value(expr, state)
        assert value == expected
        assert _plain(value), f"{expr!r} returned {type(value).__name__}"

    def test_the_qa1b_switch_now_saves_and_runs(self):
        expr = "'urgent' if 'urgent' in str(state.c).lower().splitlines()[-1] else 'routine'"
        assert expressions.unsafe_reason(expr) is None
        assert _value(expr, {"c": "Sorting\nURGENT"}) == "urgent"


class TestWhatStaysOut:
    @pytest.mark.parametrize("expr", [
        "'{0.__class__}'.format(state.s)", "'{x}'.format_map(state.d)",
        "state.s.center(10)", "state.s.ljust(10)", "state.s.rjust(10)", "state.s.zfill(10)",
        "state.s.expandtabs(8)", "range(10)",
        "state.l.append(1)", "state.l.sort()", "state.l.pop()", "state.d.update({})", "state.d.setdefault('a', 1)",
    ])
    def test_is_refused_before_it_runs(self, expr):
        assert expressions.unsafe_reason(expr) is not None, expr

    def test_sorted_takes_no_key_and_sum_no_start(self):
        assert expressions.unsafe_reason("sorted(state.l, key=len)") is not None
        result, _ = _ev("sum(state.l, 10)", {"l": [1]})
        assert result.refused and "sum" in result.error

    def test_sum_adds_numbers_only(self):
        # `sum(lists, [])` is quadratic; a list of text is not a number either.
        for items in ([["a"], ["b"]], ["a", "b"]):
            result, _ = _ev("sum(state.l)", {"l": items})
            assert result.refused and "numbers" in result.error

    @pytest.mark.parametrize("expr", [
        "state.s.partition('a').__class__", "state.s.splitlines().__class__.__bases__",
        "sorted(state.l).__iter__", "enumerate(state.l)[0].__class__",
    ])
    def test_no_new_name_reaches_an_attribute(self, expr):
        assert expressions.unsafe_reason(expr) is not None, expr


class TestTheCapsHoldForEveryNewName:
    def test_splitlines_that_would_make_too_many_items_is_refused_fast(self):
        result, took = _ev("len(state.s.splitlines())", {"s": "\n" * 2_000_000})
        assert result.refused and "items" in result.error
        assert took < 0.25

    def test_sorted_of_too_many_items_is_refused_fast(self):
        result, took = _ev("sorted(state.l)", {"l": list(range(1_500_000))})
        assert result.refused and "items" in result.error
        assert took < 0.1

    @pytest.mark.parametrize(("expr", "state"), [
        ("len(sorted(state.l))", {"l": list(range(1_000_000, 0, -1))}),
        ("len(state.s.splitlines())", {"s": "x\n" * 999_999}),
        ("len(enumerate(state.l))", {"l": list(range(1_000_000))}),
        ("sum(state.l)", {"l": list(range(1_000_000))}),
    ])
    def test_a_walker_at_its_cap_finishes_in_about_a_second(self, expr, state):
        result, took = _ev(expr, state)
        assert result.error is None
        assert took < 2.0, f"{expr!r} took {took:.2f}s"


class TestTheVocabulary:
    def test_lists_what_is_offered_from_the_same_tables(self):
        vocab = expressions.vocabulary()
        assert {"len", "str", "sorted", "sum", "any", "all", "reversed", "enumerate"} <= set(vocab["functions"])
        assert {"lower", "split", "splitlines", "partition"} <= set(vocab["methods"]["str"])
        assert set(vocab["methods"]["dict"]) == {"get", "keys", "values", "items"}
        offered = set(vocab["functions"]) | {m for ms in vocab["methods"].values() for m in ms}
        assert not offered & {"format", "format_map", "center", "zfill", "range", "append", "update", "setdefault"}

    def test_its_sentence_names_them(self):
        sentence = expressions.vocabulary_sentence()
        assert "splitlines" in sentence and "sorted" in sentence and "format" not in sentence


class TestTheRefusalSaysWhatIsOffered:
    def test_save_and_the_builder_name_the_same_list(self):
        from core.builder_tools import _refuse_unsafe_expression
        from core.orchestration.save_rules import refusal

        steps = [{"id": "s", "type": "if_else", "if_condition": "state.s.zfill(9)"}]
        for said in (refusal({"id": "o", "steps": steps}, {}), _refuse_unsafe_expression(steps)["error"]):
            assert expressions.vocabulary_sentence() in said
            assert "common string methods" not in said


def test_the_editor_s_list_is_the_engine_s():
    """`frontend/…/expressionVocabulary.ts` holds `vocabulary()` as JSON for the editor's "What you can use"; it is
    never a second opinion (F957). Change the engine's tables, then paste its new `vocabulary()` there."""
    import json
    import re
    from pathlib import Path

    ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/orchestration/expressionVocabulary.ts")
    written = re.search(r"EXPRESSION_VOCABULARY = (\{.*?\}) as const;", ts.read_text(encoding="utf-8"), re.DOTALL)
    assert written, "EXPRESSION_VOCABULARY is not written as JSON"
    assert json.loads(written.group(1)) == expressions.vocabulary()
