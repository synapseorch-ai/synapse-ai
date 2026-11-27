"""What a model-filled Tool step may send: only what fits the tool, and only addresses the run already holds (F1743).

A Tool step with no arguments set has a model write them, from the step's prompt and its tool's schema. Without a
check, a send step's model that never sees the approved address can write `to: []`, be refused, and on retry, told
only the relay's rule, invent an address that fits: what goes out is not what the person approved. So on every
attempt, before the call:

* **The arguments fit the tool's schema** (`misfits`): what it requires is there (a text not blank), and each value
  is of the type it declares. A misfit goes back to the model as the attempt's error, and nothing reaches the tool.
* **Every address in them is one the run holds** (`unseen`): each email, the host of each link (not for a tool that
  only reads), and a phone number. The run holds what is in its state, what its Human steps showed a person
  (`record_shown`, kept as values only), and the step's own prompt. Never the tool's description or schema (an
  example there is what a model copies), an earlier attempt's error, or a value a person declined. One it does not
  hold stops the step on the approval card (`card`), naming it: never sent, never tried again.

A person who declines and writes the right address in their reason gets one corrected attempt (`correction`): the
model is shown their words, and the corrected call is shown on the card again before anything goes out.

Arguments set on the step are rendered from state (E1) and checked when the plan is built (`builder_guard`); a call a
person approved is the call they saw. Neither is checked here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from core.native_builder import addresses as A

#: The values each Human step's card showed, by its step id, as `given` keys (emails, hosts, phone digits): what the
#: guard compares, never the words. Private to the engine (a leading `_`), so no step's prompt reads it.
SHOWN_KEY = "_shown_to_person"
#: A guard pause's own record, in the step's progress: what it named, what a person declined, and whether the call
#: on the card is a corrected one.
GUARD_KEY = "unseen_guard"

#: A property whose whole value is a phone number by its name or its description.
_PHONE_WORDS = re.compile(r"phone|mobile|\btel\b|telephone|sms|whatsapp", re.I)
#: The schema rules held: a required name, a type. Never a `format`, a bound, an `enum` or `additionalProperties`:
#: servers take extras and coerce, and holding those would fail calls that work.
_HELD = ("required", "type")


@dataclass(frozen=True)
class Value:
    """An address in a call's arguments: what it is said as, its kind, and the key it is known by (`addresses`)."""
    said: str
    kind: str
    key: str


def known(texts: Iterable[Any]) -> set[str]:
    """The keys of every address in `texts`: the one extraction for what is recorded and what is compared."""
    return A.given(texts)


def _phone(value: str, about: str) -> Value | None:
    value = value.strip()
    if not A._WHOLE_PHONE.match(value) or len(A._digits(value)) < 7:
        return None
    if value.startswith("+") or _PHONE_WORDS.search(about):
        return Value(value, "phone", A._digits(value))
    return None


#: A property that says where a call goes, by its name (F1744): an agent's link is held only there. A link written
#: inside a message's words sends nothing anywhere.
_DESTINATION = re.compile(r"(^|_)(url|uri|endpoint|webhook|link|callback)s?($|_)", re.I)


def values_in(arguments: Any, schema: Any = None, *, reads_only: bool = False, links: str = "all") -> list[Value]:
    """Each address in a model's `arguments`, once: emails anywhere in a value, and a whole value that is a phone
    number where it starts with "+" or its property says phone. Links by host: anywhere (`links="all"`), none
    (`"none"`, or a tool that only reads), or only where the property says where the call goes (`"destinations"`: a
    name like url, endpoint or webhook, or `format: uri`). A property's name and description come from `schema`."""
    found: list[Value] = []
    if reads_only:
        links = "none"

    def props_of(node: Any) -> dict:
        return node.get("properties") if isinstance(node, dict) and isinstance(node.get("properties"), dict) else {}

    def holds_links(node: Any, name: str) -> bool:
        if links == "all":
            return True
        if links == "none":
            return False
        return bool(_DESTINATION.search(name)) or (isinstance(node, dict) and node.get("format") == "uri")

    def walk(value: Any, node: Any, about: str, name: str) -> None:
        if isinstance(value, str):
            for a in A._in_words(value, ""):
                if a.kind == "url" and not holds_links(node, name):
                    continue
                found.append(Value(a.value, a.kind, a.key))
            if (p := _phone(value, about)) is not None:
                found.append(p)
        elif isinstance(value, dict):
            props = props_of(node)
            for k, v in value.items():
                sub = props.get(k) if isinstance(props.get(k), dict) else {}
                walk(v, sub, f"{k} {sub.get('description') or ''}", str(k))
        elif isinstance(value, list):
            items = node.get("items") if isinstance(node, dict) and isinstance(node.get("items"), dict) else {}
            for v in value:
                walk(v, items, about, name)

    walk(arguments, schema if isinstance(schema, dict) else {}, "", "")
    seen: set[str] = set()
    return [v for v in found if not (v.key in seen or seen.add(v.key))]


def _blank(value: Any) -> bool:
    """A required text left blank: what a relay refuses and the schema lets through. Never an empty list or object,
    which a tool may well take (an agent made with no tools is `"tools": []`); another type is the type check's."""
    return isinstance(value, str) and not value.strip()


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    return {str: "a string", list: "an array", dict: "an object"}.get(type(value), "null")


def misfits(tool: str, schema: Any, arguments: Any) -> list[str]:
    """What in a model's `arguments` does not fit the tool's `schema`, in words the model is shown as its error: a
    required input left out or a required text left blank, a value of another type. A schema that is not one, or a
    reference it cannot follow, says nothing (`builder_guard._misfits`'s rule)."""
    if not isinstance(schema, dict) or not isinstance(arguments, dict):
        return [] if isinstance(arguments, dict) else [f"{tool}'s arguments must be an object"]
    try:
        import referencing
        from jsonschema.exceptions import SchemaError
        from jsonschema.validators import validator_for
    except ImportError:  # mcp's own dependency; without it there is nothing to hold them to
        return []
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        errors = list(cls(schema, registry=referencing.Registry()).iter_errors(arguments))
    except SchemaError:
        return []
    except Exception:  # noqa: BLE001 — a reference it cannot follow is not evidence either way
        return []
    said: list[str] = []
    for error in errors:
        if error.validator not in _HELD:
            continue
        where = ".".join(str(p) for p in error.absolute_path)
        if error.validator == "required" and isinstance(error.instance, dict):
            left = [r for r in error.validator_value if r not in error.instance]
            said.append(f"{tool} needs {', '.join(repr(r) for r in left)}" + (f" in '{where}'" if where else ""))
        elif error.validator == "type":
            wanted = error.validator_value if isinstance(error.validator_value, list) else [error.validator_value]
            said.append(f"{tool}'s input '{where or 'arguments'}' must be {' or '.join(map(str, wanted))}, "
                        f"not {_json_type(error.instance)}")
    # Required and there, but blank.
    for name in schema.get("required") or []:
        if name in arguments and _blank(arguments[name]):
            said.append(f"{tool}'s input '{name}' is required and cannot be blank")
    return list(dict.fromkeys(said))


def reads_only(tool_obj: Any, own: dict | None) -> bool:
    """Whether the tool only reads: a custom tool by its tags, else an HTTP tool by its method (`may_repeat`'s rule);
    any other by the read-only hint it declares. A tool that declares nothing is not taken to only read."""
    from core.tool_tags import defaults_for, tags_of
    from core.tools import tool_hints

    if own is not None:
        tags = tags_of(own.get("annotations"))
        if tags is None:
            if (own.get("tool_type") or "http") != "http":
                return False
            tags = defaults_for(own.get("method"))
        return bool(tags.get("read_only"))
    hints = tool_hints(tool_obj)
    return bool(hints.declared and hints.read_only)


def held(state: dict, prompt: str, *, extra: Iterable[Any] = ()) -> set[str]:
    """What the run holds for this step: its state's values (a saved output read whole), every value its Human steps
    showed (`SHOWN_KEY`), the step's own prompt, and `extra` (a person's reason, on a corrected attempt). An
    engine-private key (a leading `_`) is not the run's data: `_run_failures` keeps earlier errors, which can repeat
    a model's own made-up value."""
    from core.step_refs import resolved

    texts: list[Any] = [prompt, *extra]
    for key, value in (state or {}).items():
        if str(key).startswith("_"):
            continue
        try:
            texts.append(resolved(value))
        except Exception:  # noqa: BLE001 — a saved output that cannot be read holds nothing here
            continue
    keys = known(texts)
    for shown in ((state or {}).get(SHOWN_KEY) or {}).values():
        keys |= {k for k in shown or [] if isinstance(k, str)}
    return keys


def unseen(values: list[Value], keys: set[str], declined: Iterable[str] = ()) -> list[Value]:
    """The values the run does not hold, or a person declined: a no is never a source again."""
    no = set(declined)
    return [v for v in values if v.key not in keys or v.key in no]


#: At most this many values kept for a run, over all its cards: values, never drafts, so a long loop stays small.
SHOWN_CAP = 500


def record_shown(state: dict, step_id: str, prompt: Any, fields: Any) -> None:
    """Keep what a Human step's card showed, as the values the guard compares, by its step id, added to what its
    earlier cards showed: a loop that approves twenty recipients, then a send after it that mails them all, holds all
    twenty. At `SHOWN_CAP` values for the run the first are kept, and the rest are not, said in the log."""
    new = known([prompt, fields])
    shown = {k: list(v) for k, v in (state.get(SHOWN_KEY) or {}).items() if isinstance(v, list)}
    have = {k for v in shown.values() for k in v}
    room = SHOWN_CAP - len(have)
    adding = sorted(new - have)
    if len(adding) > room:
        from core.log import get_logger

        get_logger(__name__).debug("shown values for this run at %d: %d from step %s not kept",
                                   SHOWN_CAP, len(adding) - max(room, 0), step_id)
        adding = adding[:max(room, 0)]
    if adding:
        shown[str(step_id)] = sorted({*shown.get(str(step_id), []), *adding})
    if shown:
        state[SHOWN_KEY] = shown


def _listed(said: list[str]) -> str:
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]


#: How a person sends it somewhere else: the card offers only Approve and Decline, so it says what a Decline can do.
CORRECT_LINE = ("To send it somewhere else, decline and write the right address in your reason: you'll see the "
                "corrected send before anything goes out.")


def card(tool: str, missing: list[Value], *, corrected: bool, policy_words: str = "") -> str:
    """The approval card's words for a guard pause: what the call would use that the run does not hold, or, after a
    person's correction, that this is the corrected call to check. `policy_words` is the tool policy's own ask when
    it asks too: one pause says both."""
    if corrected:
        head = (f"Here is **{tool}** again, written from what you said. Check it: nothing goes out until you "
                "approve it.")
        if missing:
            head += (f" It still uses {_listed([v.said for v in missing])}, which isn't anywhere in this run or "
                     "what you wrote.")
    else:
        head = (f"**{tool}** would use {_listed([v.said for v in missing])}, which isn't anywhere in this run: not "
                "in what it was given, what its steps found or what was shown for approval. Send it as it is?")
    return " ".join(p for p in (head, CORRECT_LINE, policy_words) if p)


def declined_keys(arguments: Any, before: Iterable[str] = ()) -> set[str]:
    """Every address in a call a person declined, as `known` reads it (an email's domain with it), and those declined
    before: a reason that only repeats one gives nothing new."""
    return set(before) | known([arguments])


def correction(reason: str, declined: Iterable[str]) -> set[str]:
    """The addresses a person's reason gives that they did not just decline (an email, a host, a phone's digits; never
    a handle): none, and a Decline stays a refusal."""
    no = set(declined)
    return {k for k in known([reason])
            if k not in no and not k.startswith("@") and ("@" in k or "." in k or k.isdigit())}


def correction_prompt(arguments: Any, reason: str) -> str:
    return ("A person declined this call: " + json.dumps(arguments, ensure_ascii=False, default=str)
            + f"\nThey said: {reason}\nWrite the call again with what they said. Use only what they wrote or what "
              "this run already holds; never make up an address.")


# ── An agent's own calls (F1744) ──────────────────────────────────────────────────────────────────────────────────
# An agent writes each tool call itself, in a chat, an API or scheduled turn, a team, a channel or a workflow's Agent
# step, and the same made-up address can go out through a call of its own. Held as a Tool step's are, with its own
# sources: what the person said (an assistant turn only once a person answered it), the agent's own instructions and
# memory, the run's state in a workflow, and what its tools answered without an error this turn. Never its own words
# a person never answered, a tool's description, an error, or what a person declined.


def seen_in_history(messages: Iterable[Any]) -> set[str]:
    """The keys of every address a conversation's person wrote, and of what the agent wrote that a person then
    answered: an assistant turn with no person's turn after it is the agent's own word, read by nobody, and in a
    scheduled or workflow turn it would ground its own invention."""
    turns = [m for m in messages or [] if isinstance(m, dict)]
    texts = []
    for at, m in enumerate(turns):
        role = m.get("role")
        if role == "user" or (role == "assistant" and any(n.get("role") == "user" for n in turns[at + 1:])):
            texts.append(m.get("content") if isinstance(m.get("content"), str)
                         else json.dumps(m.get("content"), default=str))
    return known(texts)


def agent_card(tool: str, missing: list[str], *, corrected: bool = False) -> str:
    """The approval card's words for an agent's call the guard stopped: what it would use that its turn does not
    hold, or, after a person's correction, that this is the corrected call to check."""
    if corrected:
        head = (f"The agent wants to run **{tool}** again, written from what you said. Check it: nothing goes out "
                "until you approve it.")
        if missing:
            head += f" It still uses {_listed(missing)}, which isn't anywhere in this conversation or what you wrote."
    else:
        head = (f"The agent wants to run **{tool}** with {_listed(missing)}, which isn't anywhere in this "
                "conversation, this run or what its tools found. Send it as it is?")
    return f"{head} {CORRECT_LINE}"


def agent_declined_line(tool: str, named: list[str], reason: str) -> str:
    """What an agent is told when a person declines a call the guard stopped: their words, and that a call with the
    address they gave is shown to them before it goes out."""
    said = f"{_listed(named)}" if named else "that call"
    return (f"\nSystem: the person declined sending '{tool}' to {said}"
            + (f": {reason}" if reason else "")
            + ". If they gave the right address, call it again with it: it is shown to them before it goes out. "
              "Never make up an address; ask them for it if they gave none.\n")


def agent_refusal_line(tool: str, missing: list[str]) -> str:
    """A turn where nobody can answer a card (a website visitor's, a channel's): the call is not run, and the agent is
    told why, in words that hold only what it wrote itself."""
    return (f"\nSystem: '{tool}' was not run: {_listed(missing)} isn't anywhere in this conversation or what your "
            "tools found. Never make up an address: ask the person for it.\n")
