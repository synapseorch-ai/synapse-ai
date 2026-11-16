"""A condition that fails on an empty value names that value, never Python's error (F1631).

A form may be sent with a field left blank (F842), so a later If/Else reading it compared nothing with a number and
failed with "'>' not supported between instances of 'NoneType' and 'int'": true, and no help to the person reading
the run. Now a condition that fails comparing or computing with an empty value says which value was empty and what to
do. A condition that fails for any other reason says what it said before.
"""
from __future__ import annotations

import pytest

from core.orchestration.steps import _route_expr

FORM = {"submit_form": {"amount": None, "category": "", "note": "Printer paper"}}


def _said(expr: str, what: str = "The If/Else condition", state: dict | None = None) -> str:
    with pytest.raises(RuntimeError) as failed:
        _route_expr(expr, FORM if state is None else state, what)
    return str(failed.value)


class TestAnEmptyValueIsNamed:
    def test_a_comparison_with_an_empty_number(self):
        said = _said("state.submit_form.amount > 500")
        assert "state.submit_form.amount" in said and "empty" in said, said
        assert "NoneType" not in said and "not supported between" not in said, said

    def test_a_text_method_on_an_empty_choice(self):
        said = _said("state.submit_form.category.lower() == 'travel'", "The Switch expression",
                     {"submit_form": {"category": None}})
        assert "state.submit_form.category" in said and "empty" in said and "NoneType" not in said, said

    def test_an_empty_text_compared_with_a_number(self):
        said = _said("state.submit_form.category > 3")
        assert "state.submit_form.category" in said and "empty" in said, said


class TestAnyOtherFailureIsSaidAsBefore:
    def test_a_division_by_zero(self):
        said = _said("state.totals.sum / state.totals.count > 1", state={"totals": {"sum": 5, "count": 0}})
        assert "could not be evaluated" in said and "empty" not in said, said

    def test_a_filled_form_decides(self):
        value, err = _route_expr("state.submit_form.amount > 500", {"submit_form": {"amount": 820}},
                                 "The If/Else condition")
        assert value is True and not err
