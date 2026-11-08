"""An expression cannot spend a worker's memory or CPU — F965 (BF11 + REL2; Naveen 2026-10-24: a hotfix, now).

`core.expressions` capped an expression's size, a `**` exponent and a sequence repeat, but never the size of what an
operation produced. Measured on v2.0 (ff2c169d), each from an expression of a dozen parts or fewer:
- `(((9**64)**64)**64)**64` took 13 s of CPU and one level more never finished (REL2). Evaluation runs on the event
  loop: the self-hosted backend froze for everyone, and a cloud worker stalled for every org's jobs on it — CPU work
  cannot be cancelled, so a job's timeout never fires.
- `round(5, -1000000000)` never finished (BF11).
- `replace` and `join` turned 1,000 characters into 1,000,000, so a megabyte of state could ask for 10¹¹ (BF11).
- `split` made one object per separator: two million of them from a two-megabyte string (REL2).
Anyone who can save a workflow could write one, and so could a prompt-injected builder model (S3's path).

The class fix: an operation that can grow a value works out the size of its result before it runs and is refused past
a cap, and an evaluation has one budget for everything it produces or scans, so a name added later cannot reopen it.
A refusal fails the step and says why (F850); nothing here routes as if it were data.
"""
from __future__ import annotations

import json
import time

import pytest

from core.expressions import evaluate_expression


def _ev(expr: str, state: dict | None = None):
    started = time.monotonic()
    result = evaluate_expression(expr, state or {}, default=None)
    return result, time.monotonic() - started


def _refused_fast(expr: str, state: dict | None = None, *, words: str = "", within: float = 0.1) -> None:
    """Refused, in words, within REL2's 100 ms. A refusal that first scans or makes what it is allowed to (a split's
    2 MB prefix, a chain's first join) is given 0.25 s: about 0.05 s on an idle machine."""
    result, took = _ev(expr, state)
    assert result.refused, (expr, result)
    assert took < within, f"{expr!r} took {took:.2f}s to refuse"
    assert words in (result.error or ""), result.error


class TestIntegers:
    @pytest.mark.parametrize("expr", [
        "(((9**64)**64)**64)**64",
        "((((9**64)**64)**64)**64)**64",
        "((9**64)**64)**64",
    ])
    def test_a_power_whose_result_is_too_big_is_refused_before_it_runs(self, expr):
        _refused_fast(expr, words="digits")

    def test_a_product_whose_result_is_too_big_is_refused(self):
        big = 10 ** 4000
        _refused_fast("state.n * state.n", {"n": big}, words="digits")

    def test_round_to_a_huge_place_is_refused(self):
        _refused_fast("round(5, -1000000000)", words="round")

    def test_numbers_of_an_ordinary_size_still_work(self):
        assert _ev("2 ** 64")[0].value == 2 ** 64
        assert _ev("state.n * 3", {"n": 10 ** 100})[0].value == 3 * 10 ** 100
        assert _ev("round(1234.5678, 2)")[0].value == 1234.57


class TestText:
    def test_replace_that_would_grow_text_past_the_cap_is_refused(self):
        _refused_fast("len(state.s.replace('a', 'b' * 100000))", {"s": "a" * 1000}, words="characters")

    def test_replace_with_an_empty_old_inserts_at_every_position_and_is_sized_so(self):
        # `s.replace('', x)` inserts x len(s)+1 times, and `s.count('')` is len(s)+1 (SAI2's note).
        assert _ev("state.s.replace('', '-')", {"s": "ab"})[0].value == "-a-b-"
        _refused_fast("state.s.replace('', 'y' * 1000)", {"s": "z" * 5000}, words="characters")

    def test_replace_on_a_large_document_that_does_not_grow_it_still_works(self):
        doc = "line\r\n" * 400_000  # 2.4 MB
        result, took = _ev("len(state.doc.replace('\\r\\n', '\\n'))", {"doc": doc})
        assert result.error is None and result.value == len(doc) - 400_000
        assert took < 1.0

    def test_join_that_would_grow_text_past_the_cap_is_refused(self):
        _refused_fast("len(('x' * 1000).join(state.l))", {"l": ["y"] * 2000}, words="characters")

    def test_join_over_a_dict_joins_its_keys_and_is_sized_so(self):
        assert _ev("'-'.join(state.d)", {"d": {"a": 1, "b": 2}})[0].value == "a-b"
        keys = {f"k{i}": i for i in range(2000)}
        _refused_fast("len(('x' * 1000).join(state.d))", {"d": keys}, words="characters")

    def test_split_that_would_make_too_many_items_is_refused(self):
        _refused_fast("len(state.s.split(','))", {"s": "," * 2_000_000}, words="items")
        _refused_fast("len(state.s.rsplit(','))", {"s": "," * 2_000_000}, words="items")
        _refused_fast("len(state.s.split())", {"s": "a " * 1_500_000}, words="items", within=0.25)

    def test_split_of_an_ordinary_string_still_works(self):
        assert _ev("state.s.split(',')", {"s": "a,b,c"})[0].value == ["a", "b", "c"]
        result, took = _ev("len(state.s.split(','))", {"s": "x," * 400_000})
        assert result.value == 400_001 and took < 1.0


class TestOneBudgetPerEvaluation:
    def test_a_chain_that_keeps_producing_is_refused(self):
        big = "x" * 12_000_000  # the first join (24 MB) is allowed; the second goes past the budget
        _refused_fast("len(state.s + state.s + state.s + state.s)", {"s": big}, words="produce", within=0.25)

    def test_reading_a_large_json_field_many_times_is_counted(self):
        js = json.dumps({"a": "y" * 10_000_000})
        expr = " + ".join(["len(state.j.a)"] * 8)
        result, _ = _ev(expr, {"j": js})
        assert result.refused and "produce" in result.error


class TestPlainData:
    def test_a_dict_s_keys_values_and_items_come_back_as_lists(self):
        """A view carries `.mapping`, its dict: plain data only (REL2's review)."""
        assert _ev("{'a': 1, 'b': 2}.keys()")[0].value == ["a", "b"]
        assert _ev("{'a': 1, 'b': 2}.values()")[0].value == [1, 2]
        assert _ev("{'a': 1, 'b': 2}.items()")[0].value == [("a", 1), ("b", 2)]

    def test_len_of_a_large_value_is_not_counted_as_reading_it(self):
        assert _ev("len(state.doc) > 0", {"doc": "x" * 60_000_000})[0].value is True


class TestTheOldBombsStillStop:
    @pytest.mark.parametrize("expr", ["10**10**10", "2 ** 10000", "'x' * 10**9", "'%1000000000d' % 1"])
    def test_each_refuses_fast(self, expr):
        result, took = _ev(expr)
        assert result.error is not None and result.value is None
        assert took < 0.1
