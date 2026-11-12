"""A Human step's fields: what types there are, and the check a typed answer passes (F455).

The browser's twin is `frontend/src/components/orchestration/humanFields.ts`. The two
read the same spellings, so a field the AI builder wrote as `select`, or one written by
hand as `tel`, is a dropdown or a phone number in the form and here alike, and a stored
spelling is read, never rewritten (F50, F75).

**What is checked (Naveen, 2026-10-12): the new types only.** An answer to an email,
date, phone or dropdown field must be one; text, long text and yes/no are taken as
they come, as they always were. A number is taken as a number since F1562 (Naveen
11-12): "3" sent through the API stayed text, and a later `> 2` failed its If/Else;
an answer that is no number is refused, naming the field.
A yes/no said in words is stored as the flag it says (F1434), a sign-off's approve or decline too (F1552); other words
stay as they came.
A field left blank is still an answer (F232's rule: it is what a form sends for a
question nobody filled in). A text answer to a fielded question is not checked
either; there is no field to check it against.

`check_answer` returns the answer with each checked value in its stored form (an
email trimmed, a date as `YYYY-MM-DD`, a dropdown's option as the step wrote it) and
raises `BadAnswer` naming the field otherwise. Every door that answers a run calls it
through `answer_paused_run` (`core/orchestration/answers.py`).
"""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Optional

#: Every type a field can be, in the order the step editor offers them.
FIELD_TYPES = ("string", "textarea", "number", "boolean", "email", "date", "phone", "choice")

#: Spellings written before this table, by hand or by a model.
ALIASES = {
    "text": "string",
    "str": "string",
    "long_text": "textarea",
    "bool": "boolean",
    "int": "number",
    "integer": "number",
    "float": "number",
    "select": "choice",
    "dropdown": "choice",
    "options": "choice",
    "enum": "choice",
    "tel": "phone",
    # The planner's own spelling of a yes/no (F1434), which the clarify form already read as one.
    "yes_no": "boolean",
    "yesno": "boolean",
    "yes/no": "boolean",
}

#: A yes/no answered in words (F1434): stored as the flag it says, so a condition that reads true/false meets it. A
#: sign-off's words count too (F1552, Naveen 11-12: "decline" sent through the API stayed text, and a plain
#: `if state.review.approved` read it as true). Only a Human step's field reads these: a tool call's own approval is
#: `engine._approved`'s, with its own words, unchanged.
_YES = frozenset({"yes", "y", "true", "1", "approve", "approved", "accept", "accepted"})
_NO = frozenset({"no", "n", "false", "0", "decline", "declined", "reject", "rejected"})

#: The types an answer is checked against.
CHECKED = frozenset({"email", "date", "phone", "choice"})

#: A domain is labels without dots joined by dots (F1472): one way to read it, so the check's time grows with the
#: answer's length. The domain's run used to take dots too, and a long dotted answer that failed at its end was tried
#: at every dot (CodeQL 9877). `a@b..c` is no longer taken; no mail server takes it either.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
#: The longest an address can be (RFC 5321's path limit): a longer answer is refused before the pattern runs.
_EMAIL_MAX = 254
_PHONE = re.compile(r"^\+?[0-9 ()\-.]+$")
_PHONE_MIN_DIGITS = 6


class BadAnswer(ValueError):
    """An answer to an email, date, phone or dropdown field that is not one."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(message)


def field_type(raw: Optional[str]) -> Optional[str]:
    """The type a field is, or None for one this table does not know. No type is text."""
    key = (raw or "").strip().lower()
    if not key:
        return "string"
    if key in FIELD_TYPES:
        return key
    return ALIASES.get(key)


def _said(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def named(fields: Any) -> Any:
    """`fields` with each one that has no name named by its `key` (F1557): how the shipped template packs spelt a
    field's name until then, which nothing read, so every field there had none and the form made its boxes one answer.
    A stored spelling is read, never rewritten: a field with a name keeps it, and the step as saved is unchanged."""
    if not isinstance(fields, list):
        return fields
    return [{**f, "name": f["key"]} if isinstance(f, dict) and not _said(f.get("name")) and _said(f.get("key"))
            else f for f in fields]


_COUNTED = {2: "two", 3: "three", 4: "four"}


def name_issues(step_id: str, key: str, fields: Any) -> list[str]:
    """What is wrong with a Human step's field names (F1557), repair first: a field with no name, or a name another
    field of the step has. The answer keeps one value per name, and the form one box's value per name, so a field
    with no name of its own loses its answer. The editor says the same (`humanFields.humanFieldNameIssues`)."""
    issues: list[str] = []
    counts: dict[str, int] = {}
    for i, field in enumerate(named(fields) if isinstance(fields, list) else [], 1):
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not _said(name):
            label = str(field.get("label") or "").strip()
            labelled = f" ('{label}')" if label else ""
            issues.append(f"Human step '{step_id}' field {i}{labelled} has no name: give it one — the answer keeps "
                          f"each field under its name, and a later step reads it as state.{key}.<name>.")
        else:
            counts[name] = counts.get(name, 0) + 1
    for name, count in counts.items():
        if count > 1:
            issues.append(f"Human step '{step_id}' has {_COUNTED.get(count, count)} fields named '{name}': give each "
                          f"a name of its own — the answer keeps one value per name, so the others are lost.")
    return issues


def field_options(field: dict[str, Any]) -> list[str]:
    """A dropdown's options, as the step wrote them, blanks left out."""
    options = field.get("options")
    if not isinstance(options, list):
        return []
    return [str(o).strip() for o in options if str(o).strip()]


def as_flag(value: Any) -> Any:
    """A yes/no answer as True or False when it says one in words; anything else as it came (never refused)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return value
    word = str(value).strip().rstrip(".!").strip().lower()
    if word in _YES:
        return True
    if word in _NO:
        return False
    return value


#: A number as it is typed: a sign, digits, one decimal point, an exponent (F1562).
_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
#: Thousands grouped by commas: 1,200 and 12,345.5, never 1,2.
_GROUPED = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")


def as_number(field: dict[str, Any], value: Any) -> Any:
    """A number field's answer as a number (F1562): "3" is 3, " 2.5 " is 2.5, "1,200" is 1200, a number as it came.
    A blank is still an answer (F232), as it came; anything else — words, true or false, a list, a number too big to
    hold — is `BadAnswer`, naming the field."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return value
    label = str(field.get("label") or "").strip() or str(field.get("name") or "")
    refused = BadAnswer(str(field.get("name") or ""), f"{label}: “{str(value).strip()[:60]}” is not a number.")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise refused
    if isinstance(value, str):
        text = value.strip()
        if _GROUPED.match(text):
            text = text.replace(",", "")
        if not _NUMBER.match(text):
            raise refused
        value = float(text) if any(c in text for c in ".eE") else int(text)
    if isinstance(value, float) and not math.isfinite(value):
        raise refused
    return value


def check_value(field: dict[str, Any], value: Any) -> Any:
    """One field's answer in its stored form, or `BadAnswer`."""
    kind = field_type(field.get("type"))
    if kind == "boolean":
        return as_flag(value)
    if kind == "number":
        return as_number(field, value)
    if kind not in CHECKED or value is None or (isinstance(value, str) and not value.strip()):
        return value
    name = str(field.get("name") or "")
    label = str(field.get("label") or "").strip() or name
    text = str(value).strip()
    if kind == "email":
        if len(text) > _EMAIL_MAX:
            raise BadAnswer(name, f"{label}: “{text[:60]}…” is not an email address.")
        if not _EMAIL.match(text):
            raise BadAnswer(name, f"{label}: “{text}” is not an email address.")
        return text
    if kind == "date":
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise BadAnswer(name, f"{label}: “{text}” is not a date. Use YYYY-MM-DD.") from None
    if kind == "phone":
        if not _PHONE.match(text) or sum(c.isdigit() for c in text) < _PHONE_MIN_DIGITS:
            raise BadAnswer(name, f"{label}: “{text}” is not a phone number.")
        return text
    options = field_options(field)
    if not options:
        # A dropdown saved with nothing to choose from: the editor refuses one, and
        # there is nothing here to hold an answer to.
        return value
    for option in options:
        if option.lower() == text.lower():
            return option
    raise BadAnswer(name, f"{label}: choose one of {', '.join(options)}.")


def check_answer(fields: Any, response: Any, *, form: bool = False) -> Any:
    """`response` with each checked field's value in its stored form; `BadAnswer` if one is not.

    Plain text to a step whose one field is a yes/no (a channel's reply, F1434) answers that field when it says yes
    or no; any other text is the answer as it came.

    Text to a Human step's own form (`form`, F1561: the door says so, `answers.asks_a_form`) is read as its fields:
    a line each, `name: value` (`read_reply`). A form of two fields or more that the text names none of is refused,
    saying how; a lone One of takes a plain reply that is one of its options, a lone number one that is a number
    (F1562). A lone text field keeps the words as
    they came — the AI builder's own questions are answered so — and so does what is no form (a tool's approval).
    """
    named_fields = [f for f in named(fields) if isinstance(f, dict) and _said(f.get("name"))] \
        if isinstance(fields, list) else []
    if isinstance(response, str) and form and named_fields and not _a_tools_approval(named_fields):
        typed = len(named_fields) > 1 or field_type(named_fields[0].get("type")) in _PLAIN_REPLY
        reply = read_reply(named_fields, response) if typed else None
        if reply:
            response = reply
        elif len(named_fields) > 1:
            raise BadAnswer("answer", how_to_answer(named_fields))
        elif typed and field_type(named_fields[0].get("type")) != "boolean":
            response = {named_fields[0]["name"]: response.strip()}
    if isinstance(response, str) and len(named_fields) == 1 and field_type(named_fields[0].get("type")) == "boolean":
        flag = as_flag(response)
        return {named_fields[0]["name"]: flag} if isinstance(flag, bool) else response
    if not isinstance(response, dict) or not isinstance(fields, list):
        return response
    checked = dict(response)
    for field in named_fields:
        if field["name"] in checked:
            checked[field["name"]] = check_value(field, checked[field["name"]])
    return checked


# ── A form in words: a channel's question and its reply (F1561) ───────────────────────────────────────────────────────

#: The lone fields a plain reply answers: one that says a yes or no (F1434), one of a One of's options, a number
#: (F1562).
_PLAIN_REPLY = frozenset({"boolean", "choice", "number"})
#: The fields a reply's lines run on into, after their own line: words.
_RUNS_ON = frozenset({"string", "textarea"})
#: `name: value`, `- Label = value`: a key of up to 120 characters before the first `:` or `=`.
_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?P<key>[^:=\n]{1,120}?)\s*[:=]\s*(?P<value>.*)$")


def _a_tools_approval(fields: list[dict]) -> bool:
    """A tool call's own approval asks `approve` and `reason` (`engine._approved` reads its "yes"): no Human form,
    wherever it was asked (a nested run's comes up through its parent)."""
    return [f.get("name") for f in fields] == ["approve", "reason"]


def _key(text: str) -> str:
    return " ".join(re.sub(r"[`*_\-]+", " ", str(text)).lower().rstrip("?").split())


def what_it_takes(field: dict[str, Any]) -> str:
    """A field's type in words: "yes or no", "one of: A, B", "a number", "text", …"""
    kind = field_type(field.get("type"))
    if kind == "boolean":
        return "yes or no"
    if kind == "choice":
        return f"one of: {', '.join(field_options(field))}"
    return {"number": "a number", "email": "an email address", "date": "a date, YYYY-MM-DD",
            "phone": "a phone number"}.get(kind or "", "text")


def asked_in_words(prompt: str, fields: Any) -> str:
    """A Human step's question with its fields, as a channel asks it (F1561): Markdown its formatter makes the
    platform's own. Each field's name, its label and what it takes, and how to answer (a line each, `name: value`);
    a lone One of, Yes / No or number as one reply. A question with no typed field is its words alone, as before."""
    shown = [f for f in named(fields) if isinstance(f, dict) and _said(f.get("name"))] \
        if isinstance(fields, list) else []
    if not shown or _a_tools_approval(shown):
        return prompt
    if len(shown) == 1:
        field = shown[0]
        if field_type(field.get("type")) not in _PLAIN_REPLY:
            return prompt
        label = str(field.get("label") or "").strip() or field["name"]
        return f"{prompt}\n\nReply with {what_it_takes(field)} — {label}{'' if label[-1] in '?.!' else '.'}"
    lines = [f"- `{f['name']}` — {str(f.get('label') or '').strip() or f['name']} ({what_it_takes(f)})" for f in shown]
    return f"{prompt}\n\nAnswer each on its own line, as `name: value`:\n" + "\n".join(lines)


def read_reply(fields: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """The fields a reply names, a line each (`name: value`, by name or label, `:` or `=`, a bullet before it), or
    None when it names none. A text field runs on over the lines after it until the next field; lines before the first
    field are not an answer (a greeting)."""
    keys: dict[str, dict[str, Any]] = {}
    for field in fields:
        keys.setdefault(_key(field["name"]), field)
        if _said(field.get("label")):
            keys.setdefault(_key(field["label"]), field)
    found: dict[str, Any] = {}
    current: dict[str, Any] | None = None
    for line in str(text).splitlines():
        match = _LINE.match(line)
        field = keys.get(_key(match["key"])) if match else None
        if field is not None:
            found[field["name"]] = match["value"].strip()
            current = field
        elif current is not None and field_type(current.get("type")) in _RUNS_ON:
            found[current["name"]] = f"{found[current['name']]}\n{line}".strip()
    return found or None


def how_to_answer(fields: list[dict[str, Any]]) -> str:
    """What a reply that names no field is told (F1561)."""
    said = ", ".join(f"{f['name']} ({what_it_takes(f).replace('one of: ', 'one of ')})" for f in fields)
    return (f"This question asks {len(fields)} things: answer each on its own line, as name: value — {said}.")
