"""Synapse AI's change ops: one closed table (SAI2-P2, F792, D308).

A change is a list of ops, each `{op, target: {kind, id | name}, fields}` (`objects.ChangeOp`). The change planner
(a model) proposes them; everything else is a fact that code decides from this table:

* **what an op may set**: its `fields`, and which of them it needs. A field it does not take is refused by name, as
  the write tools refuse theirs (F784's rule: a write never takes an id from its payload, and never drops a key
  silently);
* **what it calls**: one registry tool for a write (`registry.invoke`, so the audit is by id, F697); `prepare_action`'s
  card for an act (`ACTIONS`, F755: the op's kind decides card or review, never text);
* **what the target must look like afterwards** (`expected`), which is both the no-op check before and the effect
  check after (`differs`);
* **what undoes it** (`inverse`), from the target as it was.

Two ops hand the request to another route instead: `edit_steps` (a workflow's steps: the build route's update
path, on the typed Plan since P3) and `create` (a new agent, skill, HTTP tool or OpenAPI import: today's create
path, which holds those tools' checks — F769's host rule, TAG1's method defaults, the Connect card, F489; Naveen's
split, 10-22). Neither is a second way to make those things.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["act", "write", "delete", "create", "handoff"]

#: Every kind of thing an op can be about.
TARGET_KINDS = ("workflow", "agent", "schedule", "tool", "skill", "board", "knowledge_base", "document", "run",
                "approval", "card", "openapi", "python_tool")

#: Tool-policy decisions an op may set; `inherit` removes the agent's own rule, so the workspace's applies.
DECISIONS = ("allow", "confirm", "deny", "inherit")
POLICY_CATEGORIES = ("destructive", "open_world", "undeclared")


@dataclass(frozen=True)
class Op:
    name: str
    kind: Kind
    #: The target's kind (`create`'s is one of `CREATES`).
    target: str
    #: The registry tool a write calls; None for an act (its card's tool is `prepare_action`'s) and a hand-over.
    tool: str | None = None
    fields: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    #: At least one of `fields` must be set.
    any_of: bool = False
    #: The review's words for it: "<verb> <target>".
    verb: str = ""
    #: Built by code only, never offered to a model: an undo's re-create from a before-image (F797).
    internal: bool = False

    @property
    def is_act(self) -> bool:
        return self.kind == "act"

    @property
    def is_write(self) -> bool:
        return self.kind in ("write", "delete", "create")


_ACTS = (
    Op("run_workflow", "act", "workflow", fields=("message",), verb="Run"),
    Op("run_schedule_now", "act", "schedule", verb="Run now"),
    Op("schedule_on", "act", "schedule", verb="Turn on"),
    Op("schedule_off", "act", "schedule", verb="Turn off"),
    Op("answer_run", "act", "run", verb="Answer"),
    Op("answer_tool_call", "act", "approval", verb="Answer"),
    Op("cancel_run", "act", "run", verb="Stop"),
    Op("knowledge_add_pages", "act", "knowledge_base", fields=("urls", "kind"), required=("urls",),
       verb="Add pages to"),
    Op("knowledge_add_note", "act", "knowledge_base", fields=("title", "text"), required=("text",),
       verb="Add a note to"),
    Op("knowledge_retry_document", "act", "document", verb="Index again"),
    Op("knowledge_delete_document", "act", "document", verb="Delete"),
    Op("board_add_card", "act", "board", fields=("title", "text"), required=("title",), verb="Add a card to"),
    Op("board_update_card", "act", "card", fields=("status", "result"), verb="Update"),
    Op("board_comment", "act", "card", fields=("text",), required=("text",), verb="Comment on"),
    Op("board_create", "act", "board", fields=("text",), verb="Make"),
    Op("board_delete", "act", "board", verb="Delete"),
)

_SCHEDULE_TIME = ("schedule_type", "cron_expression", "interval_value", "interval_unit", "timezone")

_WRITES = (
    Op("rename_workflow", "write", "workflow", "set_orchestration_meta", ("name",), ("name",), verb="Rename"),
    Op("update_workflow", "write", "workflow", "set_orchestration_meta",
       ("description", "max_total_turns", "timeout_minutes", "max_run_credits", "max_run_usd"), any_of=True,
       verb="Change the settings of"),
    Op("delete_workflow", "delete", "workflow", "delete_orchestration", verb="Delete"),
    Op("set_schedule", "write", "schedule", "update_schedule", (*_SCHEDULE_TIME, "prompt", "description"),
       any_of=True, verb="Change"),
    Op("rename_schedule", "write", "schedule", "update_schedule", ("name",), ("name",), verb="Rename"),
    Op("delete_schedule", "delete", "schedule", "delete_schedule", verb="Delete"),
    Op("create_schedule", "create", "schedule", "create_schedule",
       ("runs", "prompt", *_SCHEDULE_TIME, "description"), ("runs", "prompt"), verb="Make"),
    # A model's id, or what the agent needs, or the person's own words for one (F966): code fills the id.
    Op("set_agent_model", "write", "agent", "update_agent", ("model", "needs", "wish"), any_of=True,
       verb="Change the model of"),
    Op("set_agent_tools", "write", "agent", "update_agent", ("add", "remove"), any_of=True,
       verb="Change the tools of"),
    Op("set_agent_skills", "write", "agent", "attach_skills", ("add", "remove"), any_of=True,
       verb="Change the skills of"),
    Op("set_agent_instructions", "write", "agent", "update_agent", ("system_prompt",), ("system_prompt",),
       verb="Change the instructions of"),
    Op("rename_agent", "write", "agent", "update_agent", ("name",), ("name",), verb="Rename"),
    Op("set_tool_policy", "write", "agent", "update_agent", (*POLICY_CATEGORIES, "tools"), any_of=True,
       verb="Change the tool policy of"),
    Op("delete_agent", "delete", "agent", "delete_agent", verb="Delete"),
    Op("update_skill", "write", "skill", "update_skill", ("description", "body"), any_of=True, verb="Change"),
    Op("delete_tool", "delete", "tool", "delete_custom_tool", verb="Delete"),
)

#: What `create` hands to today's create path.
CREATES = ("agent", "skill", "tool", "openapi", "python_tool")

_HANDOFFS = (
    Op("edit_steps", "handoff", "workflow", fields=("what",), required=("what",), verb="Change the steps of"),
    Op("create", "handoff", "new", fields=("what",), required=("what",), verb="Make"),
)

#: An undo's re-create of what a change deleted, from its before-image (F797): code builds these from the card it
#: kept, never a model, so they are no second way to make a new agent, workflow or tool. Each comes back as a new
#: copy, which the review says.
_RESTORES = (
    Op("restore_agent", "create", "agent", "create_agent",
       ("name", "description", "type", "tools", "system_prompt", "model", "delegate_agent_ids", "tool_policy"),
       ("name", "description", "type", "tools", "system_prompt"), verb="Bring back", internal=True),
    Op("restore_workflow", "create", "workflow", "create_orchestration",
       ("name", "description", "steps_json", "entry_step_id", "state_schema_json", "max_total_turns",
        "timeout_minutes", "max_run_credits", "max_run_usd"), ("name", "steps_json", "entry_step_id"),
       verb="Bring back", internal=True),
    Op("restore_tool", "create", "tool", "create_http_tool",
       ("name", "description", "url", "method", "input_schema_json", "body_template", "group"),
       ("name", "description", "url"), verb="Bring back", internal=True),
    Op("restore_python_tool", "create", "tool", "create_python_tool",
       ("name", "description", "code", "input_schema_json", "group"), ("name", "description", "code"),
       verb="Bring back", internal=True),
)

OPS: dict[str, Op] = {op.name: op for op in (*_ACTS, *_WRITES, *_HANDOFFS, *_RESTORES)}
#: What a model may propose: every op but the internal ones.
PLANNER_OPS = tuple(n for n, op in OPS.items() if not op.internal)
#: (Every button act in `builder_actions.ACTIONS` is an op of kind act, and only those: a test holds it, since this
#: module is imported while the builder's tools load and cannot read that table then.)

#: The registry tool each write calls, once each (a test holds every one served and its arguments its schema's).
WRITE_TOOLS = tuple(dict.fromkeys(op.tool for op in OPS.values() if op.tool))

#: The argument that names an act's target, as `prepare_action` takes it.
_ACT_ARG = {"workflow": "workflow", "schedule": "schedule", "run": "run_id", "approval": "approval_id",
            "knowledge_base": "knowledge_base", "document": "doc_id", "board": "board", "card": "card_id"}


# ── checking what a model wrote ───────────────────────────────────────────────────────────────────────────────────

def _is_list_of_text(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _field_issue(op: Op, key: str, value: Any) -> str | None:
    if key in ("add", "remove", "urls"):
        return None if _is_list_of_text(value) else f"`{key}` is a list of names"
    if key in POLICY_CATEGORIES:
        return None if value in DECISIONS else f"`{key}` is one of {', '.join(DECISIONS)}"
    if key == "tools" and op.name == "set_tool_policy":
        ok = isinstance(value, dict) and all(isinstance(k, str) and k and v in DECISIONS for k, v in value.items())
        return None if ok else f"`tools` maps a tool's name to one of {', '.join(DECISIONS)}"
    if key == "runs":
        ok = (isinstance(value, dict) and value.get("kind") in ("workflow", "agent")
              and any(isinstance(value.get(k), str) and value[k].strip() for k in ("id", "name")))
        return None if ok else "`runs` is {kind: workflow | agent, id or name} of what the schedule starts"
    if key == "schedule_type":
        return None if value in ("cron", "interval") else "`schedule_type` is cron or interval"
    if key == "timezone":
        from core.models_schedule import _is_timezone

        return None if isinstance(value, str) and _is_timezone(value) else \
            "`timezone` is a place's timezone, like Europe/Amsterdam (leave it out for the person's own)"
    if key == "interval_unit":
        return None if value in ("minutes", "hours", "days") else "`interval_unit` is minutes, hours or days"
    if key in ("interval_value", "max_total_turns", "timeout_minutes"):
        return None if isinstance(value, int) and not isinstance(value, bool) and value > 0 else \
            f"`{key}` is a whole number above 0"
    if key in ("max_run_credits", "max_run_usd"):
        return None if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else \
            f"`{key}` is a number above 0"
    if key == "needs":
        from core.native_builder.models_menu import NEEDS

        return None if value in NEEDS else f"`needs` is one of {', '.join(NEEDS)}"
    if key == "status":
        return None if value in ("open", "done", "failed") else "`status` is open, done or failed"
    if key == "kind":
        return None if value in ("page", "sitemap", "site") else "`kind` is page, sitemap or site"
    return None if isinstance(value, str) and value.strip() else f"`{key}` is text"


def issues(op_name: str, target: dict[str, Any], fields: dict[str, Any], *, internal: bool = False) -> list[str]:
    """What is wrong with one proposed op, one line each, for the model to fix (empty when nothing is). `internal`:
    code built it (an undo's re-create may be one), never a model."""
    op = OPS.get(op_name)
    if op is None or (op.internal and not internal):
        return [f"`{op_name}` is not an op. The ops are: {', '.join(PLANNER_OPS)}."]
    found: list[str] = []
    kind = (target or {}).get("kind")
    if op.name == "create":
        if kind not in CREATES:
            found.append(f"`create` makes a new {', '.join(CREATES)}: target.kind is one of those")
    elif kind != op.target:
        found.append(f"`{op.name}` is about a {op.target}: target.kind is {op.target}")
    if not any(isinstance((target or {}).get(k), str) and target[k].strip() for k in ("id", "name")):
        found.append(f"`{op.name}` needs its target's id (or its name)")
    unknown = [k for k in fields if k not in op.fields]
    if unknown:
        takes = f"it takes {', '.join(op.fields)}" if op.fields else "it takes no fields"
        found.append(f"`{op.name}` cannot set {', '.join(unknown)}: {takes}")
    # A re-create may need an empty list (an agent with no tools); never a value the card did not keep.
    left_out = [k for k in op.required if fields.get(k) is None or (not op.internal and fields.get(k) in ("", [], {}))]
    if left_out:
        found.append(f"`{op.name}` needs {', '.join(left_out)}")
    if op.any_of and not any(fields.get(k) not in (None, "", [], {}) for k in op.fields):
        found.append(f"`{op.name}` needs at least one of {', '.join(op.fields)}")
    for key in op.fields if not op.internal else ():  # a re-create's values are a stored record's; its tool checks them
        if key in fields and fields[key] not in (None, "", [], {}):
            if (why := _field_issue(op, key, fields[key])) is not None:
                found.append(f"`{op.name}`: {why}")
    if not found and not op.internal:
        found += [f"`{op.name}`: {why}" for why in _saved_as(op, fields)]
    return found


def _saved_as(op: Op, fields: dict[str, Any]) -> list[str]:
    """What the save itself would refuse, by the product's own rules, said before the review rather than after the
    yes (the branch review's finding 10): a schedule's time, a run cap."""
    from core.models_schedule import ScheduleInvalid, validate_timing
    from core.orchestration.run_cap import clean

    out: list[str] = []
    if op.name in ("set_schedule", "create_schedule"):
        time = _schedule_time(fields)
        whole = "cron_expression" in time or ("interval_value" in time and "interval_unit" in time)
        if op.name == "create_schedule" and not whole:
            out.append("a new schedule needs its time: cron_expression, or interval_value and interval_unit")
        elif whole:
            try:
                validate_timing(time)
            except ScheduleInvalid as why:
                out.append(str(why))
    for key in ("max_run_credits", "max_run_usd"):
        if op.name == "update_workflow" and key in fields:
            try:
                clean(fields[key], credits=key == "max_run_credits")
            except ValueError as why:
                out.append(str(why))
    return out


# ── what an op calls, and what it leaves ──────────────────────────────────────────────────────────────────────────

def act_arguments(op: Op, target_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """`prepare_action`'s arguments for an act on a resolved target."""
    name_arg = "name" if op.name == "board_create" else _ACT_ARG[op.target]
    return {"action": op.name, name_arg: target_id, **{k: v for k, v in fields.items() if k in op.fields}}


def _merged(names: list[str], add: list[str] | None, remove: list[str] | None) -> list[str]:
    out = [n for n in names if n not in (remove or [])]
    return out + [n for n in (add or []) if n not in out]


def _policy(live: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    policy = dict(live.get("tool_policy") or {})
    rules = {k: fields[k] for k in POLICY_CATEGORIES if k in fields}
    rules.update({f"tool:{name}": d for name, d in (fields.get("tools") or {}).items()})
    for key, decision in rules.items():
        if decision == "inherit":
            policy.pop(key, None)
        else:
            policy[key] = decision
    return policy


def _schedule_time(fields: dict[str, Any]) -> dict[str, Any]:
    out = {k: fields[k] for k in _SCHEDULE_TIME if k in fields}
    if "cron_expression" in out:
        out["cron_expression"] = str(out["cron_expression"]).strip()
        out.setdefault("schedule_type", "cron")
    elif "interval_value" in out or "interval_unit" in out:
        out.setdefault("schedule_type", "interval")
    return out


def expected(op: Op, fields: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the target must read afterwards, field by field; None for a delete (it must be gone)."""
    live = live or {}
    name = op.name
    if op.kind == "delete":
        return None
    if name in ("rename_workflow", "rename_schedule", "rename_agent"):
        return {"name": str(fields["name"]).strip()}
    if name in ("update_workflow", "update_skill"):
        return {k: fields[k] for k in op.fields if k in fields}
    if name == "set_schedule":
        return {**_schedule_time(fields), **{k: fields[k] for k in ("prompt", "description") if k in fields}}
    if name == "create_schedule":
        runs = fields["runs"]
        return {"target_type": "orchestration" if runs["kind"] == "workflow" else "agent",
                "target_id": runs["id"], "prompt": fields["prompt"], **_schedule_time(fields),
                **({"description": fields["description"]} if fields.get("description") else {})}
    if name == "set_agent_model":
        return {"model": str(fields["model"]).strip()}
    if name == "set_agent_tools":
        return {"tools": _merged(list(live.get("tools") or []), fields.get("add"), fields.get("remove"))}
    if name == "set_agent_skills":
        return {"skills": _merged(list(live.get("skills") or []), fields.get("add"), fields.get("remove"))}
    if name == "set_agent_instructions":
        return {"system_prompt": fields["system_prompt"]}
    if name == "set_tool_policy":
        return {"tool_policy": _policy(live, fields)}
    if op.internal:
        return {"name": fields["name"]}
    raise ValueError(f"{name} leaves nothing to check")


def call(op: Op, target_id: str, fields: dict[str, Any], live: dict[str, Any] | None,
         *, name: str = "") -> tuple[str, dict[str, Any]]:
    """The one registry call a write makes: its tool and exact arguments (what an approved review covers)."""
    assert op.tool is not None, f"{op.name} calls no tool"
    after = expected(op, fields, live)
    if op.kind == "delete":
        key = {"workflow": "orch_id", "agent": "agent_id", "schedule": "schedule_id", "tool": "name"}[op.target]
        return op.tool, {key: target_id}
    if op.internal:
        return op.tool, {k: v for k, v in fields.items() if k in op.fields and v is not None}
    if op.target == "workflow":
        return op.tool, {"orch_id": target_id, **after}
    if op.name == "create_schedule":
        return op.tool, {"name": name, **after}
    if op.target == "schedule":
        return op.tool, {"schedule_id": target_id, **after}
    if op.name == "set_agent_skills":
        return op.tool, {"agent_id": target_id, "names": after["skills"]}
    if op.target == "agent":
        return op.tool, {"agent_id": target_id, "fields": after}
    if op.target == "skill":
        return op.tool, {"name": target_id, **after}
    raise ValueError(f"{op.name} has no call")


def _same(want: Any, got: Any) -> bool:
    if isinstance(want, str) and isinstance(got, str):
        return want.strip() == got.strip()
    if isinstance(want, (int, float)) and isinstance(got, (int, float)) and not isinstance(want, bool):
        return float(want) == float(got)
    return want == got


def differs(record: dict[str, Any] | None, want: dict[str, Any] | None) -> dict[str, tuple[Any, Any]]:
    """Each field of `want` the record does not read as: {field: (wanted, read)}. A delete's `want` is None: the
    record must be gone."""
    if want is None:
        return {} if record is None else {"exists": (False, True)}
    if record is None:
        return {"exists": (True, False)}
    return {k: (v, record.get(k)) for k, v in want.items() if not _same(v, record.get(k))}
