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


async def issues(plan: O.Plan, ws: Any, state: dict, server_module: Any, notes: list[str] | None) -> list[str]:
    """Sent back once a run, then said on the card while the plan keeps the shape: never a block."""
    found = await found_in(plan, ws, server_module)
    if not found:
        return []
    if not state.get(SAID):
        state[SAID] = True
        return [f.sendback() for f in found]
    if notes is not None:
        notes += [f.card() for f in found]
    return []
