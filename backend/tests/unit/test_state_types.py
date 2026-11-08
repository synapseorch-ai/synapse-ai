"""One vocabulary for `state_schema` types, and what a value has to be to match it.

The AI builder emits `str/int/float/bool/list/dict`; the editor's dropdown only
offered `string/number/boolean/list/dict`. Radix renders a *blank* trigger — not
its placeholder — for a value matching no option, so every builder-written `str`
key showed an empty type box while a `dict` key looked fine. This module is the
single table both sides now read.
"""
import pytest

from core.state_types import (
    CANONICAL_STATE_TYPES,
    matches_declared_type,
    normalize_state_type,
)


class TestNormalize:
    def test_a_canonical_type_is_returned_unchanged(self):
        for t in CANONICAL_STATE_TYPES:
            assert normalize_state_type(t) == t

    @pytest.mark.parametrize("raw,expected", [
        ("string", "str"), ("text", "str"),
        ("integer", "int"),
        ("double", "float"), ("decimal", "float"),
        ("boolean", "bool"),
        ("array", "list"),
        ("object", "dict"), ("json", "dict"), ("map", "dict"),
    ])
    def test_the_editor_and_json_spellings_are_aliases(self, raw, expected):
        assert normalize_state_type(raw) == expected

    def test_number_widens_to_float(self):
        # The old dropdown's `number` covered both, and its input accepted 7.5.
        # Reading it as `int` would start rejecting values the editor used to
        # take; widening never does.
        assert normalize_state_type("number") == "float"

    @pytest.mark.parametrize("raw", ["STR", "String", " list ", "\tDict\n", "Boolean"])
    def test_case_and_surrounding_space_do_not_matter(self, raw):
        assert normalize_state_type(raw) in CANONICAL_STATE_TYPES

    @pytest.mark.parametrize("raw", [None, "", "   ", "frobnicate", "int64", 7, [], {}])
    def test_anything_unrecognised_is_none_never_a_guess(self, raw):
        assert normalize_state_type(raw) is None

    def test_normalising_twice_changes_nothing(self):
        for raw in ["string", "number", "array", "JSON", "bool"]:
            once = normalize_state_type(raw)
            assert normalize_state_type(once) == once


class TestMatches:
    @pytest.mark.parametrize("declared,value", [
        ("str", "hello"), ("str", ""),
        ("int", 7), ("int", 0), ("int", -3),
        ("float", 7.5), ("float", 7),  # an int is a perfectly good float
        ("bool", True), ("bool", False),
        ("list", []), ("list", [1, 2]),
        ("dict", {}), ("dict", {"a": 1}),
    ])
    def test_a_value_of_the_declared_type_matches(self, declared, value):
        assert matches_declared_type(declared, value) is True

    @pytest.mark.parametrize("declared,value", [
        ("str", 7), ("str", []), ("str", None),
        ("int", "7"), ("int", 7.5), ("int", None),
        ("float", "7.5"), ("float", None),
        ("bool", "true"), ("bool", 1), ("bool", 0),
        ("list", "[]"), ("list", {}), ("list", None),
        ("dict", "{}"), ("dict", []), ("dict", None),
    ])
    def test_a_value_of_another_type_does_not(self, declared, value):
        assert matches_declared_type(declared, value) is False

    def test_a_bool_is_not_an_int_even_though_python_says_it_is(self):
        # `isinstance(True, int)` is True, which would quietly let "true" satisfy
        # an int-declared key and vice versa.
        assert matches_declared_type("int", True) is False
        assert matches_declared_type("float", False) is False
        assert matches_declared_type("bool", 1) is False

    def test_an_unrecognised_declared_type_contradicts_nothing(self):
        # Nothing can be said about a type we do not know, so nothing is claimed.
        assert matches_declared_type("frobnicate", "anything") is True
        assert matches_declared_type(None, 7) is True
