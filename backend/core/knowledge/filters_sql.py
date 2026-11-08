# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""The metadata filter as SQL — the same answers as `filters.evaluate`, in the retrieval query itself.

A filter runs **inside** the query that ranks the chunks, never after it, so a
top-k is never filled with rows the filter would have thrown away.

**The one documented dialect exception.** Everywhere else above
`core/store/engine.py` the engine has no `if sqlite:` branch. JSON is where the
two dialects genuinely differ, and SQLAlchemy's own portable accessors are not
enough: `JSON.as_float()` renders a bare `CAST(meta ->> 'k' AS FLOAT)` on
Postgres, which fails the **whole** query the moment one row holds a string
there (measured 2026-09-30), and a list-valued field would be compared as one
string. So the three JSON reads below are SQLAlchemy constructs with one
`@compiles` per dialect — chosen when the statement compiles, never by a
branch in the code that builds it:

* `JsonValue(meta, key, cls)` — the value if it is of class `cls`, else NULL.
* `JsonHasKey(meta, key)` — present and not JSON null.
* `JsonListContains(meta, key, value)` — a list holding the string `value`.

Every leaf is wrapped in `coalesce(…, false)`, so NULL never leaks into NOT
and the logic stays two-valued, as `evaluate` is. Strings order by code point:
SQLite's default BINARY collation does, and Postgres is told `COLLATE "C"`.

The metadata key is validated against `filters.FIELD_RE` before it is spliced
into the SQL text, and again here; values are always bound parameters.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Boolean, Float, String, and_, false, func, literal, not_, or_
from sqlalchemy.exc import CompileError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement

from core.knowledge.filters import (
    BUILTINS, FIELD_RE, And, Cmp, Exists, FilterNode, In_, Not, _class_of, parse_instant,
)

_TYPES = {"number": Float, "string": String, "bool": Boolean}


def _checked(key: str) -> str:
    if not FIELD_RE.match(key):
        raise CompileError(f"not a metadata key: {key!r}")
    return key


class JsonValue(ColumnElement):
    """`meta`'s `key` if its JSON type is `cls` ("number", "string" or "bool"), else NULL."""

    inherit_cache = False

    def __init__(self, column: ColumnElement, key: str, cls: str):
        self.column = column
        self.key = _checked(key)
        self.cls = cls
        self.type = _TYPES[cls]()


class JsonHasKey(ColumnElement):
    """`meta` has `key`, and its value is not JSON null."""

    inherit_cache = False
    type = Boolean()

    def __init__(self, column: ColumnElement, key: str):
        self.column = column
        self.key = _checked(key)


class JsonListContains(ColumnElement):
    """`meta`'s `key` is a JSON array holding the string `value`."""

    inherit_cache = False
    type = Boolean()

    def __init__(self, column: ColumnElement, key: str, value: str):
        self.column = column
        self.key = _checked(key)
        self.value = literal(value, String())


class CodepointOrder(ColumnElement):
    """A string expression compared by code point: `COLLATE "C"` on Postgres, as-is on SQLite."""

    inherit_cache = False

    def __init__(self, expr: ColumnElement):
        self.expr = expr
        self.type = String()


def _unsupported(element, compiler, **kw):  # pragma: no cover - only a third dialect reaches it
    raise CompileError(f"{type(element).__name__} has no SQL for {compiler.dialect.name}")


for _construct in (JsonValue, JsonHasKey, JsonListContains, CodepointOrder):
    compiles(_construct)(_unsupported)


# ── SQLite ─────────────────────────────────────────────────────────────────────

def _sqlite_path(key: str) -> str:
    return "'$.\"" + key + "\"'"


@compiles(JsonValue, "sqlite")
def _json_value_sqlite(el: JsonValue, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    path = _sqlite_path(el.key)
    if el.cls == "number":
        return f"(CASE WHEN json_type({col}, {path}) IN ('integer', 'real') THEN json_extract({col}, {path}) END)"
    if el.cls == "string":
        return f"(CASE WHEN json_type({col}, {path}) = 'text' THEN json_extract({col}, {path}) END)"
    return f"(CASE json_type({col}, {path}) WHEN 'true' THEN 1 WHEN 'false' THEN 0 END)"


@compiles(JsonHasKey, "sqlite")
def _json_has_key_sqlite(el: JsonHasKey, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    path = _sqlite_path(el.key)
    return f"(json_type({col}, {path}) IS NOT NULL AND json_type({col}, {path}) <> 'null')"


@compiles(JsonListContains, "sqlite")
def _json_list_contains_sqlite(el: JsonListContains, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    path = _sqlite_path(el.key)
    value = compiler.process(el.value, **kw)
    return (f"(json_type({col}, {path}) = 'array' AND EXISTS (SELECT 1 FROM json_each({col}, {path}) AS kje "
            f"WHERE kje.type = 'text' AND kje.value = {value}))")


@compiles(CodepointOrder, "sqlite")
def _codepoint_sqlite(el: CodepointOrder, compiler, **kw) -> str:
    return compiler.process(el.expr, **kw)


# ── Postgres ───────────────────────────────────────────────────────────────────

@compiles(JsonValue, "postgresql")
def _json_value_pg(el: JsonValue, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    key = f"'{el.key}'"
    if el.cls == "number":
        return f"(CASE WHEN jsonb_typeof({col} -> {key}) = 'number' THEN ({col} ->> {key})::float8 END)"
    if el.cls == "string":
        return f"((CASE WHEN jsonb_typeof({col} -> {key}) = 'string' THEN ({col} ->> {key}) END) COLLATE \"C\")"
    return f"(CASE WHEN jsonb_typeof({col} -> {key}) = 'boolean' THEN ({col} -> {key})::boolean END)"


@compiles(JsonHasKey, "postgresql")
def _json_has_key_pg(el: JsonHasKey, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    key = f"'{el.key}'"
    return f"(jsonb_typeof({col} -> {key}) IS NOT NULL AND jsonb_typeof({col} -> {key}) <> 'null')"


@compiles(JsonListContains, "postgresql")
def _json_list_contains_pg(el: JsonListContains, compiler, **kw) -> str:
    col = compiler.process(el.column, **kw)
    key = f"'{el.key}'"
    value = compiler.process(el.value, **kw)
    return (f"(jsonb_typeof({col} -> {key}) = 'array' "
            f"AND ({col} -> {key}) @> jsonb_build_array(CAST({value} AS text)))")


@compiles(CodepointOrder, "postgresql")
def _codepoint_pg(el: CodepointOrder, compiler, **kw) -> str:
    return f"(({compiler.process(el.expr, **kw)}) COLLATE \"C\")"


# ── compiling a filter ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FilterColumns:
    """The columns a filter reads: the metadata JSON and one column per built-in."""

    meta: ColumnElement
    builtins: Mapping[str, ColumnElement]

    @classmethod
    def for_table(cls, table: Any) -> FilterColumns:
        """For a table (or ORM class's `__table__`) with a `meta` column and the built-in columns."""
        columns = table.c
        return cls(meta=columns.meta, builtins={name: columns[name] for name in BUILTINS})


def _true(expr: ColumnElement) -> ColumnElement:
    return func.coalesce(expr, false(), type_=Boolean())


def _compare(left: ColumnElement, op: str, right: Any) -> ColumnElement:
    if op == "$eq":
        return left == right
    if op == "$gt":
        return left > right
    if op == "$gte":
        return left >= right
    if op == "$lt":
        return left < right
    return left <= right


def _builtin_eq_or_order(cols: FilterColumns, field: str, op: str, value: Any) -> ColumnElement:
    column = cols.builtins[field]
    kind = BUILTINS[field]
    if kind == "datetime":
        return _true(_compare(column, op, parse_instant(value)))
    if op == "$eq":
        return _true(column == value)
    return _true(_compare(CodepointOrder(column), op, literal(value, String())))


def _meta_eq(cols: FilterColumns, field: str, value: Any) -> ColumnElement:
    cls = _class_of(value)
    scalar = _true(JsonValue(cols.meta, field, cls) == literal(value, _TYPES[cls]()))
    if cls == "string":
        return or_(scalar, _true(JsonListContains(cols.meta, field, value)))
    return scalar


def _eq(cols: FilterColumns, field: str, value: Any) -> ColumnElement:
    if field in BUILTINS:
        return _builtin_eq_or_order(cols, field, "$eq", value)
    return _meta_eq(cols, field, value)


def compile_sql(node: FilterNode | None, cols: FilterColumns) -> ColumnElement:
    """`node` as a boolean SQL expression over `cols`. No filter compiles to TRUE."""
    if node is None:
        return _true(literal(True, Boolean()))
    if isinstance(node, Cmp):
        if node.op == "$eq":
            return _eq(cols, node.field, node.value)
        if node.op == "$ne":
            return not_(_eq(cols, node.field, node.value))
        if node.field in BUILTINS:
            return _builtin_eq_or_order(cols, node.field, node.op, node.value)
        cls = _class_of(node.value)
        return _true(_compare(JsonValue(cols.meta, node.field, cls), node.op, literal(node.value, _TYPES[cls]())))
    if isinstance(node, In_):
        hit = or_(*[_eq(cols, node.field, v) for v in node.values])
        return hit if node.op == "$in" else not_(hit)
    if isinstance(node, Exists):
        if node.field in BUILTINS:
            present = _true(cols.builtins[node.field].isnot(None))
        else:
            present = _true(JsonHasKey(cols.meta, node.field))
        return present if node.value else not_(present)
    if isinstance(node, Not):
        return not_(compile_sql(node.child, cols))
    parts = [compile_sql(c, cols) for c in node.children]
    return and_(*parts) if isinstance(node, And) else or_(*parts)


def path_prefix_sql(path_column: ColumnElement, prefix: str) -> ColumnElement:
    """`path` is `prefix` or under it, component-wise: "contracts" never matches "contracts2/…"."""
    clean = prefix.strip("/")
    if not clean:
        return _true(literal(True, Boolean()))
    return or_(path_column == clean, func.substr(path_column, 1, len(clean) + 1) == clean + "/")


__all__ = [
    "CodepointOrder", "FilterColumns", "JsonHasKey", "JsonListContains", "JsonValue",
    "compile_sql", "path_prefix_sql",
]
