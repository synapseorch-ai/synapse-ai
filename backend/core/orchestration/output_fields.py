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
from typing import Any

from core.orchestration.human_fields import ALIASES, FIELD_TYPES, BadAnswer, as_flag, check_value, field_options

#: Every type an answer field can be: the Human step's, and a list of text.
ANSWER_TYPES = (*FIELD_TYPES, "list")
_ANSWER_ALIASES = {**ALIASES, "array": "list", "strings": "list", "list_of_text": "list"}
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
    not know, a choice with nothing to choose from."""
    problems: list[str] = []
    seen: set[str] = set()
    for f in fields if isinstance(fields, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("name"), str) or not f["name"].strip():
            problems.append("a field with no name: give each a `name`")
            continue
        name = f["name"].strip()
        if not _NAME.match(name):
            problems.append(f"`{name}` is not a name a later step can read as state.<key>.<name>: use letters, "
                            "digits and _ (e.g. `is_risky`)")
        if name in seen:
            problems.append(f"`{name}` is declared twice")
        seen.add(name)
        kind = field_type(f.get("type"))
        if kind is None:
            problems.append(f"`{name}` has type '{f.get('type')}', which is not a type: use one of "
                            "text, number, boolean (a yes or no), choice (with options) or list")
        elif kind == "choice" and not field_options(f):
            problems.append(f'`{name}` is a choice with no options: give it "options": ["…", "…"], one of which '
                            "the model picks")
    return problems


def _schema_of(field: dict[str, Any]) -> dict[str, Any]:
    kind = field_type(field.get("type")) or "string"
    if kind == "boolean":
        out: dict[str, Any] = {"type": "boolean"}
    elif kind == "number":
        out = {"type": "number"}
    elif kind == "choice" and field_options(field):
        out = {"type": "string", "enum": field_options(field)}
    elif kind == "list":
        out = {"type": "array", "items": {"type": "string"}}
    else:
        out = {"type": "string"}
    if str(field.get("description") or "").strip():
        out["description"] = str(field["description"]).strip()
    return out


def json_schema(fields: Any) -> dict[str, Any]:
    """The one object a provider's JSON schema mode is asked for: every field, no other (OpenAI strict's shape)."""
    declared = normalised(fields)
    return {"type": "object", "properties": {f["name"].strip(): _schema_of(f) for f in declared},
            "required": [f["name"].strip() for f in declared], "additionalProperties": False}


def type_words(field: dict[str, Any]) -> str:
    kind = field_type(field.get("type")) or "string"
    if kind == "boolean":
        return "true or false"
    if kind == "number":
        return "a number"
    if kind == "choice" and field_options(field):
        return "one of " + ", ".join(json.dumps(o) for o in field_options(field))
    if kind == "list":
        return "a list of text"
    if kind == "date":
        return "a date, YYYY-MM-DD"
    return "text"


def instruction(fields: Any) -> str:
    """What the step's prompt adds: the one object it answers, each field with its type in words."""
    lines = ["## YOUR ANSWER",
             "Answer with one JSON object and nothing else, with exactly these fields:"]
    for f in normalised(fields):
        about = str(f.get("description") or "").strip()
        lines.append(f"- `{f['name'].strip()}`: {type_words(f)}" + (f" — {about}" if about else ""))
    return "\n".join(lines)


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


def _not_its_type(field: dict[str, Any], value: Any) -> str:
    shown = json.dumps(value, ensure_ascii=False, default=str)
    shown = shown if len(shown) <= 80 else shown[:80] + "…"
    return f"`{field['name'].strip()}` is {shown}, not {type_words(field)}"


def read(fields: Any, text: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """The declared object out of `text`, each field typed, or (None, what is missing or not of its type)."""
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
    answer: dict[str, Any] = {}
    problems: list[str] = []
    for f in declared:
        name = f["name"].strip()
        if name not in best:
            problems.append(f"`{name}` is missing")
            continue
        ok, value = _typed(f, best[name])
        if not ok:
            problems.append(_not_its_type(f, best[name]))
            continue
        answer[name] = value
    return (None, problems) if problems else (answer, [])


def _quote(text: Any) -> str:
    said = " ".join(str(text or "").split())
    return said if len(said) <= _QUOTED else said[:_QUOTED] + "…"


def repair_prompt(prompt: str, answer: Any, problems: list[str], fields: Any) -> str:
    """The step's own prompt, its own answer, and what that answer lacks: asked once more of the same model."""
    return (f"{prompt}\n\n---\n\nYou answered:\n\n{str(answer or '').strip()}\n\n---\n\n"
            f"That answer cannot be read as the object asked for: {'; '.join(problems)}. "
            "Answer again, from the same task, with only the JSON object.\n\n" + instruction(fields))


def missing(step_name: str, problems: list[str], answer: Any) -> str:
    """Why the step fails: what is missing, that it was asked once more, the repair, then what it answered."""
    return (f"'{step_name}' did not answer its answer fields ({'; '.join(problems)}), asked twice. The step stops "
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
