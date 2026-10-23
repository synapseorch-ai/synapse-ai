""""Undo that": the inverse of a change Synapse AI made, built by code from the card it kept (SAI2-P2, F797, D308).

An undo is a change like any other (BWA1's review point 5): `inverse_ops` turns the applied ops on the person's card
into ops, and `check_change` puts them through the same check, the review, F754's gate, the role door and the apply.
The model only says which change (`submit_change(undo_of=…)`); it never writes the inverse.

What comes back is said, never implied:
* a value the card never kept (it held a credential) is not restored, and the review says so;
* a setting that was unset before cannot be unset from here, and is named;
* a deleted schedule, agent, workflow or HTTP tool comes back only as a new copy, through its create tool: a new id,
  with no run history or memory; a schedule paused; a tool without its headers, whose key is entered again on a
  card. A python tool cannot be re-made here (Naveen's decision, 10-22).
"""
from __future__ import annotations

import json
from typing import Any

from core.native_builder import change_ops as C

_AGENT_TYPES = ("conversational", "code", "delegate")


def _target(applied: dict, **extra: Any) -> dict:
    target = dict(applied.get("target") or {})
    return {"kind": target.get("kind"), "id": target.get("id"), "name": target.get("name"), **extra}


def _settable(before: dict) -> tuple[dict, list[str]]:
    kept = {k: v for k, v in before.items() if k != "_not_kept" and v is not None
            and k not in (before.get("_not_kept") or [])}
    gone = [k for k, v in before.items() if k != "_not_kept" and v is None]
    return kept, gone


def _inverse(applied: dict) -> tuple[dict | None, str | None]:
    """One applied op's inverse, and what it cannot bring back (or why there is none)."""
    name, before = applied["op"], applied.get("before") or {}
    fields = applied.get("fields") or {}
    label = f'the {applied["target"].get("kind")} "{applied["target"].get("name")}"'
    if name in ("rename_workflow", "rename_schedule", "rename_agent"):
        return {"op": name, "target": _target(applied), "fields": {"name": before["name"]},
                "says": "undo: its old name"}, None
    if name in ("update_workflow", "update_skill", "set_schedule"):
        kept, gone = _settable(before)
        note = (f"{', '.join(gone)} of {label} had no value before, and can't be cleared from here." if gone else None)
        if not kept:
            return None, note
        return {"op": name, "target": _target(applied), "fields": kept, "says": "undo: as it was"}, note
    if name == "set_agent_model":
        if not before.get("model"):
            return None, f"{label} used the workspace's default model before: choose it again in the agent's settings."
        return {"op": name, "target": _target(applied), "fields": {"model": before["model"]},
                "says": "undo: its old model"}, None
    if name in ("set_agent_tools", "set_agent_skills"):
        key = "tools" if name == "set_agent_tools" else "skills"
        was = list(before.get(key) or [])
        now = C._merged(was, fields.get("add"), fields.get("remove"))
        add, remove = [n for n in was if n not in now], [n for n in now if n not in was]
        return {"op": name, "target": _target(applied), "fields": {k: v for k, v in (("add", add), ("remove", remove))
                                                                    if v}, "says": f"undo: its old {key}"}, None
    if name == "set_agent_instructions":
        if "system_prompt" in (before.get("_not_kept") or []):
            return None, f"{label}'s old instructions held a key, so they were not kept and can't be put back."
        return {"op": name, "target": _target(applied), "fields": {"system_prompt": before["system_prompt"]},
                "says": "undo: its old instructions"}, None
    if name == "set_tool_policy":
        was = dict(before.get("tool_policy") or {})
        now = C._policy({"tool_policy": was}, fields)
        rules: dict[str, Any] = {}
        for key in sorted(set(was) | set(now)):
            if was.get(key) == now.get(key):
                continue
            decision = was.get(key, "inherit")
            if key.startswith("tool:"):
                rules.setdefault("tools", {})[key[5:]] = decision
            else:
                rules[key] = decision
        return {"op": name, "target": _target(applied), "fields": rules, "says": "undo: its old tool policy"}, None
    if name == "create_schedule":
        return {"op": "delete_schedule", "target": _target(applied), "fields": {},
                "says": "undo: the schedule it made"}, None
    if name == "delete_schedule":
        runs = {"kind": "workflow" if before.get("target_type") == "orchestration" else "agent",
                "id": before.get("target_id")}
        # Its time as the op table says a time is, its timezone with it (F948): no list of its own to fall behind.
        time = {k: before[k] for k in C._SCHEDULE_TIME if before.get(k) is not None}
        return {"op": "create_schedule", "target": {"kind": "schedule", "name": before.get("name")},
                "fields": {"runs": runs, "prompt": before.get("prompt") or "", **time},
                "says": "undo: it comes back as a new copy, paused"}, None
    if name == "delete_agent":
        if before.get("type") not in _AGENT_TYPES:
            return None, f"{label} was a {before.get('type')} agent, which can't be made again from here."
        keep = {k: before.get(k) for k in C.OPS["restore_agent"].fields}
        keep["tools"] = list(keep.get("tools") or [])
        keep["description"] = keep.get("description") or keep["name"]
        return {"op": "restore_agent", "target": {"kind": "agent", "name": before.get("name")}, "fields": keep,
                "says": "undo: a new copy, without its memory"}, None
    if name == "delete_workflow":
        if before.get("steps") is None:
            return None, f"{label}'s steps held a key, so they were not kept and it can't be made again."
        keep = {"name": before.get("name"), "description": before.get("description"),
                "steps_json": json.dumps(before.get("steps") or []), "entry_step_id": before.get("entry_step_id"),
                **{k: before.get(k) for k in ("max_total_turns", "timeout_minutes", "max_run_credits", "max_run_usd")}}
        if before.get("state_schema"):
            keep["state_schema_json"] = json.dumps(before["state_schema"])
        return {"op": "restore_workflow", "target": {"kind": "workflow", "name": before.get("name")},
                "fields": keep, "says": "undo: a new copy, without its run history"}, None
    if name == "delete_tool":
        if before.get("tool_type") != "http":
            return None, f"{label} was not an HTTP tool, so it can't be made again from here."
        if not before.get("url"):
            return None, f"{label}'s address held a key, so it was not kept and the tool can't be made again."
        # Its group by name, as `create_http_tool` takes it (the tool kept the group's id).
        keep = {"name": before.get("name"), "description": before.get("description") or before.get("name"),
                "url": before["url"], "method": before.get("method"), "body_template": before.get("body"),
                "group": before.get("group_name")}
        if before.get("inputSchema"):
            keep["input_schema_json"] = json.dumps(before["inputSchema"])
        return {"op": "restore_tool", "target": {"kind": "tool", "name": before.get("name")}, "fields": keep,
                "says": "undo: a new copy, without its headers: enter its key again on its card"}, None
    return None, f"{label} can't be undone from here."


async def _unsaveable(fields: dict) -> str | None:
    """Why no door would save this workflow now (`save_rules`, F850/F853), in its words; None when one would."""
    from core.orchestration.save_rules import agent_ids, refusal
    from core.store.resources import get_agent

    orch = {"name": fields.get("name"), "steps": json.loads(fields.get("steps_json") or "[]"),
            "entry_step_id": fields.get("entry_step_id")}
    agents = {aid: found for aid in agent_ids(orch) if (found := await get_agent(aid))}
    said = refusal(orch, agents)
    return said.removeprefix("Refusing to save: ") if said else None


async def inverse_ops(key: str) -> tuple[list[dict], str | None, list[str]]:
    """(the ops that undo change `key`, newest first; why there are none; what they cannot bring back)."""
    from core.boards import assistant
    from core.native_builder.change_tools import CHANGED, _caller

    card = await assistant.card(key)
    person = _caller().user_id
    if card is None or card.stage != CHANGED or (person and card.owner_id != person):
        return [], "I can't find a change of yours to undo by that name.", []
    ops: list[dict] = []
    notes: list[str] = []
    for applied in reversed((card.data or {}).get("applied") or []):
        op, note = _inverse(applied)
        if op is not None and C.issues(op["op"], op["target"], op["fields"], internal=True):
            # Checked as any op is (the branch review's finding 9): a value the card never kept, a re-create short
            # of what it needs, is not undone, rather than undone as the word None or refused after the yes.
            target = applied.get("target") or {}
            op, note = None, (f'the {target.get("kind")} "{target.get("name")}" can\'t be put back as it was: '
                              "what it held before was not kept.")
        if op is not None and op["op"] == "restore_workflow" and (said := await _unsaveable(op["fields"])):
            # Saved before F850 by a door that checked nothing: the save would refuse it after the yes, so the review
            # never promises it (each save door asks `save_rules`, and so does this).
            op, note = None, f'the workflow "{op["target"].get("name")}" can\'t be brought back as it was: {said}'
        if op is not None:
            ops.append(op)
        if note:
            notes.append(note)
    if not ops:
        return [], " ".join(notes) or "That change can't be undone from here.", []
    return ops, None, notes
