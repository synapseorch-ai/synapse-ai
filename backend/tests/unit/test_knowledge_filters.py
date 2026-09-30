"""The Knowledge metadata filter language: parsing, validation, canonical form, evaluation (D153 #18)."""
from __future__ import annotations

import pytest

from core.knowledge import filters as F
from core.knowledge.errors import FilterError
from core.knowledge.testing import corpus
from core.knowledge.testing.filter_cases import CASES, ERROR_CASES


@pytest.mark.parametrize("name,flt,expected", CASES, ids=[c[0] for c in CASES])
def test_evaluate_selects_the_hand_checked_rows(name, flt, expected):
    node = F.parse_filter(flt)
    got = frozenset(row["_id"] for row in corpus.rows() if F.evaluate(node, row))
    assert got == expected


@pytest.mark.parametrize("name,flt,code,path", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
def test_a_bad_filter_names_its_code_and_where(name, flt, code, path):
    with pytest.raises(FilterError) as caught:
        F.parse_filter(flt)
    assert caught.value.filter_code == code
    assert caught.value.path == path
    assert code in F.FILTER_ERROR_CODES


def test_no_filter_is_none():
    for empty in (None, "", "  ", {}):
        assert F.parse_filter(empty) is None
    assert F.evaluate(None, {"anything": 1}) is True


def test_the_agent_tool_passes_a_json_string():
    assert F.parse_filter('{"doc_type": "contract"}') == F.parse_filter({"doc_type": "contract"})


def test_canonical_form_ignores_order():
    a = F.parse_filter({"b": 1, "a": {"$in": ["x", "y"]}, "$or": [{"c": 1}, {"d": True}]})
    b = F.parse_filter({"$or": [{"d": True}, {"c": 1}], "a": {"$in": ["x", "y"]}, "b": 1})
    assert F.to_canonical(a) == F.to_canonical(b)
    assert F.to_canonical(a) == {"$and": [
        {"$or": [{"c": {"$eq": 1}}, {"d": {"$eq": True}}]},
        {"a": {"$in": ["x", "y"]}},
        {"b": {"$eq": 1}},
    ]}


def test_canonical_form_round_trips():
    for _, flt, _ in CASES:
        node = F.parse_filter(flt)
        again = F.parse_filter(F.to_canonical(node))
        assert F.to_canonical(again) == F.to_canonical(node)


def test_indexed_at_is_normalized_to_utc():
    node = F.parse_filter({"indexed_at": {"$gte": "2026-09-06T12:30:00+02:00"}})
    assert node.value == "2026-09-06T10:30:00+00:00"
    assert F.parse_filter({"indexed_at": "2026-09-06"}).value == "2026-09-06T00:00:00+00:00"


def test_a_bool_is_never_a_number():
    assert F.evaluate(F.parse_filter({"x": 1}), {"x": True}) is False
    assert F.evaluate(F.parse_filter({"x": True}), {"x": 1}) is False


def test_limits_are_the_documented_ones():
    assert (F.LIMITS.max_depth, F.LIMITS.max_conditions, F.LIMITS.max_in_values) == (4, 50, 100)
    four_deep = {"$and": [{"$and": [{"$and": [{"a": 1}]}]}]}
    assert F.parse_filter(four_deep) is not None
    assert F.parse_filter({f"k{i}": i for i in range(50)}) is not None


def test_field_names():
    assert F.field_names(F.parse_filter({"a": 1, "$or": [{"b": 2}, {"$not": {"path": "x"}}]})) == {"a", "b", "path"}


class TestMetadata:
    def test_accepts_scalars_and_string_lists(self):
        meta = {"a": "x", "b": 1, "c": 2.5, "d": True, "e": ["p", "q"]}
        assert F.check_metadata(meta) == meta

    @pytest.mark.parametrize("key", sorted(F.RESERVED_METADATA))
    def test_refuses_a_builtin_name(self, key):
        with pytest.raises(FilterError) as caught:
            F.check_metadata({key: "x"})
        assert caught.value.filter_code == "bad_field"

    @pytest.mark.parametrize("value", [None, {"nested": 1}, [1, 2], float("inf")])
    def test_refuses_what_a_filter_could_not_read(self, value):
        with pytest.raises(FilterError):
            F.check_metadata({"k": value})

    def test_title_and_doc_date_may_be_given(self):
        assert F.check_metadata({"title": "T", "doc_date": "2024-01-01"}) == {"title": "T", "doc_date": "2024-01-01"}


def test_path_under_is_component_wise():
    assert F.path_under("contracts/a.pdf", "contracts")
    assert F.path_under("contracts", "contracts/")
    assert not F.path_under("contracts2/a.pdf", "contracts")
    assert F.path_under("anything", "")
