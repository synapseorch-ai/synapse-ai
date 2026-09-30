"""Filter cases over `corpus.rows()`, each with the rows it must select, worked out by hand.

The same table is run through `filters.evaluate`, through `filters_sql` on
SQLite, and through `filters_sql` on Postgres; all three must select exactly
`expected`. `ERROR_CASES` must each fail to parse with the given code and
path. `random_filters` feeds the SQL-versus-Python agreement test.

Row ids are the corpus ordinals (see `corpus._ROWS`).
"""
from __future__ import annotations

import random
from typing import Any

ALL = frozenset(range(16))

#: (name, filter, the row ids it selects)
CASES: list[tuple[str, Any, frozenset[int]]] = [
    ("eq_string", {"doc_type": "contract"}, frozenset({0, 1, 2, 3, 4})),
    ("eq_is_case_sensitive", {"doc_type": "report"}, frozenset({7, 8})),
    ("eq_number_not_string", {"year": 2024}, frozenset({0, 1, 2})),
    ("eq_string_not_number", {"year": "2024"}, frozenset({5})),
    ("eq_int_matches_float", {"year": 2025}, frozenset({6, 7, 8, 9})),
    ("ne_matches_missing", {"region": {"$ne": "EU"}}, ALL - {0, 1, 2}),
    ("gt_skips_strings", {"year": {"$gt": 2024}}, frozenset({6, 7, 8, 9})),
    ("lte", {"year": {"$lte": 2023}}, frozenset({3, 4})),
    ("range_pair_is_and", {"amount": {"$gte": 100, "$lt": 200000}}, frozenset({0, 6})),
    ("string_range_by_codepoint", {"region": {"$gte": "EU"}}, frozenset({0, 1, 2, 5, 6, 12})),
    ("eq_on_list_contains", {"tags": "legal"}, frozenset({0, 1, 3})),
    ("in_on_list", {"tags": {"$in": ["finance", "support"]}}, frozenset({5, 7, 10})),
    ("nin_on_list_matches_missing", {"tags": {"$nin": ["legal"]}}, ALL - {0, 1, 3}),
    ("in_on_scalar", {"doc_type": {"$in": ["invoice", "ticket"]}}, frozenset({5, 6, 10, 11})),
    ("exists_true_counts_empty_list", {"tags": {"$exists": True}}, frozenset({0, 1, 3, 5, 7, 8, 9, 10, 12})),
    ("exists_false", {"region": {"$exists": False}}, frozenset({7, 8, 9, 10, 11, 13, 14, 15})),
    ("bool_true", {"confidential": True}, frozenset({0, 1})),
    ("bool_false", {"confidential": False}, frozenset({7})),
    ("bool_ne", {"confidential": {"$ne": True}}, ALL - {0, 1}),
    ("or", {"$or": [{"doc_type": "ticket"}, {"region": "CH"}]}, frozenset({3, 4, 10, 11})),
    ("not", {"$not": {"doc_type": "contract"}}, ALL - {0, 1, 2, 3, 4}),
    ("and_with_nested_or", {"$and": [{"doc_type": "contract"}, {"$or": [{"region": "CH"}, {"tags": "urgent"}]}]},
     frozenset({0, 3, 4})),
    ("builtin_path", {"path": "contracts/acme.pdf"}, frozenset({0, 1, 2})),
    ("builtin_source_in", {"source": {"$in": ["web", "record"]}}, frozenset({10, 11, 12, 13})),
    ("builtin_doc_date_range", {"doc_date": {"$gte": "2024-01-01", "$lt": "2025-01-01"}}, frozenset({0, 1, 2, 5, 6})),
    ("builtin_doc_date_ne_matches_missing", {"doc_date": {"$ne": "2024-03-01"}}, ALL - {0, 1, 2}),
    ("builtin_indexed_at_date", {"indexed_at": {"$gte": "2026-09-07"}}, frozenset({12, 13, 14, 15})),
    ("builtin_indexed_at_zulu", {"indexed_at": {"$lt": "2026-09-02T10:00:00Z"}}, frozenset({0, 1, 2})),
    ("builtin_title_missing", {"title": {"$exists": False}}, frozenset({9, 14, 15})),
    ("builtin_unicode", {"title": "Zürich Vertrag"}, frozenset({3, 4})),
    ("builtin_mime", {"mime": "text/markdown"}, frozenset({7, 8, 12, 13})),
    ("int_range", {"priority": {"$gte": 1}}, frozenset({10, 11})),
    ("missing_key_range", {"nonexistent": {"$gt": 0}}, frozenset()),
    ("missing_key_ne", {"nonexistent": {"$ne": 0}}, ALL),
    ("in_no_match", {"tags": {"$in": ["x"]}}, frozenset()),
    ("list_eq_number_never", {"tags": 1}, frozenset()),
    ("or_of_ranges", {"$or": [{"year": {"$lt": 2024}}, {"amount": {"$gt": 1000}}]}, frozenset({0, 3, 4})),
    ("not_exists", {"$not": {"amount": {"$exists": True}}}, ALL - {0, 6}),
    ("nin_scalar_matches_missing", {"region": {"$nin": ["EU", "US"]}},
     frozenset({3, 4, 7, 8, 9, 10, 11, 13, 14, 15})),
    ("metadata_language", {"language": "de"}, frozenset({3, 4})),
    ("range_never_on_list", {"tags": {"$gt": "a"}}, frozenset()),
    ("string_range_only_strings", {"year": {"$gte": "2000"}}, frozenset({5})),
]

_DEEP = {"$and": [{"$and": [{"$and": [{"$and": [{"$and": [{"a": 1}]}]}]}]}]}

#: (name, filter, error code, JSON pointer)
ERROR_CASES: list[tuple[str, Any, str, str]] = [
    ("not_an_object", ["a"], "not_an_object", ""),
    ("bare_list", {"a": []}, "bad_value", "/a"),
    ("empty_and", {"$and": []}, "empty_list", "/$and"),
    ("unknown_top_operator", {"$foo": 1}, "unknown_operator", "/$foo"),
    ("unknown_field_operator", {"a": {"$foo": 1}}, "unknown_operator", "/a/$foo"),
    ("nested_object", {"a": {"b": 1}}, "unknown_operator", "/a/b"),
    ("bad_field_name", {"bad key": 1}, "bad_field", "/bad key"),
    ("null_value", {"a": None}, "bad_value", "/a"),
    ("empty_in", {"a": {"$in": []}}, "empty_list", "/a/$in"),
    ("too_many_values", {"a": {"$in": list(range(101))}}, "too_many_values", "/a/$in"),
    ("range_on_bool", {"a": {"$gt": True}}, "type_mismatch", "/a/$gt"),
    ("source_value", {"source": "email"}, "bad_value", "/source"),
    ("source_range", {"source": {"$gt": "a"}}, "type_mismatch", "/source/$gt"),
    ("doc_date_format", {"doc_date": "2024/01/01"}, "bad_value", "/doc_date"),
    ("indexed_at_format", {"indexed_at": "yesterday"}, "bad_value", "/indexed_at"),
    ("path_number", {"path": 3}, "type_mismatch", "/path"),
    ("exists_not_bool", {"a": {"$exists": "yes"}}, "bad_value", "/a/$exists"),
    ("nan", {"a": float("nan")}, "bad_value", "/a"),
    ("bad_json", "{not json", "bad_json", ""),
    ("too_deep", _DEEP, "too_deep", "/$and/0/$and/0/$and/0/$and/0"),
    ("too_many_conditions", {f"k{i}": i for i in range(51)}, "too_many_conditions", "/k50"),
]

_FIELDS: dict[str, list[Any]] = {
    "doc_type": ["contract", "invoice", "report", "Report", "ticket", "web", "none"],
    "year": [2023, 2024, 2025, 2025.0, "2024", 1999],
    "tags": ["legal", "finance", "Finance", "urgent", "support", "x"],
    "region": ["EU", "CH", "US", "AA", "zz"],
    "confidential": [True, False],
    "amount": [0, 900, 125000.5, 1e9],
    "priority": [0, 1, 2],
    "language": ["de", "en"],
    "nonexistent": [0, "a"],
    "path": ["contracts/acme.pdf", "reports/q3.md", "misc", "_web/example.com/pricing.md"],
    "source": ["upload", "vault", "web", "record"],
    "doc_date": ["2023-11-15", "2024-03-01", "2025-01-01"],
    "indexed_at": ["2026-09-03", "2026-09-05T10:00:00Z", "2026-09-06T12:30:00+02:00"],
    "title": ["Acme MSA", "Zürich Vertrag", "Pricing"],
}


def _leaf(rng: random.Random) -> dict:
    field = rng.choice(sorted(_FIELDS))
    values = _FIELDS[field]
    op = rng.choice(["$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists", "bare"])
    if op == "$exists":
        return {field: {"$exists": rng.random() < 0.5}}
    if op in ("$in", "$nin"):
        return {field: {op: rng.sample(values, k=rng.randint(1, min(3, len(values))))}}
    value = rng.choice(values)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if isinstance(value, bool) or field == "source":
            return {field: value}
        return {field: {op: value}}
    return {field: value} if op == "bare" else {field: {op: value}}


def _node(rng: random.Random, depth: int) -> dict:
    roll = rng.random()
    if depth >= 3 or roll < 0.5:
        return _leaf(rng)
    if roll < 0.7:
        return {"$not": _node(rng, depth + 1)}
    key = "$and" if roll < 0.85 else "$or"
    return {key: [_node(rng, depth + 1) for _ in range(rng.randint(1, 3))]}


def random_filters(seed: int = 153, n: int = 300) -> list[dict]:
    """`n` valid random filters over the corpus's fields, deterministic for `seed`."""
    from core.knowledge.filters import parse_filter

    rng = random.Random(seed)
    out: list[dict] = []
    while len(out) < n:
        candidate = _node(rng, 1)
        try:
            parse_filter(candidate)
        except ValueError:
            continue
        out.append(candidate)
    return out
