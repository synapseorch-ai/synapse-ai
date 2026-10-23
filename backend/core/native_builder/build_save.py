"""Synapse AI's build route after the person's yes: the agents' instructions, the approval, and the save, in code
(SAI2-P3, D300; F940, F941).

* `submit_prompts` (the prompt writer's E2 finish): instructions for each of the Plan's new agents, by name. Written
  after the yes and before the approval is recorded, so the approval covers the exact calls (Naveen, 10-23, Q2).
* `approve_plan` (E1, on F754's yes side): checks the yes itself, that what waits is what the card showed (the digest
  `check_build` took), and that the card offered Build it; then records the person's approval of exactly the calls
  `save_build` makes (F778, `review.record_approval`), with placeholders for the ids the build makes (Q1, F940), and
  what the workspace held then, so a save run again knows what it made.
* `save_build` (E1, `step_save`): one pass, no model (F867c by construction). It checks the workspace again before
  the first write — a tool gone since the yes, or a rule broken, stops it with nothing written and says why (F768,
  F867a) — then makes each call through `registry.invoke(approved_review)`, as the person: agents (members before
  their team), inner workflows (E3), the workflow (or, for an update, over its own id), its settings, boards and the
  schedule, made paused (F149/F150). F695's sink gives the run each `resource_saved`, so `run_writes` says what was
  made. On a failure it removes what this pass made and says why (`build_stopped`). Run again after a restart, it
  takes what an earlier pass made rather than making it twice. Afterwards it reads back what it saved.
"""
from __future__ import annotations

import json
from typing import Any

from core.builder_tools import register_builder_tool
from core.log import get_logger
from core.native_builder import objects as O
from core.native_builder.build_plan import (FINDINGS_KEY, MADE, _obj, _state, checked_in, dry_issues,
                                            placeholder_mint, reviewed_of, workspace)
from core.native_builder.change_tools import _caller, _run
from core.product_tools.errors import AlreadySpent, ToolRefusal

_log = get_logger(__name__)

PROMPTS_KEY, ANSWER_KEY, APPROVAL_KEY = "agent_prompts", "review_feedback", "plan_approval"
EXISTING_KEY = "existing_orch"
#: What a removal after a failed save says, beside the failure itself.
UNDONE = "the build stopped before it was saved whole"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()) if not isinstance(value, str) else value.strip()


def _plan_of(checked: dict) -> O.Plan:
    return O.Plan.model_validate(checked.get("plan") or {})


def _prompts(state: dict) -> dict[str, str]:
    raw = _obj(state.get(PROMPTS_KEY))
    raw = raw.get("prompts") if isinstance(raw, dict) and isinstance(raw.get("prompts"), dict) else raw
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


# ── the prompt writer's finish ────────────────────────────────────────────────────────────────────────────────────

SUBMIT_PROMPTS = {"type": "function", "function": {
    "name": "submit_prompts",
    "description": "Finish with the instructions for each of the plan's new agents, by the agent's name.",
    "parameters": {"type": "object", "properties": {"prompts": {"type": "array", "items": {
        "type": "object", "properties": {
            "agent": {"type": "string", "description": "The new agent's name, as the plan has it"},
            "instructions": {"type": "string", "description": "Its instructions: who it is, what it does with its "
                                                              "tools, and how it answers"}},
        "required": ["agent", "instructions"]}}}, "required": ["prompts"]},
}}


async def submit_prompts(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.secret_scrub import holds_credential

    names = [a.get("name") for a in (checked_in(_state()).get("plan") or {}).get("agents") or []]
    given = {_text(p.get("agent")): _text(p.get("instructions")) for p in args.get("prompts") or []
             if isinstance(p, dict)}
    unknown = [n for n in given if n not in names]
    empty = [n for n in names if not given.get(n)]
    if unknown or empty:
        return {"error": "Write instructions for each of the plan's new agents, by its name: "
                         + ", ".join(f'"{n}"' for n in names) + "; and for no other."}
    if any(holds_credential(text) for text in given.values()):
        return {"error": "Instructions never hold a key, a token or a password: the agent's tools carry those."}
    return {"finish": "submit_prompts", "prompts": given}


# ── the calls a save makes ────────────────────────────────────────────────────────────────────────────────────────

_CAPS = ("max_total_turns", "timeout_minutes", "max_run_credits", "max_run_usd")
_SCHEDULE_ARGS = ("name", "description", "target_type", "target_id", "prompt", "schedule_type", "cron_expression",
                  "interval_value", "interval_unit")


def _workflow_call(tool: str, doc: dict, *, update: bool = False) -> dict:
    args: dict[str, Any] = {"name": doc["name"], "description": doc.get("description") or "",
                            "steps_json": json.dumps(doc["steps"], sort_keys=True),
                            "entry_step_id": doc["entry_step_id"]}
    if doc.get("state_schema"):
        args["state_schema_json"] = json.dumps(doc["state_schema"], sort_keys=True)
    args |= {k: doc[k] for k in _CAPS if doc.get(k) is not None}
    if update:
        args["orch_id"] = doc["id"]
    return {"tool": tool, "args": args, "makes": None if update else doc["id"], "kind": "orchestration",
            "name": doc["name"], "update": update}


def save_calls(plan: O.Plan, prompts: dict[str, str], state: dict) -> list[dict]:
    """Every call the save makes, in order, as the approval covers them: ids the build makes are placeholders. Pure,
    and the same each time for the same Plan: `approve_plan` records these, `save_build` makes them."""
    from core.native_builder.build_plan import _orch_id
    from core.native_builder.compile import compile_plan

    compiled = compile_plan(plan, mint=placeholder_mint(), orch_id=_orch_id(plan, state))
    calls: list[dict] = []
    for agent in sorted(compiled.agents, key=lambda a: a.get("type") == "delegate"):  # members before their team
        args: dict[str, Any] = {"name": agent["name"], "description": agent.get("description") or "",
                                "type": agent["type"], "tools": list(agent.get("tools") or []),
                                "system_prompt": prompts.get(agent["name"]) or agent.get("description") or ""}
        args |= {k: agent[k] for k in ("delegate_agent_ids", "team", "tool_policy") if agent.get(k)}
        calls.append({"tool": "create_agent", "args": args, "makes": agent["id"], "kind": "agent",
                      "name": agent["name"]})
    for inner in compiled.inner:
        calls.append(_workflow_call("create_orchestration", inner.orchestration))
    main = compiled.orchestration
    if plan.kind == "update":
        _keep_places(main, _obj(state.get(EXISTING_KEY)))
    calls.append(_workflow_call("update_orchestration" if plan.kind == "update" else "create_orchestration", main,
                                update=plan.kind == "update"))
    meta = {k: main[k] for k in ("result_key", "strict_state_types") if main.get(k) not in (None, "", False)}
    if meta:
        calls.append({"tool": "set_orchestration_meta", "args": {"orch_id": main["id"], **meta}, "makes": None,
                      "kind": "meta", "name": main["name"]})
    for board in compiled.boards:
        calls.append({"tool": "create_board", "args": {"name": board["name"],
                                                       "description": board.get("description") or ""},
                      "makes": None, "kind": "board", "name": board["name"]})
    if compiled.schedule:
        calls.append({"tool": "create_schedule",
                      "args": {k: v for k, v in compiled.schedule.items() if k in _SCHEDULE_ARGS and v is not None},
                      "makes": None, "kind": "schedule", "name": compiled.schedule["name"]})
    return calls


def _keep_places(doc: dict, existing: Any) -> None:
    """An update keeps where its steps sit on the canvas: the compiler places only what a Plan cannot say."""
    if not isinstance(existing, dict):
        return
    at = {s.get("id"): s for s in existing.get("steps") or [] if isinstance(s, dict)}
    for step in doc["steps"]:
        old = at.get(step["id"])
        if old is not None:
            for key in ("position_x", "position_y"):
                if old.get(key) is not None:
                    step[key] = old[key]


def _filled(value: Any, made: dict[str, str]) -> Any:
    """`value` with each placeholder this pass has made replaced by its real id."""
    if not made:
        return value
    if isinstance(value, str):
        for placeholder in sorted(made, key=len, reverse=True):
            value = value.replace(placeholder, made[placeholder])
        return value
    if isinstance(value, dict):
        return {k: _filled(v, made) for k, v in value.items()}
    if isinstance(value, list):
        return [_filled(v, made) for v in value]
    return value


# ── the yes ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def _ids_now() -> dict[str, list[str]]:
    from core.routes.agents import load_user_agents
    from core.store import schedules
    from core.store.resources import load_orchestrations

    return {"agents": sorted(a["id"] for a in await load_user_agents() if a.get("id")),
            "workflows": sorted(o["id"] for o in await load_orchestrations() if o.get("id")),
            "schedules": sorted(s["id"] for s in await schedules.load() if s.get("id"))}


async def approve_plan(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder import review
    from core.product_tools.approved import digest

    run_id, run = _run()
    if run is None:
        return {"error": "There is no plan waiting here to approve."}
    state = run.shared_state
    answer = _obj(state.get(ANSWER_KEY))
    answer = answer.get("feedback") if isinstance(answer, dict) else answer
    if not review.is_yes(answer):
        return {"error": "The plan was not approved, so nothing is built."}
    checked = checked_in(state)
    if checked.get("outcome") != "review" or not checked.get("plan"):
        return {"error": "There is no plan waiting here to approve."}
    if digest(reviewed_of(checked)) != checked.get("digest"):
        return {"error": "What is waiting is not what the review showed, so nothing is built."}
    if "build" not in (checked.get("offers") or []):
        return {"error": "This plan can't be built as it is: " + (checked.get("say") or "it needs something first.")}
    calls = save_calls(_plan_of(checked), _prompts(state), state)
    review.record_approval(state, reviewed_of(checked), _caller(), [(c["tool"], c["args"]) for c in calls],
                           run_id=run_id)
    return {"approved": len(calls), "before": await _ids_now()}


# ── the save ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _stopped(events: list[dict], reason: str) -> dict:
    events.append({"type": "build_stopped", "reason": reason})
    return {"outcome": "stopped", "reason": reason, "effect": ""}


def _why_missing(gone: list[str], plan: O.Plan, state: dict) -> str:
    """F768's words, from the Plan's own tool status and the lookup's findings."""
    findings = _obj(state.get(FINDINGS_KEY)) or {}
    providers = {t.provider for t in plan.tools if t.name in gone and t.provider}
    found = []
    for option in (findings.get("options") or []) if isinstance(findings, dict) else []:
        if isinstance(option, dict) and option.get("provider") in providers and option.get("how") in ("mcp", "api"):
            way = "its own MCP server" if option["how"] == "mcp" else "a public API"
            page = next(iter(option.get("sources") or []), "")
            found.append(f"{option['provider']} has {way}" + (f" ({page})" if page else ""))
    names = ", ".join(f"`{t}`" for t in gone[:5])
    return ("Nothing was built: this plan uses " + names + ", which nothing in this workspace provides now. "
            + (("The research found: " + "; ".join(found) + ". ") if found else "")
            + "Connect the app or service it needs, then ask me to build it again.")


def _issues_reason(issues: list[str]) -> str:
    from core.builder_tools import _issues_said

    return "Nothing was built: it would not pass validation: " + "; ".join(_issues_said(issues)) + "."


async def _made_before(call: dict, made: dict[str, str], before: dict[str, list[str]]) -> str | None:
    """What an earlier pass of this save made for `call`: a thing of its kind with its name that the workspace did
    not hold at the yes (a restart forgets OSS's spent-once claims; MCP2's re-check, D)."""
    from core.routes.agents import load_user_agents
    from core.store import schedules
    from core.store.resources import load_orchestrations

    if call["kind"] == "agent":
        rows, held = await load_user_agents(), before.get("agents") or []
    elif call["kind"] == "orchestration" and not call.get("update"):
        rows, held = await load_orchestrations(), before.get("workflows") or []
    elif call["kind"] == "schedule":
        target = _filled(call["args"].get("target_id"), made)
        rows = [s for s in await schedules.load() if s.get("target_id") == target]
        held = before.get("schedules") or []
    else:
        return None
    return next((r["id"] for r in rows if r.get("name") == call["name"] and r.get("id") not in held), None)


def _id_of(call: dict, result: Any) -> str | None:
    parsed = _obj(result)
    if not isinstance(parsed, dict):
        return None
    nested = parsed.get("agent" if call["kind"] == "agent" else "orchestration")
    for record in (nested, parsed):
        if isinstance(record, dict) and record.get("id"):
            return str(record["id"])
    return call["args"].get("orch_id")


async def _undo(done: list[tuple[str, str, str]], state: dict, events: list[dict], why: str) -> None:
    """Remove what this pass made, newest first, as `finish_build` removes a build's leftovers (F156, F492): the
    person reads each removal and why. An updated workflow goes back to how it was."""
    from core import agent_memory
    from core.builder_tools import _emit
    from core.store import boards as board_store
    from core.store import schedules
    from core.store.resources import delete_agent, delete_orchestration, save_orchestration

    reason = f"{UNDONE}: {why}"
    for kind, real, name in reversed(done):
        try:
            if kind == "agent":
                await delete_agent(real)
                await agent_memory.wipe(real)
                _emit(events, "agent", "deleted", real, name, reason=reason)
            elif kind == "orchestration":
                await delete_orchestration(real)
                _emit(events, "orchestration", "deleted", real, name, reason=reason)
            elif kind == "updated":
                existing = _obj(state.get(EXISTING_KEY))
                if isinstance(existing, dict) and existing.get("id") == real:
                    await save_orchestration(existing)
                    _emit(events, "orchestration", "updated", real, name, reason=f"put back as it was: {reason}")
            elif kind == "schedule":
                await schedules.delete_one(real)
                _emit(events, "schedule", "deleted", real, name, reason=reason)
            elif kind == "board":
                await board_store.delete_board(real)
        except Exception as exc:  # noqa: BLE001 — one removal that fails never stops the others
            _log.warning("could not remove %s %s after a stopped build: %s", kind, real, exc)


async def _read_back(calls: list[dict], made: dict[str, str]) -> str:
    """What the save left, read again: each workflow's steps as compiled, each agent there, the schedule paused. ""
    when it all reads as made; otherwise the plain words for what does not (the "change without effect" class)."""
    from core.routes.agents import load_user_agents
    from core.store import schedules
    from core.store.resources import get_orchestration

    agents = {a.get("id") for a in await load_user_agents()}
    said = []
    for call in calls:
        if call["kind"] == "agent" and made.get(call["makes"]) not in agents:
            said.append(f"the agent \"{call['name']}\" is not there")
        if call["kind"] == "orchestration":
            real = made.get(call["makes"]) if call["makes"] else call["args"].get("orch_id")
            saved = await get_orchestration(real) if real else None
            want = [s["id"] for s in json.loads(_filled(call["args"]["steps_json"], made))]
            if saved is None:
                said.append(f"the workflow \"{call['name']}\" is not there")
            elif sorted(s.get("id") for s in saved.get("steps") or []) != sorted(want):
                said.append(f"the workflow \"{call['name']}\" does not have the steps the plan has")
        if call["kind"] == "schedule":
            target = _filled(call["args"].get("target_id"), made)
            found = [s for s in await schedules.load()
                     if s.get("target_id") == target and s.get("name") == call["name"]]
            if not found:
                said.append(f"the schedule \"{call['name']}\" is not there")
            elif any(s.get("enabled") for s in found):
                said.append(f"the schedule \"{call['name']}\" is on, though it is made paused")
    return (" I saved it, but on reading it back " + "; ".join(said) + ".") if said else ""


async def save_build(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.native_builder.review import approved_review
    from core.product_tools import areas, build, registry

    run_id, run = _run()
    if run is None:
        return _stopped(events, "Nothing was built: there is no approved plan here.")
    state = run.shared_state
    checked = checked_in(state)
    approval = approved_review(state)
    if approval is None or not checked.get("plan"):
        return _stopped(events, "Nothing was built: the plan was not approved.")
    plan = _plan_of(checked)

    # The workspace again, before the first write: what the yes saw may have changed since.
    now = await O.fill(plan, server_module)
    gone = [t.name for t in now.tools if t.status != "present"]
    if gone:
        return _stopped(events, _why_missing(gone, plan, state))
    issues = [i for i in await dry_issues(plan, server_module, await workspace(server_module), state)]
    if issues:
        return _stopped(events, _issues_reason(issues))

    calls = save_calls(plan, _prompts(state), state)
    reviewed = reviewed_of(checked)
    before = (_obj(state.get(APPROVAL_KEY)) or {}).get("before") or {}
    caller = _caller()
    areas.ensure_loaded()
    made: dict[str, str] = {}
    done: list[tuple[str, str, str]] = []
    failure = ""
    with build.using_module(server_module), build.collecting_events(events):
        for call in calls:
            earlier = await _made_before(call, made, before)
            if earlier:
                if call.get("makes"):
                    made[call["makes"]] = earlier
                done.append((call["kind"], earlier, call["name"]))
                continue
            try:
                result = await registry.invoke(call["tool"], caller, _filled(call["args"], made),
                                               confirm_policy="approved_review", review=approval, reviewed=reviewed,
                                               made={real: placeholder for placeholder, real in made.items()})
            except AlreadySpent:
                result = None
            except ToolRefusal as why:
                failure = f"{call['name']}: {why}"
                break
            parsed = _obj(result)
            if isinstance(parsed, dict) and parsed.get("status") == "needs_confirmation":
                failure = f"{call['name']}: your approval does not cover this call"
                break
            if isinstance(parsed, dict) and parsed.get("error"):
                failure = f"{call['name']}: {parsed['error']}"
                break
            real = _id_of(call, parsed) or await _made_before(call, made, before)
            if call.get("makes"):
                if not real:
                    failure = f"{call['name']}: it was not made"
                    break
                made[call["makes"]] = real
            if real and call["kind"] in ("agent", "orchestration", "schedule", "board"):
                done.append(("updated" if call.get("update") else call["kind"], real, call["name"]))
    if failure:
        reason = " ".join(failure.split()).rstrip(". ")
        await _undo(done, state, events, reason)
        return _stopped(events, f"Nothing was built: {reason}.")
    left = [c for c in calls if c.get("makes")]
    return {"outcome": "saved", "made": {c["name"]: made[c["makes"]] for c in left if c["makes"] in made},
            "effect": await _read_back(calls, made)}


register_builder_tool(SUBMIT_PROMPTS, submit_prompts)
for _name, _handler, _says in (("approve_plan", approve_plan, "Synapse AI's record of the person's yes to a build "
                                                              "(code)."),
                               ("save_build", save_build, "Synapse AI's save of an approved build (code).")):
    register_builder_tool({"type": "function", "function": {
        "name": _name, "description": _says, "parameters": {"type": "object", "properties": {}}}}, _handler)

__all__ = ["MADE", "save_calls"]
