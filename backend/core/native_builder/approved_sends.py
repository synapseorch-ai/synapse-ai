"""A send after an approval sends what the approval showed: its inputs set from that state, never left to a model
(F1743).

A Tool step with no `config.tool_arguments` has a model write its call at run time, from the step's own prompt, which
does not hold what a Human step before it showed. Planned "show me the reply, then send it", the send's model writes
who it goes to without seeing who was approved: what goes out need not be what the person approved. The run holds a
call to addresses the run holds (`orchestration.grounded`); here the plan is asked, once, to set the send's inputs from
what the approval showed, as `{{state.<step id>.<field>}}`, and F1402's checks hold what it sets to the tool's schema.

The shape: a Human step, then, through decisions only (if_else, switch, evaluator), a Tool step a model fills whose
tool takes an address (an input named or described as a recipient, an email or a phone) and does more than read. A
tool that only reads (a look-up by email) sends to no one, as `grounded` does not hold a read's links. Its lines name
only what the Human step showed: no mapping is guessed, the planner knows its own fields.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from core.native_builder import objects as O
from core.templating import STATE_REF

#: Said once a run: the shape is sent back once, then said on the card.
SAID = "_approved_send_said"
#: The steps a person's answer leads on through to the step it decides.
_DECIDES = ("if_else", "switch", "evaluator")
_ADDRESS_NAME = re.compile(r"^(to|cc|bcc|recipients?|e-?mails?|e-?mail_address(es)?|\w+_e-?mails?|phone(_number)?|"
                           r"\w+_phone(_number)?|mobile|whatsapp)$", re.I)
_ADDRESS_SAID = re.compile(r"\b(e-?mail address(es)?|recipients?|phone number|mobile number|who it goes to)\b", re.I)
_ADDRESS_FIELD = re.compile(r"e-?mail|phone|mobile|recipient|address", re.I)


@dataclass(frozen=True)
class Found:
    send: O.Step
    approval: O.Step
    address: str
    shown: tuple[str, ...]

    def sendback(self) -> str:
        refs = ", ".join("{{state.%s}}" % r for r in self.shown)
        where = (f"from what that step showed: {refs}" if self.shown else
                 "from what earlier steps wrote, as {{state.<step id>.<field>}}")
        pick = next((r for r in self.shown if _ADDRESS_FIELD.search(r.rsplit(".", 1)[-1])), None)
        example = f', e.g. "{self.address}": "{{{{state.{pick}}}}}"' if pick else ""
        return (f'step {self.send.id}: it sends after "{self.approval.name}" is approved, but a model would write its '
                f"inputs, and it never sees what was approved. Set every input in its `config.tool_arguments` {where}"
                f"{example}.")

    def card(self) -> str:
        return (f'"{self.send.name}" sends after "{self.approval.name}", and a model writes who it goes to: the run '
                "stops and asks if that's an address the run doesn't hold.")


def _shown(step: O.Step) -> tuple[str, ...]:
    """Every `{{state.…}}` a Human step's card shows: its question and its fields' words."""
    config = step.config or {}
    texts = [step.prompt or "", str(config.get("human_prompt") or ""),
             *(str(f.get(k) or "") for f in config.get("human_fields") or [] if isinstance(f, dict)
               for k in ("label", "default", "description"))]
    return tuple(dict.fromkeys(r for t in texts for r in STATE_REF.findall(t)))


def address_input(schema: Any) -> str | None:
    """The input of a tool's `schema` that takes an address, by its name or what it says it is; None if none does."""
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return None
    for name, prop in props.items():
        said = str(prop.get("description") or "") if isinstance(prop, dict) else ""
        if _ADDRESS_NAME.match(str(name)) or _ADDRESS_SAID.search(said):
            return str(name)
    return None


def _after_approvals(steps: list[O.Step]) -> list[tuple[O.Step, O.Step]]:
    """Each (Human step, Tool step a model fills) where the second follows the first through decisions only."""
    by_id = {s.id: s for s in steps}
    pairs = []
    for h in steps:
        if h.type != "human":
            continue
        seen: set[str] = set()
        todo = [n for n in (h.next, *(h.branches or {}).values()) if n]
        while todo:
            sid = todo.pop()
            if sid in seen or sid not in by_id:
                continue
            seen.add(sid)
            s = by_id[sid]
            if s.type in _DECIDES:
                todo += [n for n in (s.next, *(s.branches or {}).values()) if n]
            elif O.fills_in(s) and s.tool:
                pairs.append((h, s))
    return pairs


async def _what_it_takes(name: str, plan: O.Plan, ws: Any, server_module: Any) -> tuple[Any, bool]:
    """A tool's input schema and whether it only reads: a tool the build makes by its call's method, one here by its
    tags or the read-only hint it declares (`grounded.reads_only`, Part 1's rule for links)."""
    from core.native_builder.build_save import made_schema

    made = next((t for t in plan.tools if t.name == name and t.status == "found" and t.via == "http_tool" and t.api),
                None)
    if made is not None:
        method = str(((made.api or {}).get("endpoint") or {}).get("method") or "GET").upper()
        return made_schema(made), method in ("GET", "HEAD")
    schema = (ws.takes or {}).get(name)
    if schema is None:
        return None, False
    try:
        from core.orchestration.grounded import reads_only
        from core.routes.tools import load_custom_tools
        from core.tools import resolve_one_tool

        custom = await load_custom_tools()
        own = next((c for c in custom if isinstance(c, dict) and c.get("name") == name), None)
        tool = None if own is not None else await resolve_one_tool(server_module, name, {"tools": [name]}, custom)
        return schema, reads_only(tool, own)
    except Exception:  # noqa: BLE001 — not knowing whether it reads is held as a send, the safer side
        return schema, False


async def found_in(plan: O.Plan, ws: Any, server_module: Any) -> list[Found]:
    """Every send after an approval that a model would fill, in the plan's workflow and its inner ones."""
    out = []
    for steps in (plan.steps, *(w.steps for w in plan.inner_workflows)):
        for h, s in _after_approvals(list(steps)):
            schema, reads = await _what_it_takes(s.tool or "", plan, ws, server_module)
            if reads or (address := address_input(schema)) is None:
                continue
            out.append(Found(s, h, address, _shown(h)))
    return out


# ── A field a send reads that starts empty (F1753) ──────────────────────────────────────────────────────────────────
#
# A Human step's field can start with a value (`default`, filled in from the run when it asks). One a later send sets
# an input from that starts empty is a blank the person must fill before the yes: right for an address nothing
# earlier found, wrong for a draft the step shows above it ("edit the fields below if needed", every field blank, the
# send failing after the yes). The plan is asked once which it means; kept empty, the card says the person fills it in.

#: Said once a run, as `SAID` is.
EMPTY_SAID = "_empty_start_said"


@dataclass(frozen=True)
class EmptyStart:
    approval: O.Step
    send: O.Step
    field: dict

    def sendback(self) -> str:
        return (f'step {self.approval.id}: its field `{self.field["name"]}` starts empty, and "{self.send.name}" sends '
                'it. Give it a starting value from the step that wrote it, as `"default": "{{state.<step id>.<field>}}"`,'
                " or leave it empty if the person types it.")

    def card(self) -> str:
        label = str(self.field.get("label") or "").strip().rstrip("?:") or self.field["name"]
        return f'"{self.approval.name}" asks the person to fill in {label} before "{self.send.name}" sends it.'


def _set_after_approvals(steps: list[O.Step]) -> list[tuple[O.Step, O.Step]]:
    """Each (Human step, Tool step with its inputs set) where the second follows the first through decisions only."""
    by_id = {s.id: s for s in steps}
    pairs = []
    for h in steps:
        if h.type != "human":
            continue
        seen: set[str] = set()
        todo = [n for n in (h.next, *(h.branches or {}).values()) if n]
        while todo:
            sid = todo.pop()
            if sid in seen or sid not in by_id:
                continue
            seen.add(sid)
            s = by_id[sid]
            if s.type in _DECIDES:
                todo += [n for n in (s.next, *(s.branches or {}).values()) if n]
            elif s.type == "tool" and s.tool and isinstance((s.config or {}).get("tool_arguments"), dict):
                pairs.append((h, s))
    return pairs


async def empty_starts(plan: O.Plan, ws: Any, server_module: Any) -> list[EmptyStart]:
    """Every field of an approval that a later send sets an input from and that starts empty: never the approval's
    own Yes / No, and never for a tool that only reads."""
    import json

    from core.orchestration.human_fields import approval_field, named

    out: list[EmptyStart] = []
    said: set[tuple[str, str]] = set()
    for steps in (plan.steps, *(w.steps for w in plan.inner_workflows)):
        for h, s in _set_after_approvals(list(steps)):
            _, reads = await _what_it_takes(s.tool or "", plan, ws, server_module)
            if reads:
                continue
            config = h.config or {}
            fields = [f for f in named(config.get("human_fields") or []) if isinstance(f, dict) and f.get("name")]
            decision = approval_field(fields)
            keys = {h.id, str(config.get("output_key") or h.id)}
            read = {parts[1] for ref in STATE_REF.findall(json.dumps(s.config["tool_arguments"], default=str))
                    if len(parts := ref.split(".")) > 1 and parts[0] in keys}
            for f in fields:
                if f is decision or "default" in f or f["name"] not in read or (h.id, f["name"]) in said:
                    continue
                said.add((h.id, f["name"]))
                out.append(EmptyStart(h, s, f))
    return out


# ── What a send after an approval sends, shown by it (F1754) ────────────────────────────────────────────────────────
#
# A send whose inputs are set passes F1743, whatever its approval shows: "Here is the email drafted. Please review and
# approve or decline", with no value of the draft in it, and the draft goes out unseen. So every value such a send sets
# from an earlier step, who it goes to and what it says, is one the approval shows: in its words, as a field's
# starting value (F1753), or through the value that holds it. An id is not a thing a person approves.

#: Said once a run, as `SAID` is.
UNSHOWN_SAID = "_unshown_send_said"
#: An input that names a thing rather than says something: never asked to be shown.
_AN_ID = re.compile(r"(^|_)(id|ids|uuid|key)$", re.I)


@dataclass(frozen=True)
class Unshown:
    send: O.Step
    approval: O.Step
    refs: tuple[str, ...]
    inputs: tuple[str, ...]
    address: str | None

    def sendback(self) -> str:
        refs = ", ".join("{{state.%s}}" % r for r in self.refs)
        them = "it" if len(self.refs) == 1 else "them"
        name = self.inputs[0]
        example = "%s: {{state.%s}}" % (name.replace("_", " ").capitalize(), self.refs[0])
        return (f'step {self.send.id}: it sends {refs} after "{self.approval.name}" is approved, but that step never '
                f"shows {them}, so the person would approve what they never saw. Show each in its words, e.g. "
                f'"{example}", or start a field with it as its `default`.')

    def card(self) -> str:
        said = ["who it goes to" if i == self.address else f"the {i.replace('_', ' ')}" for i in self.inputs]
        listed = said[0] if len(said) == 1 else f"{', '.join(said[:-1])} and {said[-1]}"
        return f'"{self.approval.name}" is approved without showing {listed} "{self.send.name}" sends.'


def _says_something(name: str, prop: Any) -> bool:
    """An input a person would judge: text that is not an id, a choice or a formatted value (a date, a link)."""
    if _AN_ID.search(name):
        return False
    if not isinstance(prop, dict):
        return True
    return prop.get("type") in (None, "string") and "enum" not in prop and "format" not in prop


async def unshown_in(plan: O.Plan, ws: Any, server_module: Any) -> list[Unshown]:
    """Every send after an approval, its inputs set, that sends a value from an earlier step its approval never
    shows: never a tool that only reads, never what the person answered on the approval itself."""
    import json

    out: list[Unshown] = []
    for steps in (plan.steps, *(w.steps for w in plan.inner_workflows)):
        for h, s in _set_after_approvals(list(steps)):
            schema, reads = await _what_it_takes(s.tool or "", plan, ws, server_module)
            if reads or not isinstance(schema, dict):
                continue
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            address = address_input(schema)
            own = {h.id, str((h.config or {}).get("output_key") or h.id)}
            shown = _shown(h)
            refs: list[str] = []
            inputs: list[str] = []
            for name, value in (s.config or {}).get("tool_arguments", {}).items():
                if name != address and not _says_something(name, props.get(name)):
                    continue
                for ref in STATE_REF.findall(json.dumps(value, default=str)):
                    if ref.split(".")[0] in own or any(ref == x or ref.startswith(x + ".") for x in shown):
                        continue
                    if ref not in refs:
                        refs.append(ref)
                    if name not in inputs:
                        inputs.append(name)
            if refs:
                out.append(Unshown(s, h, tuple(refs), tuple(inputs), address))
    return out


async def issues(plan: O.Plan, ws: Any, state: dict, server_module: Any, notes: list[str] | None) -> list[str]:
    """Sent back once a run, then said on the card while the plan keeps the shape: never a block. Each rule here —
    a model-filled send after an approval (F1743), a field a send reads that starts empty (F1753), a value a send
    sends that its approval never shows (F1754) — is asked once."""
    out: list[str] = []
    for key, found in ((SAID, await found_in(plan, ws, server_module)),
                       (EMPTY_SAID, await empty_starts(plan, ws, server_module)),
                       (UNSHOWN_SAID, await unshown_in(plan, ws, server_module))):
        if not found:
            continue
        if not state.get(key):
            state[key] = True
            out += [f.sendback() for f in found]
        elif notes is not None:
            notes += [f.card() for f in found]
    return out
