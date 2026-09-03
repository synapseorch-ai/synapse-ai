"""`{{ … }}` references, rendered the same way everywhere.

One syntax for the three places a person types a value that is not yet known:
a custom HTTP tool's URL, headers and body; an agent's system prompt; an
orchestration step's prompt template. Double braces with a namespace:

    {{ global.api_base }}     an organisation-wide variable (`global_variables`)
    {{ state.topic }}         orchestration shared state (beside the older `{state.topic}`)
    {{ body }}  {{ body.data }}   inside a tool: an input argument, or a path into one

Inside a tool a bare name resolves against the tool's arguments, because that
is what a person writing `{{body}}` means; everywhere else a bare name is left
alone. An unknown reference is **left as written, never blanked** — a prompt
that says `{{global.tone}}` before the variable exists should show the gap,
not hide it. Scalars render as text, objects and lists as JSON.

The single-brace forms the engine already understood keep working: `{param}`
and `{data.key}` in URLs, `{state.key}` in prompts. This module only adds the
double-brace family and the body modes; it does not rewrite the old ones.
"""
from __future__ import annotations

import json
import re
from typing import Any

REF = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")

#: The namespaces a reference may start with. Anything else is a bare name.
NAMESPACES = ("global", "state", "args")


def _walk(root: Any, path: list[str]) -> tuple[bool, Any]:
    """Follow `path` into dicts (and list indexes). `(found, value)`."""
    cur = root
    for part in path:
        if isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if value is None:
        return ""
    return str(value)


def lookup(reference: str, context: dict[str, Any], *, bare: str | None = None) -> tuple[bool, Any]:
    """Resolve one reference (without braces) against `context`.

    `context` maps namespace → mapping. `bare` names the namespace a
    reference without one resolves in (a tool's arguments), or None to leave
    bare names unresolved.
    """
    parts = reference.split(".")
    if parts[0] in context and len(parts) > 1 and parts[0] in NAMESPACES:
        return _walk(context[parts[0]], parts[1:])
    if bare is not None and bare in context:
        return _walk(context[bare], parts)
    return False, None


def render(text: str, context: dict[str, Any], *, bare: str | None = None) -> str:
    """Substitute every `{{ … }}` in `text` it can resolve; leave the rest."""
    if not text or "{{" not in text:
        return text

    def _sub(match: re.Match[str]) -> str:
        found, value = lookup(match.group(1), context, bare=bare)
        return _text(value) if found else match.group(0)

    return REF.sub(_sub, text)


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
