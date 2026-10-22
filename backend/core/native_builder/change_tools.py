"""Synapse AI's change route, in code (SAI2-P2, D308; F793, F794, F796, F797).

One model step writes language and judgement once, into a typed `Change`; code does every fact (DESIGN.md,
"Principle"). The route, as workflow steps:

1. `read_change_context` (E1): what the conversation's board card holds — the last change applied (for "undo that")
   and a which-one question waiting (its pick list).
2. The change planner (an agent, E2) finishes on exactly one of:
   * `submit_change`: ops by id or by name (`change_ops.OPS`);
   * `ask_person`: a question only the person can answer (which one, what time, what value);
   * `cannot`: the request is outside what the product does, and the nearest thing that does (P0's can't-do exit).
   Each checks what the model wrote and answers an error for the model to fix (E2 retries on `{"error": …}`).
3. `check_change` (E1): for each op, the target (by id, else by name; two that match become a pick list and a
   question), the person's role for that op (the registry tool's own `min_role`, read now), a no-op from live state
   (`change_ops.differs`), what uses a delete (F690 / F467). Acts become cards through `prepare_action`'s code (F755:
   by the op's kind). Writes become exact calls, rendered into the review by code. Its `outcome` routes the rest.
4. The review (`step_manage_review`) and F754's yes gate, unchanged.
5. `approve_change` (E1) records the person's yes to exactly those calls (F778); `apply_change` (E1) makes each
   through `registry.invoke(confirm_policy="approved_review")`, re-reads the target and says plainly what did not take
   effect, and keeps each op's before-image on the board card for undo.

The model never sees a token, an approval or another model's prose: the review it would act on is the code's.
"""
from __future__ import annotations

import json
from typing import Any

from core.builder_tools import register_builder_tool
from core.log import get_logger
from core.native_builder import change_ops as C
from core.product_tools.roles import Role

_log = get_logger(__name__)

#: The board card's stage while a change waits for its person, and once it is done.
WAITING, CHANGED, SET_ASIDE = "waiting", "changed", "set_aside"

#: A change whose new value holds a key (or the door's mark where one was taken out of the person's message).
KEY_REFUSAL = ("Keys never go in a change: a key there would be kept with it. Add it on its own settings screen, "
               "where it is stored encrypted, and if it was real, rotate it at the provider.")


def _holds_a_key(fields: dict) -> bool:
    from core.native_builder.export import CREDENTIAL_MARK
    from core.secret_scrub import holds_credential

    text = json.dumps(fields, default=str, ensure_ascii=False)
    return holds_credential(text) or CREDENTIAL_MARK in text


#: The review's lead-in is the definition's (`step_manage_review`, F754's words); these are what code adds.
DELETE_NOTE = ("It can come back only as a new copy: a new id, without its run history or an agent's memory; a "
               "schedule comes back paused, and a tool's key is entered again on a card.")


# ── the planner's three ways to finish ────────────────────────────────────────────────────────────────────────────

def _op_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": list(C.PLANNER_OPS)},
            "target": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": list(C.TARGET_KINDS)},
                "id": {"type": "string", "description": "Its id, from what you read. Leave out when you only have "
                                                        "its name."},
                "name": {"type": "string", "description": "Its name as the person said it, or the new one's name "
                                                          "for create and create_schedule."},
            }, "required": ["kind"]},
            "fields": {"type": "object", "description": "What the op sets, by the op's own field names."},
            "says": {"type": "string", "description": "This op in the person's words, one short line."},
        },
        "required": ["op", "target"],
    }


SUBMIT = {"type": "function", "function": {
    "name": "submit_change",
    "description": (
        "Finish with the change to make: one op per thing to do. Nothing happens yet: code checks each op, puts up "
        "a card for each button act, and shows the person the rest to approve. Fields per op: rename_* {name}; "
        "update_workflow {description, max_total_turns, timeout_minutes, max_run_credits, max_run_usd}; "
        "set_schedule {cron_expression | interval_value + interval_unit, prompt, description}; create_schedule "
        "{runs: {kind: workflow | agent, id}, prompt, cron_expression | interval_value + interval_unit}; "
        "set_agent_model {model}; set_agent_tools / set_agent_skills {add: [...], remove: [...]}; "
        "set_agent_instructions {system_prompt}; set_tool_policy {destructive | open_world | undeclared: allow | "
        "confirm | deny | inherit, tools: {name: decision}}; update_skill {description, body}; run_workflow "
        "{message}; knowledge_add_pages {urls}; knowledge_add_note {title, text}; board_add_card {title, text}; "
        "board_update_card {status, result}; board_comment {text}; board_create {text}; edit_steps and create "
        "{what}. Deletes and the other acts take none. For \"undo that\", send undo_of with no ops."
    ),
    "parameters": {"type": "object", "properties": {
        "ops": {"type": "array", "items": _op_schema()},
        "undo_of": {"type": "string", "description": "The key of the change to undo, from the context you were "
                                                     "given."},
    }},
}}

ASK = {"type": "function", "function": {
    "name": "ask_person",
    "description": ("Finish with one question that only the person can answer: which one they mean, what time, or "
                    "what value. Never ask what reading the workspace would tell you."),
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string"},
        "about": {"type": "string", "enum": ["which", "when", "what"]},
        "options": {"type": "array", "items": {"type": "string"}, "description": "The choices, when there are "
                                                                                 "a few."},
    }, "required": ["question", "about"]},
}}

CANNOT = {"type": "function", "function": {
    "name": "cannot",
    "description": ("Finish when the request is outside what this product does, or no op fits it: why, in one "
                    "sentence, and the nearest thing that does work here, from product_capabilities."),
    "parameters": {"type": "object", "properties": {
        "why": {"type": "string"},
        "nearest": {"type": "string"},
    }, "required": ["why", "nearest"]},
}}

FINISH_TOOLS = ("submit_change", "ask_person", "cannot")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


async def submit_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder.objects import Change

    ops = args.get("ops") if isinstance(args.get("ops"), list) else []
    undo_of = _text(args.get("undo_of"))
    if not ops and not undo_of:
        return {"error": "Send at least one op, or undo_of for \"undo that\"."}
    found: list[str] = []
    for n, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            found.append(f"op {n} is not an object")
            continue
        fields = op.get("fields") if isinstance(op.get("fields"), dict) else {}
        found += [f"op {n}: {line}" for line in C.issues(str(op.get("op") or ""), op.get("target") or {}, fields)]
    if found:
        return {"error": "Fix these and call submit_change again: " + "; ".join(found)}
    change = Change.model_validate({"ops": ops, "undo_of": undo_of or None})
    return {"finish": "submit_change", **change.model_dump(exclude_none=True)}


async def ask_person(args: dict, server_module: Any, events: list[dict]) -> dict:
    question = _text(args.get("question"))
    if not question:
        return {"error": "ask_person needs the question."}
    about = args.get("about") if args.get("about") in ("which", "when", "what") else "what"
    options = [o.strip() for o in args.get("options") or [] if isinstance(o, str) and o.strip()][:8]
    return {"finish": "ask_person", "question": question, "about": about, "options": options}


async def cannot(args: dict, server_module: Any, events: list[dict]) -> dict:
    why, nearest = _text(args.get("why")), _text(args.get("nearest"))
    if not why or not nearest:
        return {"error": "cannot needs both why and nearest: the nearest thing this product does instead."}
    return {"finish": "cannot", "why": why, "nearest": nearest}


# ── what exists ───────────────────────────────────────────────────────────────────────────────────────────────────

async def candidates(kind: str) -> list[dict] | None:
    """Everything of `kind` a change may name, as {id, name, detail}; None for a kind an act's own lookup resolves
    (a run, a board, a knowledge base…)."""
    if kind == "workflow":
        from core.builder_tools import _is_visible
        from core.store.resources import load_orchestrations

        return [{"id": o["id"], "name": o.get("name") or o["id"], "detail": o.get("description") or ""}
                for o in await load_orchestrations() if _is_visible(o) and o.get("is_active") is not False]
    if kind == "agent":
        from core.store.resources import load_agents

        return [{"id": a["id"], "name": a.get("name") or a["id"], "detail": a.get("description") or ""}
                for a in await load_agents() if not a.get("system")]
    if kind == "schedule":
        from core.store import schedules

        return [{"id": s["id"], "name": s.get("name") or s["id"],
                 "detail": s.get("cron_expression") or ""} for s in await schedules.load()]
    if kind == "skill":
        from core import skills

        # The library's only: a change is made there (`update_skill`), and a library skill shadows a disk copy of the
        # same name, which would otherwise be a second, identical option (the branch review's finding 6).
        return [{"id": s["name"], "name": s["name"], "detail": s.get("description") or ""}
                for s in await skills.list_skills() if s.get("source") == "library"]
    if kind == "tool":
        from core.store.resources import load_tools

        return [{"id": t["name"], "name": t["name"], "detail": t.get("description") or ""}
                for t in await load_tools() if t.get("name")]
    return None


def resolve(items: list[dict], target: dict) -> list[dict]:
    """The items `target` names: by id, else one exact name, else every name it is part of (F760's rule, as the
    act cards' `_pick`)."""
    wanted_id, wanted = _text(target.get("id")), _text(target.get("name")) or _text(target.get("id"))
    if wanted_id:
        by_id = [i for i in items if i["id"] == wanted_id]
        if by_id:
            return by_id
    needle = wanted.lower()
    if not needle:
        return []
    exact = [i for i in items if i["name"].lower() == needle]
    return exact or [i for i in items if needle in i["name"].lower()]


async def read(kind: str, target_id: str) -> dict | None:
    """The target as it is now, for the no-op check, the before-image and the effect check."""
    if kind == "workflow":
        from core.builder_tools import _visible_orchestration

        return await _visible_orchestration(target_id)
    if kind == "agent":
        from core.store.resources import get_agent

        agent = await get_agent(target_id)
        return None if agent is None or agent.get("system") else agent
    if kind == "schedule":
        from core.store import schedules

        return await schedules.get(target_id)
    if kind == "skill":
        from core import skills

        return await skills.get_skill(target_id)
    if kind == "tool":
        from core.store.resources import load_tools

        return next((t for t in await load_tools() if t.get("name") == target_id), None)
    return None


# ── the check ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _caller():
    from core.builder_actions import _caller as caller

    return caller()


def _run():
    from core.builder_tools import _current_run
    from core.orchestration.engine import live_run

    run_id = _current_run.get() or ""
    return run_id, (live_run(run_id) if run_id else None)


def _quoted(name: str) -> str:
    return f'"{name}"'


def _words(op: C.Op, target: dict, fields: dict, says: str) -> str:
    """One line of the review: what this op does, in code's words, then the person's."""
    kind = "workflow" if op.target == "workflow" else op.target.replace("_", " ")
    what = f"{op.verb} the {kind} {_quoted(target['name'])}"
    if op.name in ("rename_workflow", "rename_schedule", "rename_agent"):
        detail = f"to {_quoted(fields['name'])}"
    elif op.name == "create_schedule":
        detail = f"to start {_quoted(fields['runs'].get('name') or fields['runs']['id'])}"
    elif op.internal:
        detail = ""  # an undo's re-create: its line says what it is, not its whole record
    else:
        shown = {k: v for k, v in fields.items() if k != "runs"}
        detail = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v}"
                           for k, v in shown.items())
    detail = "; ".join(p for p in (detail, says) if p)
    return f"{what} — {detail}" if detail else what


def _review(ops: list[dict]) -> str:
    lines: list[str] = []
    for item in ops:
        if item["status"] != "ready":
            continue
        if item["kind"] == "delete":
            used = item.get("used_by") or []
            uses = ("used by " + ", ".join(used)) if used else "nothing uses it"
            lines.append(f"- ⚠️ DELETES: {item['line']} — {uses}. {DELETE_NOTE}")
        else:
            lines.append(f"- {item['line']}")
    return "\n".join(lines)


def _said(ops: list[dict]) -> str:
    """What the person is told when nothing waits for approval: each card, each no-op, each refusal."""
    out = [item["say"] for item in ops if item["status"] in ("card", "noop", "refused") and item.get("say")]
    return " ".join(out) or "There was nothing to change."


async def _used_by(op: C.Op, target_id: str) -> tuple[list[str], str | None]:
    """What uses a delete's target, as names, and the refusal when that stops the delete (F690 / F467)."""
    from core.orchestration import dependents

    if op.target == "agent":
        users = await dependents.what_uses_agent(target_id)
        agent = await read("agent", target_id) or {}
        return [f"{u.kind} {_quoted(u.name)}" for u in users], (
            dependents.agent_refusal(agent.get("name") or target_id, users) if users else None)
    if op.target == "workflow":
        runners = await dependents.what_runs(target_id)
        orch = await read("workflow", target_id) or {}
        return [f"{u.kind} {_quoted(u.name)}" for u in runners], (
            dependents.refusal(orch.get("name") or target_id, runners) if runners else None)
    if op.target == "tool":
        from core.store.resources import load_agents

        holders = [a.get("name") or a["id"] for a in await load_agents()
                   if not a.get("system") and target_id in (a.get("tools") or [])]
        return [f"agent {_quoted(n)}" for n in holders], None
    return [], None


async def _group_name(group_id: str) -> str | None:
    """A tool group's name: a tool keeps its group's id, and `create_http_tool` takes the name (an undo's re-create)."""
    from core.routes.tools import _load_groups

    return next((g.get("name") for g in await _load_groups() if g["id"] == group_id), None)


async def _not_found(kind: str, target: dict) -> str:
    """Why nothing matches: there is none by that name, or (a skill) it is read from disk, where it is changed."""
    wanted = target.get("name") or target.get("id") or ""
    if kind == "skill":
        from core import skills

        if resolve([{"id": s["name"], "name": s["name"]} for s in skills.scan_disk_skills()], target):
            return f"The skill {_quoted(wanted)} is read from a folder on disk, so it is changed there, not here."
    return f"No {kind} is called {_quoted(wanted)}."


async def _skill_names(fields: dict) -> str | None:
    """An agent's skills, named as `attach_skills` keeps them (lower case, hyphens: "Customer FAQ" is
    `customer-faq`), in place; and the refusal when one to add is no skill here (said now, not after the yes)."""
    from core import skills

    for key in ("add", "remove"):
        if fields.get(key):
            fields[key] = list(dict.fromkeys(skills.normalize_name(n) for n in fields[key]))
    known = {s["name"] for s in await skills.list_skills()}
    missing = [n for n in fields.get("add") or [] if n not in known]
    return f"No skill is called {', '.join(_quoted(n) for n in missing)}." if missing else None


def _keep(op: C.Op, record: dict | None, want: dict | None) -> dict | None:
    """The before-image an undo needs, never a secret's value: the fields the op touches, or for a delete the
    record with its credentials, memory and bookkeeping left out (F797)."""
    from core.secret_scrub import holds_credential

    if record is None:
        return None
    if want is not None:
        kept = {k: record.get(k) for k in want}
    else:
        kept = {k: v for k, v in record.items()
                if k not in ("memory", "created_at", "updated_at", "last_run_at", "next_run_at", "headers")}
    clean: dict[str, Any] = {}
    for key, value in kept.items():
        if holds_credential(json.dumps(value, default=str)):
            clean[key] = None
            clean.setdefault("_not_kept", []).append(key)
        else:
            clean[key] = value
    return clean


async def check_change(args: dict, server_module: Any, events: list[dict]) -> dict:
    """Every fact about the planner's change, decided here; see the module's docstring. `draft` is the planner's
    finish (E2's object); the person and their role are the run's, read now, never an argument."""
    from core.builder_actions import _NotOffered, _offer
    from core.product_tools import areas, registry
    from core.product_tools.approved import digest

    draft = args.get("draft")
    if isinstance(draft, str):
        try:
            draft = json.loads(draft)
        except ValueError:
            draft = None
    draft = draft if isinstance(draft, dict) else {}
    finish = draft.get("finish")
    base = {"object": "change", "ops": [], "picks": [], "calls": [], "review": "", "say": "", "question": ""}

    if finish == "ask_person":
        options = draft.get("options") or []
        question = draft.get("question") or ""
        if options:
            question += "\n" + "\n".join(f"{n}. {o}" for n, o in enumerate(options, 1))
        return {**base, "outcome": "ask", "question": question}
    if finish == "cannot":
        return {**base, "outcome": "said", "say": f"{draft.get('why', '').rstrip('.')}. {draft.get('nearest', '')}"
                .strip()}
    if finish != "submit_change":
        return {**base, "outcome": "said", "say": "I could not work out what to change. Say it again in other words."}

    ops = draft.get("ops") or []
    notes: list[str] = []
    if draft.get("undo_of"):
        from core.native_builder.change_undo import inverse_ops

        ops, refusal, notes = await inverse_ops(str(draft["undo_of"]))
        if refusal:
            return {**base, "outcome": "said", "say": refusal}

    handoffs = [o for o in ops if C.OPS.get(o.get("op", ""), C.OPS["create"]).kind == "handoff"]
    if handoffs and len(handoffs) == len(ops):
        return await _handed_over(base, handoffs)

    caller = _caller()
    areas.ensure_loaded()
    checked: list[dict] = []
    picks: list[dict] = []
    for n, raw in enumerate(ops):
        op = C.OPS[raw["op"]]
        target = dict(raw.get("target") or {})
        fields = dict(raw.get("fields") or {})
        item = {"op": op.name, "kind": op.kind, "target": target, "fields": fields, "says": raw.get("says") or ""}
        checked.append(item)
        if _holds_a_key(fields):
            # Nothing keeps it: not the review, its event, the card or this answer (the branch review's finding 7).
            item.update(fields={}, says="", status="refused", say=KEY_REFUSAL)
            continue
        if op.kind == "handoff":
            item.update(status="refused", say=f"{op.verb} {target.get('name') or ''} is its own request: ask me "
                                              "for it on its own once this is done.".replace("  ", " "))
            continue
        if op.kind == "create":
            if op.name == "create_schedule":
                runs = dict(fields["runs"])
                found = resolve(await candidates(runs["kind"]) or [], runs)
                if len(found) != 1:
                    item.update(status="refused" if not found else "pick",
                                say=f"No {runs['kind']} is called {_quoted(runs.get('name') or runs.get('id'))}."
                                if not found else "")
                    if found:
                        picks.append({"op": n, "field": "runs", "kind": runs["kind"],
                                      "wanted": runs.get("name") or runs.get("id"), "options": found[:8]})
                    continue
                fields["runs"] = {"kind": runs["kind"], "id": found[0]["id"], "name": found[0]["name"]}
            # Whatever is made needs a name nothing has: an undo's re-create checks nothing took it since (F797).
            name = _text(target.get("name")) or _text(fields.get("name"))
            if any(i["name"].lower() == name.lower() for i in await candidates(op.target) or []):
                item.update(status="refused", say=f"A {op.target} is already called {_quoted(name)}: "
                                                  + ("say another name." if not op.internal else
                                                     "so I can't bring the old one back under its name."))
                continue
            target["name"] = name
        else:
            items = await candidates(op.target)
            if items is not None:
                found = resolve(items, target)
                if not found:
                    item.update(status="refused", say=await _not_found(op.target, target))
                    continue
                if len(found) > 1:
                    item["status"] = "pick"
                    picks.append({"op": n, "field": "target", "kind": op.target,
                                  "wanted": target.get("name") or target.get("id"), "options": found[:8]})
                    continue
                target.update(id=found[0]["id"], name=found[0]["name"])
            else:
                target.setdefault("name", target.get("id") or "")
        item["status"] = "resolved"

    if picks:
        question = _pick_question(picks)
        out = {**base, "outcome": "ask", "ops": checked, "picks": picks, "question": question}
        # The pick list waits on the person's card, where a later turn reads "the second one" from (P4's read_turn).
        await _card(out, stage=WAITING, title=question.split("\n", 1)[0])
        return out

    # Each target is read once, and an op is checked against what the ops before it leave: two on one field (an add
    # and a remove of tools, two policy rules) each build on the last, rather than the second undoing the first
    # (the branch review's finding 3).
    leaves: dict[tuple[str, str], dict | None] = {}
    for item in checked:
        if item.get("status") != "resolved":
            continue
        op, target, fields = C.OPS[item["op"]], item["target"], item["fields"]
        if op.is_act:
            try:
                card = await _offer(op.name, C.act_arguments(op, target["id"] if target.get("id") else
                                                             target.get("name", ""), fields))
            except _NotOffered as why:
                item.update(status="noop", say=str(why))
                continue
            events.append(card)
            pressed = " or ".join(f'"{b["label"]}"' for b in card["buttons"]) or "answer on it"
            item.update(status="card", say=f"A card is up: {card['title']}. Press {pressed} to do it.")
            continue
        tool = registry.get(op.tool)
        if tool is None or not caller.role.can_act_as(tool.min_role):
            need = tool.min_role.label if tool is not None else Role.BUILDER.label
            item.update(status="refused", say=f"{op.verb} the {op.target} {_quoted(target['name'])} needs the {need} "
                                              f"role; you are a {caller.role.label} in this workspace, so I can't.")
            continue
        here = (op.target, target.get("id") or "")
        if op.kind == "create":
            live = None
        elif here in leaves:
            live = leaves[here]
        else:
            live = await read(op.target, target["id"])
        if op.kind != "create" and live is None:
            item.update(status="refused", say=f"The {op.target} {_quoted(target['name'])} is not there any more.")
            continue
        if op.name == "set_agent_skills":
            refusal = await _skill_names(fields)
            if refusal:
                item.update(status="refused", say=refusal)
                continue
        want = C.expected(op, fields, live)
        if op.kind == "delete":
            used, refusal = await _used_by(op, target["id"])
            if refusal:
                item.update(status="refused", say=refusal)
                continue
            item["used_by"] = used
            if op.target == "tool" and live.get("group"):
                live = {**live, "group_name": await _group_name(live["group"])}
        elif op.kind != "create" and not C.differs(live, want):
            item.update(status="noop", say=f"The {op.target} {_quoted(target['name'])} is already that way: "
                                           "nothing to change.")
            continue
        tool_name, call_args = C.call(op, target.get("id", ""), fields, live, name=target.get("name", ""))
        item.update(status="ready", call={"tool": tool_name, "args": call_args}, expected=want,
                    before=_keep(op, live, want), line=_words(op, target, fields, item["says"]))
        if op.kind != "create":
            leaves[here] = None if want is None else {**live, **want}

    ready = [i for i in checked if i["status"] == "ready"]
    # What can't be undone is said once: on the review when there is one, else as the answer.
    said = " ".join([*([] if ready else notes), *([_said(checked)] if len(ready) < len(checked) or not ready else [])])
    if not ready:
        return {**base, "outcome": "said", "ops": checked, "say": said}
    calls = [[i["call"]["tool"], i["call"]["args"]] for i in ready]
    review = _review(checked) + "".join(f"\n- Not undone: {note}" for note in notes)
    out = {**base, "outcome": "review", "ops": checked, "calls": calls, "review": review, "say": said,
           **({"undo_of": draft["undo_of"]} if draft.get("undo_of") else {})}
    out["digest"] = digest(reviewed_of(out))
    await _card(out, stage=WAITING)
    return out


def reviewed_of(checked: dict) -> dict:
    """The part of the check a person approves: the ops as shown and the exact calls (what F778's digest covers)."""
    return {"ops": checked.get("ops") or [], "calls": checked.get("calls") or []}


def _pick_question(picks: list[dict]) -> str:
    parts = []
    for pick in picks:
        options = "\n".join(f"{n}. {_quoted(o['name'])} ({o['id']})" + (f" — {o['detail']}" if o.get("detail") else "")
                            for n, o in enumerate(pick["options"], 1))
        parts.append(f"More than one {pick['kind']} matches {_quoted(pick['wanted'])}. Which one do you mean?\n"
                     f"{options}")
    return "\n\n".join(parts)


async def _handed_over(base: dict, handoffs: list[dict]) -> dict:
    """`edit_steps` goes to today's update path with the workflow on it; `create` to today's create path (Naveen's
    split, 10-22). Same turn: the person is not asked again."""
    first = handoffs[0]
    if first["op"] == "edit_steps":
        found = resolve(await candidates("workflow") or [], first.get("target") or {})
        if len(found) > 1:
            pick = {"op": 0, "field": "target", "kind": "workflow", "wanted": first["target"].get("name"),
                    "options": found[:8]}
            return {**base, "outcome": "ask", "ops": handoffs, "picks": [pick], "question": _pick_question([pick])}
        if not found:
            return {**base, "outcome": "said",
                    "say": f"No workflow is called {_quoted(first['target'].get('name') or first['target'].get('id'))}."}
        _routed("update", current_orchestration_id=found[0]["id"], current_orchestration_name=found[0]["name"])
        return {**base, "outcome": "steps", "ops": handoffs, "workflow": {"id": found[0]["id"],
                                                                          "name": found[0]["name"]}}
    _routed("create")
    return {**base, "outcome": "build", "ops": handoffs}


def _routed(intent: str, **facts: str) -> None:
    """The run goes on as the route it is handed to: `intent` is what the door (`step_may_change`, which the hand-over
    passes through again: a builder's route), the update path's prompts and its saver (`step_save_route`) read."""
    _, run = _run()
    if run is not None:
        run.shared_state.update(intent=intent, **facts)


async def _card(checked: dict, *, stage: str, data: dict | None = None, title: str = "") -> None:
    """Keep this change on Synapse AI's board, as its person's card (F776): a failure to write it never stops the
    change itself."""
    from core.boards import assistant

    run_id, _ = _run()
    if not run_id:
        return
    caller = _caller()
    title = title or "; ".join(i.get("line") or i["op"] for i in checked.get("ops") or [] if i.get("status") == "ready")
    try:
        await assistant.put(run_id, title=(title or "A change")[:200], stage=stage, owner_id=caller.user_id,
                            body=checked.get("review") or "", data=data or {"change": checked}, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the board is the record, not the change
        _log.warning("change card for run %s not written: %s", run_id, exc)


for _schema, _handler in ((SUBMIT, submit_change), (ASK, ask_person), (CANNOT, cannot)):
    register_builder_tool(_schema, _handler)

CHECK = {"type": "function", "function": {
    "name": "check_change",
    "description": "Synapse AI's change check (code): targets, roles, no-ops, cards, the review.",
    "parameters": {"type": "object", "properties": {"draft": {"type": "object"}}, "required": ["draft"]},
}}
register_builder_tool(CHECK, check_change)

