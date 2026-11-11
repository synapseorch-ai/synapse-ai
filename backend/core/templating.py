"""`{{ … }}` references, rendered the same way everywhere.

One syntax for the three places a person types a value that is not yet known:
a custom HTTP tool's URL, headers and body; an agent's system prompt; an
orchestration step's prompt template. Double braces with a namespace:

    {{ global.api_base }}     an organisation-wide variable (`global_variables`)
    {{ state.topic }}         orchestration shared state (beside the older `{state.topic}`)
    {{ body }}  {{ body.data }}   inside a tool: an input argument, or a path into one
    {{ args }}                inside a tool: every argument, as JSON

Inside a tool a bare name resolves against the tool's arguments, because that
is what a person writing `{{body}}` means; everywhere else a bare name is left
alone. An unknown reference is **left as written, never blanked** — a prompt
that says `{{global.tone}}` before the variable exists should show the gap,
not hide it. A workflow step's prompt, which a model reads, says the gap in
words instead, and reads a state key no step has written yet as empty (U74,
F1536: `orchestration.context._fill_task_template`); a Human or a Print step's
text keeps it as written. Scalars render as text, objects and lists as JSON.

The single-brace forms the engine already understood keep working: `{param}`
and `{data.key}` in URLs, `{state.key}` in prompts. This module only adds the
double-brace family and the body modes; it does not rewrite the old ones.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from core.step_refs import is_ref, resolved

REF = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")

#: A reference to the run's state, in the one form every renderer reads (F1252, Naveen 10-30): `{{ state.a.b }}`,
#: spaced or not. Group 1 is the path after `state.`.
STATE_REF = re.compile(r"\{\{\s*state\.([\w][\w.\-]*)\s*\}\}")
#: The older single-brace form, `{state.a.b}`, standing on its own (never the inside of a `{{ … }}`): what `one_form`
#: rewrites, and nothing else reads.
_OLD_STATE_REF = re.compile(r"(?<!\{)\{state\.([\w][\w.]*)\}(?!\})")
#: A step's fields that are templates over state, as text, or as text inside a mapping (F1252).
TEMPLATE_FIELDS = ("prompt_template", "evaluator_prompt", "print_content", "human_prompt")
TEMPLATE_MAPPINGS = ("tool_arguments", "recording_inputs", "retrieve", "ingest")

#: The namespaces a reference may start with. Anything else is a bare name.
NAMESPACES = ("global", "state", "args")

#: How deep JSON text a step reads a field of may nest (F1164). Counted, not left to `json.loads`: where it runs out
#: of the C stack depends on the stack's size (an 8 MB stack refuses 100,000 levels, a 16 MB one parses them).
#: 3.11's scanner counts against the recursion limit with the caller's frames, ~992 less the stack (F1436).
MAX_JSON_DEPTH = 512

#: A JSON string (so the braces in it are not depth) or one bracket.
_JSON_DEPTH_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]')


def loads_within_depth(text: str) -> Any:
    """`json.loads(text)`, or ValueError when the text nests deeper than `MAX_JSON_DEPTH`."""
    if text.count("{") + text.count("[") > MAX_JSON_DEPTH:
        depth = 0
        for m in _JSON_DEPTH_TOKEN.finditer(text):
            token = m.group()
            if token in ("{", "["):
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise ValueError(f"JSON nested more than {MAX_JSON_DEPTH} deep")
            elif token in ("}", "]"):
                depth -= 1
    return json.loads(text)


def _walk(root: Any, path: list[str], *, keep_ref: bool = False) -> tuple[bool, Any]:
    """Follow `path` into dicts (and list indexes). `(found, value)`.

    A step into text that holds a JSON object reads the object (F801): a builder tool's output is JSON text in
    state, and a prompt names a field of it as a condition or a set argument does. The text is parsed, never
    rendered: nothing in it is read as a template.

    A step output saved to the vault (`core.step_refs`, D339 P4) is read as what it was, on the way and at the end;
    `keep_ref` leaves the reference itself at the end, for a reader that shows it as a file (a model's prompt).
    """
    cur = root
    for part in path:
        if is_ref(cur):
            cur = resolved(cur)
        if isinstance(cur, str) and cur.lstrip().startswith("{"):
            try:
                parsed = loads_within_depth(cur)
            except (ValueError, RecursionError):  # nested too deep to read is no object either
                parsed = None
            if isinstance(parsed, dict):
                cur = parsed
        if isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    if is_ref(cur) and not keep_ref:
        cur = resolved(cur)
    return True, cur


def _text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if value is None:
        return ""
    return str(value)


def lookup(reference: str, context: dict[str, Any], *, bare: str | None = None,
           keep_ref: bool = False) -> tuple[bool, Any]:
    """Resolve one reference (without braces) against `context`.

    `context` maps namespace → mapping. `bare` names the namespace a
    reference without one resolves in (a tool's arguments), or None to leave
    bare names unresolved.

    **`{{args}}` with no path is the whole argument payload**, which is what the
    single-brace `{data}` has always meant and the only thing that had no
    double-brace spelling. Adding it is what makes "one syntax" true rather than
    true-with-a-footnote.

    Only `args`. A bare `{{global}}` would render every one of an organisation's
    variables — including, on a hosted deployment, the ones backed by secrets —
    into whatever URL or prompt it appeared in. That is not a shorthand anybody
    wants by accident, so a bare namespace resolves for `args` and nothing else.
    """
    parts = reference.split(".")
    if len(parts) == 1 and parts[0] == "args" and "args" in context:
        return True, context["args"]
    if parts[0] in context and len(parts) > 1 and parts[0] in NAMESPACES:
        return _walk(context[parts[0]], parts[1:], keep_ref=keep_ref)
    if bare is not None and bare in context:
        return _walk(context[bare], parts, keep_ref=keep_ref)
    return False, None


def render(text: str, context: dict[str, Any], *, bare: str | None = None,
           refs: Callable[[dict], str] | None = None) -> str:
    """Substitute every `{{ … }}` in `text` it can resolve; leave the rest.

    A step output saved to the vault renders as what it was, unless `refs` is given: then a reference that is the
    whole value renders as `refs` says (a model's prompt shows the file it does not fit; D339 P4)."""
    if not text or "{{" not in text:
        return text

    def _sub(match: re.Match[str]) -> str:
        found, value = lookup(match.group(1), context, bare=bare, keep_ref=refs is not None)
        if found and refs is not None and is_ref(value):
            return refs(value)
        return _text(value) if found else match.group(0)

    return REF.sub(_sub, text)


def one_form(text: str) -> str:
    """`text` with every older `{state.a.b}` written `{{ state.a.b }}` (F1252). Twice is once: a `{{ … }}`, JSON
    (`{"state": …}`) and every other brace are left as they are."""
    if not isinstance(text, str) or "{state." not in text:
        return text
    return _OLD_STATE_REF.sub(lambda m: "{{ state." + m.group(1) + " }}", text)


def _one_form_value(value: Any) -> Any:
    if isinstance(value, str):
        return one_form(value)
    if isinstance(value, dict):
        return {k: _one_form_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_one_form_value(v) for v in value]
    return value


def one_form_step(step: Any) -> Any:
    """A step (as stored) with its templates in the one form (`one_form`): its text templates and the text inside its
    template mappings. Its code (`transform_code`) and its conditions are expressions, never templates: untouched."""
    if not isinstance(step, dict):
        return step
    out = dict(step)
    for key in TEMPLATE_FIELDS:
        if isinstance(out.get(key), str):
            out[key] = one_form(out[key])
    for key in TEMPLATE_MAPPINGS:
        if isinstance(out.get(key), (dict, list)):
            out[key] = _one_form_value(out[key])
    return out


def one_form_definition(doc: Any) -> Any:
    """A workflow definition (as stored, imported or exported) with every step's templates in the one form."""
    if not isinstance(doc, dict) or not isinstance(doc.get("steps"), list):
        return doc
    return {**doc, "steps": [one_form_step(s) for s in doc["steps"]]}


def references(text: str) -> list[str]:
    """Every `{{ … }}` reference in `text`, in order, once each."""
    seen: list[str] = []
    for match in REF.finditer(text or ""):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def render_mapping(values: dict[str, str], context: dict[str, Any], *, bare: str | None = None) -> dict[str, str]:
    """`render` over a header-style mapping's values."""
    return {k: render(v, context, bare=bare) if isinstance(v, str) else v for k, v in values.items()}


def request_body(template: str | None, args: dict[str, Any], context: dict[str, Any]) -> tuple[Any, set[str]]:
    """The JSON body an HTTP tool sends, and the argument keys it consumed.

    Three modes, decided by the template a person typed on the tool:

    * **empty** — the arguments themselves (the caller subtracts the ones the
      URL used); nothing consumed here.
    * **one reference**, `{{body}}` or `{{body.data}}` — that argument (or a
      path into it) *is* the body, as an object; its top-level key is consumed.
    * **anything else** — a JSON template; every `{{…}}` inside is substituted
      and the result parsed as JSON (a reference standing alone as a value
      becomes that value, not a quoted string). Consumes the top-level keys it
      referenced. If the rendered text is not JSON it is sent as a string.
    """
    template = (template or "").strip()
    if not template:
        return dict(args), set()

    full = context | {"args": args}
    single = REF.fullmatch(template)
    if single:
        found, value = lookup(single.group(1), full, bare="args")
        consumed = {single.group(1).split(".")[0]} if found and not single.group(1).startswith(tuple(f"{n}." for n in NAMESPACES)) else set()
        return (value if found else None), consumed

    consumed: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        ref = match.group(1)
        found, value = lookup(ref, full, bare="args")
        if not found:
            return match.group(0)
        if not ref.startswith(tuple(f"{n}." for n in NAMESPACES)):
            consumed.add(ref.split(".")[0])
        # Inside a JSON template a reference is spliced as JSON, so an object
        # stays an object and a string is quoted exactly once.
        return json.dumps(value)

    rendered = _JSON_REF.sub(_sub, template)
    try:
        return json.loads(rendered), consumed
    except json.JSONDecodeError:
        return render(template, full, bare="args"), consumed


#: A reference that may be standing where a JSON value goes — quoted or bare.
#: `"{{name}}"` and `{{name}}` both become the value; the quotes are dropped so a
#: string is not double-quoted and an object is not stringified.
_JSON_REF = re.compile(r'"?\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}"?')
