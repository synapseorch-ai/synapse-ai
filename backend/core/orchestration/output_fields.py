"""What a model step answers when it declares its fields, and how the run holds it to them (F1553, BLD4).

An `llm` or `agent` step's answer is the model's text, and a later step that reads a field of it (an If/Else on
`state.analyze.risky`) read a guess: Extract JSON or the engine's typed read found JSON when the model wrote some,
and a miss took the If/Else's documented False with the run Completed (BLD4's triage: a 2-star review got the
thank-you email, a safe clause went to legal). A step that declares `output_fields` answers that one object instead:

* `instruction` is what the step's prompt adds, and `json_schema` what a provider with a JSON schema mode is sent
  (`llm_providers`: OpenAI, Gemini, Ollama), so the model is asked in both ways it can be;
* `read` takes the object out of the answer as Extract JSON takes JSON out of text (`steps.extract_all_json`: the
  whole text, a fenced block, matching braces) and types each field: a yes/no said in words is the flag
  (`human_fields.as_flag`, the Human step's own reading), a number written as text is the number, a choice is its
  option whatever its case, a lone text is a list of one; an email, a date or a phone is checked as a Human step's;
* on a miss the engine asks the step's own model once more with `repair_prompt` (its own prompt, its own answer and
  what was missing: the same judge, never a second one reading the text alone), and a second miss fails the step with
  `missing`'s sentence — never a branch taken on a guess (Naveen, 11-12).

The field types are the Human step's (`human_fields.FIELD_TYPES`, its spellings too), and `list`, a list of text.
`declaration_problems` is the validator's rule for a declaration the run could not hold a model to.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from core.orchestration.human_fields import ALIASES, FIELD_TYPES, BadAnswer, as_flag, check_value, field_options

#: Every type an answer field can be: the Human step's, a list (of text unless it says `of`), and a group of fields
#: (`object`, F1618).
ANSWER_TYPES = (*FIELD_TYPES, "list", "object")
_ANSWER_ALIASES = {**ALIASES, "array": "list", "strings": "list", "list_of_text": "list", "group": "object"}
#: How deep a declaration may nest, how many fields it may hold in all, and how many may be empty (F1618): a schema an
#: editor and a model can still read, and a provider's own limit on fields that may be empty (16 per request).
MAX_LEVELS = 4
MAX_FIELDS = 60
MAX_MAY_BE_EMPTY = 16
#: What a list may be of, besides groups (`{"fields": [...]}`).
_LIST_OF = {"text": "string", "string": "string", "number": "number", "boolean": "boolean"}
#: How many problems a repair or a failure names before saying how many more.
_NAMED = 5
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: How much of an answer a failure quotes.
_QUOTED = 200


def field_type(raw: Any) -> str | None:
    """The type an answer field is, or None for one this table does not know. No type is text."""
    key = str(raw or "").strip().lower()
    if not key:
        return "string"
    if key in ANSWER_TYPES:
        return key
    return _ANSWER_ALIASES.get(key)


def normalised(fields: Any) -> list[dict[str, Any]]:
    """The declared fields that name something, as given (`output_fields` on the step, or its JSON from a tool)."""
    if isinstance(fields, str):
        try:
            fields = json.loads(fields or "[]")
        except ValueError:
            return []
    return [f for f in fields or [] if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].strip()]


def names(fields: Any) -> tuple[str, ...]:
    return tuple(f["name"].strip() for f in normalised(fields))


def declaration_problems(fields: Any) -> list[str]:
    """What keeps a declaration from being held: a name a condition cannot read, one twice, a type this table does
    not know, a choice with nothing to choose from, a group with no fields, a list of no kind; and past the limits,
    deeper than four levels, more than 60 fields, or more than 16 that may be empty (F1618)."""
    problems: list[str] = []
    counts = {"fields": 0, "empty": 0, "deep": False}
    _problems_in(fields, 1, "", problems, counts)
    if counts["deep"]:
        problems.append(f"the answer nests deeper than {_words(MAX_LEVELS)} levels: flatten a group into its parent")
    if counts["fields"] > MAX_FIELDS:
        problems.append(f"the answer has {counts['fields']} fields; at most {MAX_FIELDS}: keep the ones a later step reads")
    if counts["empty"] > MAX_MAY_BE_EMPTY:
        problems.append(f"{counts['empty']} fields may be empty; at most {MAX_MAY_BE_EMPTY}: make the rest always given")
    return problems


def _words(n: int) -> str:
    return {4: "four"}.get(n, str(n))


def _problems_in(fields: Any, level: int, prefix: str, problems: list[str], counts: dict) -> None:
    seen: set[str] = set()
    if level > MAX_LEVELS:
        counts["deep"] = True
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f["name"].strip():
            problems.append("a field with no name: give each a `name`")
            continue
        name = f["name"].strip()
        where = f"{prefix}{name}"
        if not _NAME.match(name):
            problems.append(f"`{where}` is not a name a later step can read as state.<key>.<name>: use letters, "
                            "digits and _ (e.g. `is_risky`)")
        if name in seen:
            problems.append(f"`{where}` is declared twice")
        seen.add(name)
        if f.get("may_be_empty"):
            counts["empty"] += 1
        kind = field_type(f.get("type"))
        if kind is None:
            problems.append(f"`{where}` has type '{f.get('type')}', which is not a type: use one of text, number, "
                            "boolean (a yes or no), choice (with options), list, or object (a group with its own "
                            "fields)")
            counts["fields"] += 1
        elif kind == "choice" and not field_options(f):
            problems.append(f'`{where}` is a choice with no options: give it "options": ["…", "…"], one of which '
                            "the model picks")
            counts["fields"] += 1
        elif kind == "object":
            inner = f.get("fields")
            if not isinstance(inner, list) or not inner:
                problems.append(f'`{where}` is a group with no fields: give it "fields": [...]')
            else:
                _problems_in(inner, level + 1, f"{where}.", problems, counts)
        elif kind == "list":
            of = f.get("of")
            if isinstance(of, dict):
                inner = of.get("fields")
                if not isinstance(inner, list) or not inner:
                    problems.append(f'`{where}` is a list of groups with no fields: give its "of" "fields": [...]')
                else:
                    _problems_in(inner, level + 1, f"{where}[].", problems, counts)
            elif of is not None and str(of).strip().lower() not in _LIST_OF:
                problems.append(f"`{where}` is a list of '{of}', which is not a kind: use text, number, boolean, or "
                                '{"fields": [...]} for a list of groups')
                counts["fields"] += 1
            else:
                counts["fields"] += 1
        else:
            counts["fields"] += 1


def _schema_of(field: dict[str, Any]) -> dict[str, Any]:
    kind = field_type(field.get("type")) or "string"
    if kind == "boolean":
        out: dict[str, Any] = {"type": "boolean"}
    elif kind == "number":
        out = {"type": "number"}
    elif kind == "choice" and field_options(field):
        out = {"type": "string", "enum": field_options(field)}
    elif kind == "list":
        of = field.get("of")
        if isinstance(of, dict):
            out = {"type": "array", "items": _object_schema(of.get("fields"))}
        else:
            out = {"type": "array", "items": {"type": _LIST_OF.get(str(of or "text").strip().lower(), "string")}}
    elif kind == "object":
        out = _object_schema(field.get("fields"))
    else:
        out = {"type": "string"}
    if field.get("may_be_empty"):
        # Itself or nothing (F1618): still required, as a strict schema wants every field.
        out = {"anyOf": [out, {"type": "null"}]}
    if str(field.get("description") or "").strip():
        out["description"] = str(field["description"]).strip()
    return out


def _object_schema(fields: Any) -> dict[str, Any]:
    declared = normalised(fields)
    return {"type": "object", "properties": {f["name"].strip(): _schema_of(f) for f in declared},
            "required": [f["name"].strip() for f in declared], "additionalProperties": False}


def json_schema(fields: Any) -> dict[str, Any]:
    """The one object a provider's JSON schema mode is asked for: every field, no other (OpenAI strict's shape), and
    each group's own fields the same way to its depth (F1618)."""
    return _object_schema(fields)


#: How many items a made-up list holds: two, so code that pairs, compares or keys on items sees more than one.
SAMPLE_ITEMS = 2


def sample(fields: Any) -> dict[str, Any]:
    """A made-up answer in the declared shape (F1619): every field there, each a value of its type, a list with
    `SAMPLE_ITEMS` items, a group with its own fields to its depth. The builder's practice run reads it in place of a
    model's or a tool's answer, and `read` takes it as it is. A field that may be empty is given: the practice tests
    the shape, and the read checks judge the empties. The two items of a list differ."""
    return _sampled(normalised(fields), 1)


def _sampled(declared: list[dict[str, Any]], nth: int) -> dict[str, Any]:
    return {f["name"].strip(): _sample_value(f, nth) for f in declared}


def _sample_value(field: dict[str, Any], nth: int) -> Any:
    kind = field_type(field.get("type")) or "string"
    name = field["name"].strip()
    if kind == "boolean":
        return True
    if kind == "number":
        return 2 + nth
    if kind == "email":
        return f"person{nth}@example.com"
    if kind == "date":
        return f"2026-01-{10 + nth:02d}"
    if kind == "phone":
        return f"+1 555 010{nth}"
    if kind == "choice" and field_options(field):
        return field_options(field)[0]
    if kind == "object":
        return _sampled(normalised(field.get("fields")), nth)
    if kind == "list":
        of = field.get("of")
        if isinstance(of, dict):
            return [_sampled(normalised(of.get("fields")), i) for i in range(1, SAMPLE_ITEMS + 1)]
        kind_of = str(of or "text").strip().lower()
        if kind_of == "number":
            return [2 + i for i in range(1, SAMPLE_ITEMS + 1)]
        if kind_of == "boolean":
            return [True] * SAMPLE_ITEMS
        return [f"example {name} {i}" for i in range(1, SAMPLE_ITEMS + 1)]
    return f"example {name} {nth}"


def type_words(field: dict[str, Any]) -> str:
    kind = field_type(field.get("type")) or "string"
    if kind == "boolean":
        return "true or false"
    if kind == "number":
        return "a number"
    if kind == "choice" and field_options(field):
        return "one of " + ", ".join(json.dumps(o) for o in field_options(field))
    if kind == "list":
        of = field.get("of")
        if isinstance(of, dict):
            return "a list of groups, each with: " + ", ".join(names(of.get("fields")))
        return {"number": "a list of numbers", "boolean": "a list of true or false"}.get(
            str(of or "").strip().lower(), "a list of text")
    if kind == "object":
        return "a group with: " + ", ".join(names(field.get("fields")))
    if kind == "date":
        return "a date, YYYY-MM-DD"
    return "text"


def instruction(fields: Any) -> str:
    """What the step's prompt adds: the one object it answers, each field with its type in words."""
    lines = ["## YOUR ANSWER",
             "Answer with one JSON object and nothing else, with exactly these fields:"]
    _said_lines(fields, "", lines)
    return "\n".join(lines)


def _said_lines(fields: Any, indent: str, lines: list[str]) -> None:
    """Each field with its type in words, and a group's own fields under it (F1618)."""
    for f in normalised(fields):
        about = str(f.get("description") or "").strip()
        empty = " (or null when there is none)" if f.get("may_be_empty") else ""
        lines.append(f"{indent}- `{f['name'].strip()}`: {type_words(f)}{empty}" + (f" — {about}" if about else ""))
        kind = field_type(f.get("type"))
        inner = (f.get("fields") if kind == "object"
                 else (f.get("of") or {}).get("fields") if kind == "list" and isinstance(f.get("of"), dict) else None)
        if inner:
            _said_lines(inner, indent + "  ", lines)


def path_problem(fields: Any, path: list[str], root: str = "its output_fields") -> str | None:
    """Why `path` (the parts after the step's key: field names and list indexes) reads nothing an answer declaring
    `fields` has, with its fix; None when it holds (F1618: every read checked to its end). `root` names where the
    answer's top fields are declared: a model step's output_fields, or a tool's declared fields (F1620)."""
    import difflib

    group, field, trail = normalised(fields), None, []

    def where() -> str:
        named = [t for t in trail if not t.isdigit()]
        return f"`{named[-1]}`'s fields" if named else root

    for i, part in enumerate(path):
        if field is not None:
            kind = field_type(field.get("type"))
            if kind == "list":
                if not part.isdigit():
                    name = ".".join(trail)
                    return (f"`{name}` is a list: read one item of it (`{name}.0.{part}`), or loop over it and read "
                            f"`{part}` of each")
                trail.append(part)
                of = field.get("of")
                if isinstance(of, dict):
                    group, field = normalised(of.get("fields")), None
                    continue
                if i + 1 < len(path):
                    return f"`{'.'.join(trail)}` is one {of or 'text'} of a list, which has no fields: read it itself"
                return None
            if kind != "object":
                return f"`{'.'.join(trail)}` is {type_words(field)}, which has no fields: read `{'.'.join(trail)}` itself"
            group, field = normalised(field.get("fields")), None
        names = [f["name"].strip() for f in group]
        match = next((f for f in group if f["name"].strip() == part), None)
        if match is None:
            close = difflib.get_close_matches(part, names, n=1)
            if close:
                return (f"`{part}` is not one of {where()}: read `{'.'.join([*trail, close[0]])}` (did you mean "
                        f"`{close[0]}`?), or declare `{part}` in {where()}")
            listed = ", ".join(f"`{n}`" for n in names) or "none yet"
            return f"`{part}` is not one of {where()} ({listed}): declare `{part}` in {where()}"
        trail.append(part)
        field = match
    return None


def proposed(paths: list[list[str]], numbers: Any = ()) -> list[dict[str, Any]]:
    """The answer shape later steps read, from their paths (F1618): a field read by an index is a list, its item's
    reads that list's fields; a field read by a field is a group; a field compared with a number (its path in
    `numbers`) a number; any other read text. Never more than the reads say."""
    tree: dict[str, dict] = {}
    numbered = {tuple(p) for p in numbers}
    for path in paths:
        node, i, seen = tree, 0, []
        while i < len(path):
            part = path[i]
            if part.isdigit():
                i += 1
                continue
            entry = node.setdefault(part, {"list": False, "children": {}, "number": False})
            seen.append(part)
            if i + 1 < len(path) and path[i + 1].isdigit():
                entry["list"] = True
                i += 2
            else:
                i += 1
            if i >= len(path) and tuple(path) in numbered:
                entry["number"] = True
            node = entry["children"]

    def fields_of(node: dict) -> list[dict[str, Any]]:
        out = []
        for name, e in node.items():
            if e["list"]:
                out.append({"name": name, "type": "list", **({"of": {"fields": fields_of(e["children"])}}
                                                             if e["children"] else {})})
            elif e["children"]:
                out.append({"name": name, "type": "object", "fields": fields_of(e["children"])})
            else:
                out.append({"name": name, "type": "number" if e["number"] else "text"})
        return out

    return fields_of(tree)


def _number(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() and "." not in text and "e" not in text.lower() else number
    return None


def _typed(field: dict[str, Any], value: Any) -> tuple[bool, Any]:
    """(True, the value in its stored form), or (False, None) when it is not one of the field's type."""
    kind = field_type(field.get("type")) or "string"
    if value is None:
        return False, None
    if kind == "boolean":
        flag = as_flag(value)
        return (True, flag) if isinstance(flag, bool) else (False, None)
    if kind == "number":
        number = _number(value)
        return (number is not None), number
    if kind == "choice":
        options = field_options(field)
        if not options:
            return True, value
        said = str(value).strip().lower()
        for option in options:
            if option.lower() == said:
                return True, option
        return False, None
    if kind == "list":
        if isinstance(value, list):
            return True, value
        if isinstance(value, str) and value.strip():
            return True, [value.strip()]
        return False, None
    if isinstance(value, (dict, list)):
        return True, json.dumps(value, ensure_ascii=False)
    if kind in ("email", "date", "phone"):
        try:
            return True, check_value(field, value)
        except BadAnswer:
            return False, None
    return True, str(value) if not isinstance(value, str) else value


def _not_its_type(field: dict[str, Any], value: Any, path: str | None = None, quote: Quote | None = None) -> str:
    if quote is not None:
        shown = quote(value)
    else:
        shown = json.dumps(value, ensure_ascii=False, default=str)
        shown = shown if len(shown) <= 80 else shown[:80] + "…"
    return f"`{path or field['name'].strip()}` is {shown}, not {type_words(field)}"


def read(fields: Any, text: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """The declared object out of `text`, each field typed to its depth, or (None, what is missing or not of its type,
    each named by its path: `leads[2].email`, F1618)."""
    from core.orchestration.steps import extract_all_json

    declared = normalised(fields)
    if isinstance(text, dict):
        found: list = [text]
    else:
        found = [x for x in extract_all_json(str(text or "")) if isinstance(x, dict)]
        if not found:
            # A list of objects (several fenced blocks, or one array): the objects in it.
            found = [y for x in extract_all_json(str(text or "")) if isinstance(x, list) for y in x
                     if isinstance(y, dict)]
    wanted = [f["name"].strip() for f in declared]
    best = max(found, key=lambda obj: sum(1 for n in wanted if n in obj), default=None)
    if best is None:
        return None, ["no JSON object with " + ", ".join(f"`{n}`" for n in wanted)]
    problems: list[str] = []
    answer = _read_fields(declared, best, "", problems)
    return (None, problems) if problems else (answer, [])


_NOT_READ = object()

#: How a value a problem names is shown: its JSON, as a person may read it.
Quote = Callable[[Any], str]


@dataclass(frozen=True)
class _Holding:
    """How an answer is held where it is not a model's (F1620): a tool's. `quote` shows a value a problem names;
    `absent_when_empty` lets a field that may be empty be absent too, as a tool's declaration leaves a field it does
    not require out; `presence_only` holds a declaration known at its first level to its fields being there."""

    quote: Quote | None = None
    absent_when_empty: bool = True
    presence_only: bool = False


def held(fields: Any, value: Any, *, quote: Quote, first_level: bool = False) -> list[str]:
    """What a tool's answer lacks of what its tool declares (F1620): each declared field missing, or not of its type,
    named by its path to its depth (`leads[2].email`), as a model's answer is read (`read`); a field it does not
    require may be absent or empty. A declaration known at its first level only (`first_level`) is held to its fields
    being there. An answer that is no JSON object is one problem. Each value a problem names is shown by `quote`."""
    obj = value
    if isinstance(value, (str, bytes)):
        try:
            obj = json.loads(value)
        except ValueError:
            obj = None
    if not isinstance(obj, dict):
        return [f"the answer is {quote(value)}, not an object"]
    problems: list[str] = []
    _read_fields(normalised(fields), obj, "", problems, _Holding(quote=quote, presence_only=first_level))
    return problems


def _read_fields(declared: list[dict[str, Any]], obj: dict, prefix: str, problems: list[str],
                 how: _Holding | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in declared:
        name = f["name"].strip()
        path = f"{prefix}{name}"
        if name not in obj:
            if not (how is not None and how.absent_when_empty and f.get("may_be_empty")):
                problems.append(f"`{path}` is missing")
            continue
        value = _read_value(f, obj[name], path, problems, how)
        if value is not _NOT_READ:
            out[name] = value
    return out


def _read_value(f: dict[str, Any], value: Any, path: str, problems: list[str], how: _Holding | None = None) -> Any:
    quote = how.quote if how is not None else None
    if value is None and f.get("may_be_empty"):
        return None
    kind = field_type(f.get("type")) or "string"
    if kind == "object":
        if not isinstance(value, dict):
            problems.append(_not_its_type(f, value, path, quote))
            return _NOT_READ
        return _read_fields(normalised(f.get("fields")), value, f"{path}.", problems, how)
    if kind == "list" and isinstance(f.get("of"), dict):
        items = value if isinstance(value, list) else [value] if isinstance(value, dict) else None
        if items is None:
            problems.append(_not_its_type(f, value, path, quote))
            return _NOT_READ
        inner = normalised(f["of"].get("fields"))
        read_items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                shown = quote(item) if quote is not None else json.dumps(item, ensure_ascii=False, default=str)[:80]
                problems.append(f"`{path}[{i}]` is {shown}, not a group")
                continue
            read_items.append(_read_fields(inner, item, f"{path}[{i}].", problems, how))
        return read_items
    if kind == "list" and str(f.get("of") or "").strip().lower() in ("number", "boolean"):
        items = value if isinstance(value, list) else [value]
        each = {"name": f.get("name", ""), "type": str(f["of"]).strip().lower()}
        read_items = []
        for i, item in enumerate(items):
            ok, typed = _typed(each, item)
            if not ok:
                problems.append(_not_its_type(each, item, f"{path}[{i}]", quote))
                continue
            read_items.append(typed)
        return read_items
    if how is not None and how.presence_only:
        return value
    ok, typed = _typed(f, value)
    if not ok:
        problems.append(_not_its_type(f, value, path, quote))
        return _NOT_READ
    return typed


def _named(problems: list[str]) -> str:
    """The problems a repair or a failure names: five at most, then how many more (F1618)."""
    shown = "; ".join(problems[:_NAMED])
    return shown + (f"; and {len(problems) - _NAMED} more" if len(problems) > _NAMED else "")


def _quote(text: Any) -> str:
    said = " ".join(str(text or "").split())
    return said if len(said) <= _QUOTED else said[:_QUOTED] + "…"


def repair_prompt(prompt: str, answer: Any, problems: list[str], fields: Any) -> str:
    """The step's own prompt, its own answer, and what that answer lacks: asked once more of the same model."""
    return (f"{prompt}\n\n---\n\nYou answered:\n\n{str(answer or '').strip()}\n\n---\n\n"
            f"That answer cannot be read as the object asked for: {_named(problems)}. "
            "Answer again, from the same task, with only the JSON object.\n\n" + instruction(fields))


def missing(step_name: str, problems: list[str], answer: Any) -> str:
    """Why the step fails: what is missing, that it was asked once more, the repair, then what it answered."""
    return (f"'{step_name}' did not answer its answer fields ({_named(problems)}), asked twice. The step stops "
            "rather than guess: change its prompt or its answer fields, then Replay. It answered: "
            f"“{_quote(answer)}”")


def repaired(step_name: str, problems: list[str]) -> str:
    """The step's warning when the second ask gave the fields."""
    return f"'{step_name}' did not answer its answer fields at first ({'; '.join(problems)}); asked once more, it did."


# ── A decision read out of a model's words (F1553's follow-up, Naveen's pick 1) ─────────────────────────────────────

#: The methods only text has, as a condition calls them on a model's answer.
_TEXT_METHODS = frozenset({"split", "rsplit", "strip", "lstrip", "rstrip", "lower", "upper", "casefold", "replace",
                           "startswith", "endswith", "splitlines", "partition", "rpartition", "encode", "find", "rfind",
                           "count", "index", "title", "capitalize"})
#: The condition fields a decision is made in (a Loop's `loop_over` goes through a list; it decides nothing).
DECIDING_FIELDS = ("if_condition", "switch_expression", "loop_while_condition", "loop_break_condition")


def words_read(expr: Any, keys: set[str]) -> list[str]:
    """The keys of `keys` a condition decides on as words: a text method called on the whole value (`.strip()`,
    `.startswith(…)`), the value compared with a quoted word (`== 'URGENT'`), a quoted word `in` it, or it `in` a list
    of words. A whole read for its length or its truth, and a field of it, are not words."""
    import ast

    from core.expressions import _unbraced

    if not isinstance(expr, str) or not expr.strip():
        return []
    try:
        tree = ast.parse(_unbraced(expr), mode="eval")
    except SyntaxError:
        return []

    def whole(node: Any) -> str | None:
        """The key a node is the whole text of: `state.k`, `state['k']`, `str(…)` of one, a text method's or a slice's
        result on one."""
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "state":
            return node.attr
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "state":
            sl = node.slice
            return sl.value if isinstance(sl, ast.Constant) and isinstance(sl.value, str) else None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and \
                len(node.args) == 1:
            return whole(node.args[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _TEXT_METHODS:
            return whole(node.func.value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            return whole(node.value)
        return None

    def word(node: Any) -> bool:
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    def words(node: Any) -> bool:
        return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and bool(node.elts) and all(word(e) for e in node.elts)

    found: list[str] = []
    for node in ast.walk(tree):
        hits: list[str | None] = []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _TEXT_METHODS:
            hits.append(whole(node.func.value))
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for i, op in enumerate(node.ops):
                left, right = operands[i], operands[i + 1]
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    hits += [whole(left) if word(right) else None, whole(right) if word(left) else None]
                elif isinstance(op, (ast.In, ast.NotIn)):
                    hits += [whole(right) if word(left) else None, whole(left) if words(right) else None]
        for key in hits:
            if key in keys and key not in found:
                found.append(key)
    return found


def words_said(reader: dict[str, Any], writer: dict[str, Any], key: str, expr: str) -> str:
    """The warning for `reader` deciding on `writer`'s answer as words, and its fix."""
    shown = " ".join(str(expr).split())
    return (f"Step '{reader.get('id', '?')}' decides on {writer.get('type')} step '{writer.get('id', '?')}''s answer "
            f"as text (`{shown}`): it reads the model's own words; declare an answer field — give "
            f"'{writer.get('id', '?')}' output_fields (a choice with the words it picks from as options, or a boolean "
            f"for a yes or no) and read state.{key}.<field>. It runs as it is today.")


# ── A JSON Schema read into answer fields (F1620): a tool's declared answer ─────────────────────────────────────────
#
# The server's twin of the editor's `answerFieldsFromSchema` (`frontend/src/components/orchestration/answerFields.ts`),
# held to it byte for byte by a test. A schema inside the answer's subset becomes its fields; every part outside it is
# named by its path, and then nothing is taken, so a tool whose answer is not wholly known is never half-checked.

#: Words that describe a schema and constrain nothing: read past.
_READ_PAST = frozenset({"title", "$schema", "$id", "$comment", "examples", "default", "$defs", "definitions"})
_LIMITS = frozenset({"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "pattern",
                     "minItems", "maxItems", "uniqueItems", "multipleOf", "minProperties", "maxProperties",
                     "minContains", "maxContains"})
_SHAPES = frozenset({"oneOf", "allOf", "anyOf", "not", "if", "then", "else"})
_OPEN = frozenset({"additionalProperties", "patternProperties", "propertyNames", "unevaluatedProperties"})
_FORMATS = {"date": "date", "email": "email"}
#: What each type of node may say besides `type` and `description`.
_OWN = {"string": {"enum", "format"}, "number": set(), "integer": set(), "boolean": set(), "array": {"items"},
        "object": {"properties", "required", "additionalProperties"}}


def _at(where: str) -> str:
    return f"`{where}`" if where else "The schema"


def _refused_keyword(key: str, value: Any, kind: str | None) -> str | None:
    """Why a keyword is outside the subset, or None when it is inside it."""
    if key in ("type", "description") or key in _READ_PAST:
        return None
    if key == "$ref":
        return "uses $ref: write the shape out where it is used"
    if key in _SHAPES:
        return f"is one of several shapes ({key}): a field has one type, or that type or nothing (null)"
    if key in _LIMITS:
        return f"has `{key}`: an answer holds no limits on lengths, sizes or values; say it in the description"
    if key == "const":
        return 'has `const`: give it as a choice, "enum": ["…"]'
    if key == "additionalProperties" and value is False and kind == "object":
        return None
    if key in _OPEN:
        return f"takes keys besides its own ({key}): an answer holds its named fields only"
    if key == "enum":
        words = kind == "string" and isinstance(value, list) and len(value) > 0 and all(isinstance(v, str) for v in value)
        return None if words else "has an enum that is not words: a choice is one of some words"
    if key == "format" and kind == "string":
        return None if str(value) in _FORMATS else (
            f"has format '{value}': an answer holds the date and email formats; take it out to read it as text")
    if kind and key in _OWN.get(kind, ()):
        return None
    return f"has `{key}`, which an answer cannot hold"


def _from_node(name: str, node: Any, where: str, required: bool, refused: list[str]) -> dict | None:
    if not isinstance(node, dict):
        refused.append(f"{_at(where)} is not a schema")
        return None
    may = not required
    description = node["description"].strip() if isinstance(node.get("description"), str) else ""
    inner = node
    # Itself or nothing, as the run writes it (`anyOf` with null) or as a list of two types.
    if isinstance(node.get("anyOf"), list):
        parts = node["anyOf"]

        def is_null(p: Any) -> bool:
            return isinstance(p, dict) and p.get("type") == "null" and len(p) == 1

        other = next((p for p in parts if not is_null(p)), None) \
            if len(parts) == 2 and any(is_null(p) for p in parts) else None
        outer_ok = all(k in ("anyOf", "description") or k in _READ_PAST for k in node)
        if not isinstance(other, dict) or not outer_ok:
            refused.append(f"{_at(where)} is one of several shapes (anyOf): a field has one type, or that type or "
                           "nothing (null)")
            return None
        inner = other
        may = True
        if not description and isinstance(other.get("description"), str):
            description = other["description"].strip()
    kind = inner["type"] if isinstance(inner.get("type"), str) else None
    if isinstance(inner.get("type"), list):
        types = [str(t) for t in inner["type"]]
        rest = [t for t in types if t != "null"]
        if len(types) != 2 or len(rest) != 1:
            refused.append(f"{_at(where)} has several types ({', '.join(types)}): a field has one type, or that type "
                           "or null")
            return None
        kind = rest[0]
        may = True
    if kind is None and isinstance(inner.get("properties"), dict):
        kind = "object"
    reasons = [r for k, v in inner.items() if (r := _refused_keyword(k, v, kind)) is not None]
    if reasons:
        refused.append(f"{_at(where)} {reasons[0]}")
        return None
    if kind is None:
        refused.append(f"{_at(where)} has no type: give it one of string, number, boolean, array or object")
        return None
    if kind not in _OWN:
        refused.append(f"{_at(where)} has type '{kind}', which an answer cannot hold")
        return None
    out: dict[str, Any] = {"name": name, "type": "string"}
    if kind in ("number", "integer"):
        out["type"] = "number"
    elif kind == "boolean":
        out["type"] = "boolean"
    elif kind == "string":
        if isinstance(inner.get("enum"), list):
            out["type"] = "choice"
            out["options"] = [str(o) for o in inner["enum"]]
        elif isinstance(inner.get("format"), str):
            out["type"] = _FORMATS[inner["format"]]
    elif kind == "array":
        items = inner.get("items")
        if isinstance(items, list):
            refused.append(f"{_at(where)} gives its `items` as a list of shapes: a list's items share one shape")
            return None
        if not isinstance(items, dict):
            refused.append(f"{_at(where)} is a list of anything: give its `items` a type")
            return None
        item = _from_node("item", items, f"{where}[]", True, refused)
        if item is None:
            return None
        if item["type"] == "list":
            refused.append(f"{_at(where)} is a list of lists: a list holds values or groups, one level at a time")
            return None
        if item.get("may_be_empty") or item.get("description") or \
                item["type"] not in ("string", "number", "boolean", "object"):
            refused.append(f"{_at(f'{where}[]')} is not plain text, a number, a yes or no or a group: a list's items "
                           "are one of those, never empty, and the list is described rather than its items")
            return None
        out["type"] = "list"
        if item["type"] == "object":
            out["of"] = {"fields": item.get("fields") or []}
        elif item["type"] != "string":
            out["of"] = item["type"]
    elif kind == "object":
        properties = inner.get("properties")
        if not isinstance(properties, dict) or not properties:
            refused.append(f"{_at(where)} is a group with no fields: give it properties")
            return None
        needed = {str(r) for r in inner["required"]} if isinstance(inner.get("required"), list) else set()
        fields = []
        for key, sub in properties.items():
            field = _from_node(key, sub, f"{where}.{key}" if where else key, key in needed, refused)
            if field:
                fields.append(field)
        out["type"] = "object"
        out["fields"] = fields
    if description:
        out["description"] = description
    if may:
        out["may_be_empty"] = True
    return out


def _ordered(field: dict) -> dict:
    """A field as a person reads it: name, type, then options, items, rows, description, may be empty."""
    out: dict[str, Any] = {"name": field["name"], "type": field["type"]}
    if "options" in field:
        out["options"] = field["options"]
    if "of" in field:
        out["of"] = ({"fields": [_ordered(f) for f in field["of"].get("fields") or []]}
                     if isinstance(field["of"], dict) else field["of"])
    if "fields" in field:
        out["fields"] = [_ordered(f) for f in field["fields"]]
    if field.get("description"):
        out["description"] = field["description"]
    if field.get("may_be_empty"):
        out["may_be_empty"] = True
    return out


def from_json_schema(schema: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """A JSON Schema of one object (or a provider's wrapper of one) as answer fields, when every part is inside the
    answer's subset; else no fields and each part outside it, named by its path (F1620: a tool's declared answer).
    The editor's `answerFieldsFromSchema`, byte for byte."""
    root = schema
    if isinstance(root, dict) and root.get("type") == "json_schema" and isinstance(root.get("json_schema"), dict) \
            and isinstance(root["json_schema"].get("schema"), dict):
        root = root["json_schema"]["schema"]
    elif isinstance(root, dict) and "properties" not in root and isinstance(root.get("schema"), dict):
        root = root["schema"]
    if not isinstance(root, dict) or not (root.get("type") == "object"
                                          or ("type" not in root and isinstance(root.get("properties"), dict))):
        return [], ['The answer is one object of named fields: a schema whose "type" is "object", with "properties".']
    refused: list[str] = []
    read = _from_node("answer", root, "", True, refused)
    if refused or read is None:
        return [], refused
    fields = [_ordered(f) for f in read.get("fields") or []]
    problems = declaration_problems(fields)
    return ([], problems) if problems else (fields, [])
