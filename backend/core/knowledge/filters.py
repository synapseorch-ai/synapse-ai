# FROZEN CONTRACT (KB0, D153): change it through a contract-change request in MEMORY/16-knowledge.md §8.
"""The metadata filter language — one syntax in the API, the agent tool and the Retrieve step.

Mongo/Pinecone-style ``$`` operators (Naveen, 2026-09-30), so it reads like what
RAG builders already write and translates one-to-one to Pinecone and closely
to Qdrant when external stores arrive:

    {"doc_type": "contract", "year": {"$gte": 2024}, "$or": [{"region": "EU"}, {"tags": "urgent"}]}

**Syntax.**

* An object is an AND of its keys. ``$and`` / ``$or`` take a non-empty list of
  objects; ``$not`` takes one object.
* A bare scalar is ``$eq``. ``{"f": {"$gte": 1, "$lt": 5}}`` is an AND. A bare
  list is an error (say ``$in``).
* Operators: ``$eq $ne $gt $gte $lt $lte $in $nin $exists $and $or $not``.
* Fields are the built-ins (`BUILTINS`) or a metadata key matching `FIELD_RE`.
  Metadata keys are flat.
* The agent tool passes the filter as a JSON *string* (a tool schema may not
  hold type unions — the shape that broke Gemini on 2026-09-01); `parse_filter`
  accepts both.

**Semantics** — identical here (`evaluate`) and in SQL (`filters_sql`), and
proven so by one shared case table run on SQLite and Postgres. Every leaf is
true or false, never unknown:

* ``$eq v``: the field is present, of the same class as `v` (number, string or
  bool — a bool is never a number), and equal; or the field is a list of
  strings containing the string `v`.
* ``$ne`` is ``NOT $eq`` — so a missing field matches ``$ne``.
* ``$gt $gte $lt $lte``: same-class numbers or strings only; strings compare by
  code point. Never true for a list, a bool or a missing field.
* ``$in`` is an OR of ``$eq``; ``$nin`` is ``NOT $in``.
* ``$exists true``: present and not null.
* ``doc_date`` compares as its ``YYYY-MM-DD`` string; ``indexed_at`` compares as
  a time, the operand read as an ISO date or date-time (UTC when it has no zone).

Pure Python on purpose: an external index without filter pushdown post-filters
with `evaluate`, and the cloud API validates filters without SQLAlchemy.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal, Union

from core.knowledge.errors import FilterError

Scalar = Union[str, int, float, bool]

CMP_OPS: tuple[str, ...] = ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte")
RANGE_OPS: tuple[str, ...] = ("$gt", "$gte", "$lt", "$lte")
SET_OPS: tuple[str, ...] = ("$in", "$nin")
LOGIC_OPS: tuple[str, ...] = ("$and", "$or", "$not")
FIELD_OPS: tuple[str, ...] = (*CMP_OPS, *SET_OPS, "$exists")

#: The fields every chunk has, whatever its metadata. Their type decides which
#: operators and values are allowed.
BUILTINS: dict[str, str] = {
    "path": "string",
    "source": "enum",
    "mime": "string",
    "title": "string",
    "doc_date": "date",
    "indexed_at": "datetime",
}
SOURCE_VALUES: tuple[str, ...] = ("upload", "web", "record", "vault")

#: Metadata names a document may not set: they would shadow a built-in. `title`
#: and `doc_date` are allowed at ingest and lifted into their columns.
RESERVED_METADATA: frozenset[str] = frozenset({"path", "source", "mime", "indexed_at"})

FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FILTER_ERROR_CODES: tuple[str, ...] = (
    "bad_json", "not_an_object", "too_deep", "too_many_conditions", "too_many_values",
    "empty_list", "unknown_operator", "bad_field", "bad_value", "type_mismatch",
)


@dataclass(frozen=True)
class FilterLimits:
    max_depth: int = 4
    max_conditions: int = 50
    max_in_values: int = 100
    max_string: int = 1000


LIMITS = FilterLimits()


# ── the syntax tree ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Cmp:
    field: str
    op: Literal["$eq", "$ne", "$gt", "$gte", "$lt", "$lte"]
    value: Scalar


@dataclass(frozen=True)
class In_:
    field: str
    op: Literal["$in", "$nin"]
    values: tuple[Scalar, ...]


@dataclass(frozen=True)
class Exists:
    field: str
    value: bool


@dataclass(frozen=True)
class And:
    children: tuple[FilterNode, ...]


@dataclass(frozen=True)
class Or:
    children: tuple[FilterNode, ...]


@dataclass(frozen=True)
class Not:
    child: FilterNode


FilterNode = Union[Cmp, In_, Exists, And, Or, Not]


# ── parsing ────────────────────────────────────────────────────────────────────

def _ptr(path: str, key: object) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{token}"


def _class_of(value: object) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


class _Parser:
    def __init__(self, limits: FilterLimits):
        self.limits = limits
        self.conditions = 0

    def object(self, obj: object, path: str, depth: int) -> FilterNode:
        if not isinstance(obj, dict):
            raise FilterError("not_an_object", path, "A filter is a JSON object.")
        if not obj:
            raise FilterError("bad_value", path, "An empty object filters nothing out; leave the filter out instead.")
        if depth > self.limits.max_depth:
            raise FilterError("too_deep", path, f"Filters nest at most {self.limits.max_depth} levels deep.")
        children: list[FilterNode] = []
        for key, value in obj.items():
            here = _ptr(path, key)
            if not isinstance(key, str):
                raise FilterError("bad_field", here, "Field names are strings.")
            if key in ("$and", "$or"):
                if not isinstance(value, list):
                    raise FilterError("bad_value", here, f"{key} takes a list of filters.")
                if not value:
                    raise FilterError("empty_list", here, f"{key} needs at least one filter.")
                parts = tuple(self.object(v, _ptr(here, i), depth + 1) for i, v in enumerate(value))
                children.append(And(parts) if key == "$and" else Or(parts))
            elif key == "$not":
                children.append(Not(self.object(value, here, depth + 1)))
            elif key.startswith("$"):
                raise FilterError("unknown_operator", here, f"Unknown operator {key}.")
            else:
                children.extend(self.field(key, value, here))
        return children[0] if len(children) == 1 else And(tuple(children))

    def field(self, name: str, spec: object, path: str) -> list[FilterNode]:
        if name not in BUILTINS and not FIELD_RE.match(name):
            raise FilterError("bad_field", path,
                              "A field is a built-in or a metadata key of letters, digits and _ (at most 64).")
        if isinstance(spec, dict):
            if not spec:
                raise FilterError("bad_value", path, "An empty condition; give an operator such as $eq.")
            leaves = []
            for op, value in spec.items():
                here = _ptr(path, op)
                if op not in FIELD_OPS:
                    raise FilterError("unknown_operator", here,
                                      f"Unknown operator {op}." if str(op).startswith("$")
                                      else f"Nested objects are not supported; use an operator such as $eq, not {op!r}.")
                leaves.append(self.leaf(name, op, value, here))
            return leaves
        if isinstance(spec, list):
            raise FilterError("bad_value", path, "A list needs an operator: {\"$in\": [...]}.")
        return [self.leaf(name, "$eq", spec, path)]

    def leaf(self, name: str, op: str, value: object, path: str) -> FilterNode:
        self.conditions += 1
        if self.conditions > self.limits.max_conditions:
            raise FilterError("too_many_conditions", path,
                              f"A filter holds at most {self.limits.max_conditions} conditions.")
        if op == "$exists":
            if not isinstance(value, bool):
                raise FilterError("bad_value", path, "$exists takes true or false.")
            return Exists(name, value)
        if op in SET_OPS:
            if not isinstance(value, list):
                raise FilterError("bad_value", path, f"{op} takes a list.")
            if not value:
                raise FilterError("empty_list", path, f"{op} needs at least one value.")
            if len(value) > self.limits.max_in_values:
                raise FilterError("too_many_values", path, f"{op} takes at most {self.limits.max_in_values} values.")
            items = tuple(self.value(name, "$eq", v, _ptr(path, i)) for i, v in enumerate(value))
            return In_(name, op, items)  # type: ignore[arg-type]
        return Cmp(name, op, self.value(name, op, value, path))  # type: ignore[arg-type]

    def value(self, name: str, op: str, value: object, path: str) -> Scalar:
        cls = _class_of(value)
        if cls is None:
            raise FilterError("bad_value", path, "Values are strings, numbers or true/false.")
        if cls == "number" and not math.isfinite(float(value)):  # type: ignore[arg-type]
            raise FilterError("bad_value", path, "Numbers must be finite.")
        if cls == "string" and len(value) > self.limits.max_string:  # type: ignore[arg-type]
            raise FilterError("bad_value", path, f"Strings are at most {self.limits.max_string} characters.")
        if op in RANGE_OPS and cls == "bool":
            raise FilterError("type_mismatch", path, f"{op} compares numbers or strings, not true/false.")
        kind = BUILTINS.get(name)
        if kind is None:
            return value  # type: ignore[return-value]
        if cls != "string":
            raise FilterError("type_mismatch", path, f"{name} is compared with a string.")
        if kind == "enum":
            if op in RANGE_OPS:
                raise FilterError("type_mismatch", path, f"{name} is compared with $eq, $ne, $in or $nin.")
            if value not in SOURCE_VALUES:
                raise FilterError("bad_value", path, f"{name} is one of {', '.join(SOURCE_VALUES)}.")
        elif kind == "date":
            if not _DATE_RE.match(value):  # type: ignore[arg-type]
                raise FilterError("bad_value", path, f"{name} is a date, YYYY-MM-DD.")
            try:
                date.fromisoformat(value)  # type: ignore[arg-type]
            except ValueError:
                raise FilterError("bad_value", path, f"{name} is a date, YYYY-MM-DD.") from None
        elif kind == "datetime":
            try:
                return parse_instant(value).isoformat()  # type: ignore[arg-type]
            except ValueError:
                raise FilterError("bad_value", path,
                                  f"{name} is an ISO date or date-time, e.g. 2026-09-30T12:00:00Z.") from None
        return value  # type: ignore[return-value]


def parse_instant(value: str) -> datetime:
    """An ISO date or date-time as an aware UTC datetime. A zone-less value is UTC."""
    text = value.strip()
    if _DATE_RE.match(text):
        parsed = datetime.fromisoformat(text + "T00:00:00")
    else:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_filter(obj: Any, *, limits: FilterLimits = LIMITS) -> FilterNode | None:
    """A filter from its JSON form (an object, or a JSON string of one), validated.

    None, "" and {} mean no filter. Raises `FilterError` with a code from
    `FILTER_ERROR_CODES` and a JSON-pointer path.
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
        except ValueError:
            raise FilterError("bad_json", "", "The filter is not valid JSON.") from None
    if isinstance(obj, dict) and not obj:
        return None
    return _Parser(limits).object(obj, "", 1)


# ── canonical form ─────────────────────────────────────────────────────────────

def to_canonical(node: FilterNode | None) -> dict | None:
    """The filter with every operator explicit and every AND/OR sorted.

    Two filters that mean the same thing by reordering have the same canonical
    form, so it is what a trace records and what a cache key is made from.
    """
    if node is None:
        return None
    return _canon(node)


def _canon(node: FilterNode) -> dict:
    if isinstance(node, Cmp):
        return {node.field: {node.op: node.value}}
    if isinstance(node, In_):
        return {node.field: {node.op: list(node.values)}}
    if isinstance(node, Exists):
        return {node.field: {"$exists": node.value}}
    if isinstance(node, Not):
        return {"$not": _canon(node.child)}
    key = "$and" if isinstance(node, And) else "$or"
    parts = [_canon(c) for c in node.children]
    parts.sort(key=lambda p: json.dumps(p, sort_keys=True, ensure_ascii=False))
    return {key: parts}


def field_names(node: FilterNode | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, (Cmp, In_, Exists)):
        return {node.field}
    if isinstance(node, Not):
        return field_names(node.child)
    out: set[str] = set()
    for child in node.children:
        out |= field_names(child)
    return out


# ── evaluation ─────────────────────────────────────────────────────────────────

def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return parse_instant(value)
        except ValueError:
            return None
    return None


def _eq(field: str, have: object, want: Scalar) -> bool:
    if have is None:
        return False
    if BUILTINS.get(field) == "datetime":
        a, b = _instant(have), _instant(want)
        return a is not None and b is not None and a == b
    if isinstance(have, list):
        return isinstance(want, str) and any(isinstance(x, str) and x == want for x in have)
    cls = _class_of(have)
    return cls is not None and cls == _class_of(want) and have == want


def _order(field: str, have: object, op: str, want: Scalar) -> bool:
    if have is None or isinstance(have, list):
        return False
    if BUILTINS.get(field) == "datetime":
        a, b = _instant(have), _instant(want)
        if a is None or b is None:
            return False
    else:
        cls = _class_of(have)
        if cls not in ("number", "string") or cls != _class_of(want):
            return False
        a, b = have, want  # type: ignore[assignment]
    if op == "$gt":
        return a > b  # type: ignore[operator]
    if op == "$gte":
        return a >= b  # type: ignore[operator]
    if op == "$lt":
        return a < b  # type: ignore[operator]
    return a <= b  # type: ignore[operator]


def evaluate(node: FilterNode | None, fields: Mapping[str, Any]) -> bool:
    """Whether a chunk whose filterable fields are `fields` passes `node`.

    `fields` holds the built-ins and the metadata in one mapping (the reserved
    names keep them apart). No filter passes everything.
    """
    if node is None:
        return True
    if isinstance(node, Cmp):
        have = fields.get(node.field)
        if node.op == "$eq":
            return _eq(node.field, have, node.value)
        if node.op == "$ne":
            return not _eq(node.field, have, node.value)
        return _order(node.field, have, node.op, node.value)
    if isinstance(node, In_):
        have = fields.get(node.field)
        hit = any(_eq(node.field, have, v) for v in node.values)
        return hit if node.op == "$in" else not hit
    if isinstance(node, Exists):
        present = fields.get(node.field) is not None
        return present if node.value else not present
    if isinstance(node, Not):
        return not evaluate(node.child, fields)
    if isinstance(node, And):
        return all(evaluate(c, fields) for c in node.children)
    return any(evaluate(c, fields) for c in node.children)


def path_under(path: str, prefix: str) -> bool:
    """`path` is `prefix` or inside it, component-wise ("contracts" never matches "contracts2/a.pdf")."""
    clean = prefix.strip("/")
    return not clean or path == clean or path.startswith(clean + "/")


def check_metadata(metadata: Mapping[str, Any], path: str = "/metadata") -> dict:
    """Metadata a document or record may carry, validated at ingest.

    Keys match `FIELD_RE` and are not reserved; values are strings, finite
    numbers, true/false or lists of strings. Null is refused: a missing key and
    a null key would otherwise filter differently in SQL and Python.
    """
    out: dict = {}
    for key, value in metadata.items():
        here = _ptr(path, key)
        if not isinstance(key, str) or not FIELD_RE.match(key):
            raise FilterError("bad_field", here, "Metadata keys are letters, digits and _ (at most 64).")
        if key in RESERVED_METADATA:
            raise FilterError("bad_field", here, f"{key} is set by Synapse and cannot be metadata.")
        if isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise FilterError("bad_value", here, "A metadata list holds strings only.")
            out[key] = list(value)
            continue
        cls = _class_of(value)
        if cls is None:
            raise FilterError("bad_value", here, "Metadata values are strings, numbers, true/false or lists of strings.")
        if cls == "number" and not math.isfinite(float(value)):
            raise FilterError("bad_value", here, "Numbers must be finite.")
        out[key] = value
    return out
