"""Synapse AI's build route after the person's yes: the agents' instructions, the approval, and the save, in code
(SAI2-P3, D300; F940, F941).

* `submit_prompts` (the prompt writer's E2 finish): instructions for each of the Plan's new agents, by name, and what
  each was written for. Written before the review, shown on the card and covered by the digest, so the person's yes
  covers exactly what is saved (Naveen, 10-23, Q2 as revised after MCP2's review).
* `approve_plan` (E1, on F754's yes side): checks the yes itself, that what waits is what the card showed (the digest
  `check_build` took), and that the card offered Build it; then records the person's approval of exactly the calls
  `save_build` makes (F778, `review.record_approval`), with placeholders for the ids the build makes (Q1, F940), and
  what the workspace held then, so a save run again knows what it made.
* `save_build` (E1, `step_save`): one pass, no model (F867c by construction). It checks the workspace again before
  the first write — a tool gone since the yes, or a rule broken, stops it with nothing written and says why (F768,
  F867a) — then makes each call through `registry.invoke(approved_review)`, as the person: agents (members before
  their team), inner workflows (E3), the workflow (or, for an update, over its own id), its settings, boards and the
  schedule, made paused (F149/F150). F695's sink gives the run each `resource_saved`, so `run_writes` says what was
  made. It records each id it makes in the run's progress as it goes, so a re-run after a restart takes what it
  recorded and never anything by name; a thing of the plan's name made since the yes stops it (MCP2's finding 1). On
  a failure it removes only what it recorded making, and says why (`build_stopped`). Afterwards it reads back what
  it saved.
"""
from __future__ import annotations

import json
from typing import Any

from core.builder_tools import register_builder_tool
from core.log import get_logger
from core.native_builder import objects as O
from core.native_builder.build_plan import (FINDINGS_KEY, MADE, _obj, _state, agent_fingerprint, checked_in,
                                            dry_issues, placeholder_mint, reviewed_of, workspace)
from core.native_builder.change_tools import _caller, _run
from core.product_tools.errors import AlreadySpent, ToolRefusal

_log = get_logger(__name__)

ANSWER_KEY, APPROVAL_KEY = "review_feedback", "plan_approval"
EXISTING_KEY = "existing_orch"
#: What a removal after a failed save says, beside the failure itself.
UNDONE = "the build stopped before it was saved whole"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()) if not isinstance(value, str) else value.strip()


def _plan_of(checked: dict) -> O.Plan:
    return O.Plan.model_validate(checked.get("plan") or {})


def _prompts(checked: dict) -> dict[str, str]:
    """The new agents' instructions the card showed (and the digest covers), never the run's working copy."""
    raw = checked.get("prompts")
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
    from core.native_builder.change_tools import _holds_a_key

    checked = checked_in(_state())
    agents = (checked.get("plan") or {}).get("agents") or []
    names = [a.get("name") for a in agents]
    given = {_text(p.get("agent")): _text(p.get("instructions")) for p in args.get("prompts") or []
             if isinstance(p, dict)}
    unknown = [n for n in given if n not in names]
    empty = [n for n in names if not given.get(n)]
    if unknown or empty:
        return {"error": "Write instructions for each of the plan's new agents, by its name: "
                         + ", ".join(f'"{n}"' for n in names) + "; and for no other."}
    if _holds_a_key(given):  # a key, or the door's mark where one was (SAI2's review, 3)
        return {"error": "Instructions never hold a key, a token or a password: the agent's tools carry those."}
    if any(MADE in text for text in given.values()):
        return {"error": f"Instructions never hold the text {MADE}: it is how the save names an id it has yet to make."}
    # What each was written for, so a plan revised since is written for again (`build_plan.prompts_for`).
    written_for = {a["name"]: (checked.get("for") or {}).get(a["name"]) or agent_fingerprint(a) for a in agents}
    return {"finish": "submit_prompts", "prompts": given, "for": written_for}


# ── the calls a save makes ────────────────────────────────────────────────────────────────────────────────────────

_CAPS = ("max_total_turns", "timeout_minutes", "max_run_credits", "max_run_usd")
_SCHEDULE_ARGS = ("name", "description", "target_type", "target_id", "prompt", "schedule_type", "cron_expression",
                  "interval_value", "interval_unit", "timezone")


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
    from core.native_builder.build_plan import _orch_id, inner_order
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
    for inner in inner_order(compiled)[0]:  # each before any workflow of the build that runs it (SAI2's review, 2)
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
        # Back to the same review, with why (SAI2's review, 18): no call is spent, and the plan stays open.
        return {"error": "This plan can't be built as it is yet. "
                         + (checked.get("say") or "It needs something first."), "refused": "not_offered"}
    calls = save_calls(_plan_of(checked), _prompts(checked), state)
    review.record_approval(state, reviewed_of(checked), _caller(), [(c["tool"], c["args"]) for c in calls],
                           run_id=run_id)
    return {"approved": len(calls), "before": await _ids_now()}


# ── the save ──────────────────────────────────────────────────────────────────────────────────────────────────────

_SHAPE = ("name", "description", "entry_step_id", "steps", "state_schema", "result_key", "max_total_turns",
          "timeout_minutes", "max_run_credits", "max_run_usd")


async def _moved_since_read(state: dict) -> str | None:
    """The workflow's name when someone changed it after it was read for the review (P2's `_moved_since`, for a
    whole workflow: SAI2's review, 12); None when it reads as it did."""
    from core.store.resources import get_orchestration

    read = _obj(state.get(EXISTING_KEY))
    if not isinstance(read, dict) or not read.get("id"):
        return None
    now = await get_orchestration(read["id"])
    if now is None:
        return read.get("name") or read["id"]
    shape = lambda o: json.dumps({k: o.get(k) for k in _SHAPE}, sort_keys=True, default=str)  # noqa: E731
    return None if shape(now) == shape(read) else (now.get("name") or read["id"])


def _stopped(events: list[dict], reason: str) -> dict:
    events.append({"type": "build_stopped", "reason": reason})
    return {"outcome": "stopped", "reason": reason, "effect": ""}


def _gates_moved(reviewed: list[O.Gate], now: list[O.Gate]) -> list[str]:
    """Each approval or block that is not what the card said, in the card's words: was, and is."""
    from core.native_builder.render import gate_words

    def key(g: O.Gate) -> tuple[str, str, str]:
        return g.kind, g.step or g.who, g.tool

    was, is_ = {key(g): g for g in reviewed}, {key(g): g for g in now}
    out = []
    for k in dict.fromkeys([*was, *is_]):
        before, after = was.get(k), is_.get(k)
        if before == after:
            continue
        name = f"{(before or after).who} and `{k[2]}`"
        out.append(f"{name}: the card said \"{gate_words(before)}\", now "
                   + (f"\"{gate_words(after)}\"" if after else "it acts without asking") if before
                   else f"{name}: now \"{gate_words(after)}\"")
    return out


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


#: Where a save keeps what it has made, call by call, in the run's checkpointed progress: a re-run (a resume, a
#: redelivered job) takes what it recorded making, by id, and never anything by name (MCP2's finding 1).
PROGRESS_KEY = "build_save"


async def _taken_since(call: dict, made: dict[str, str], before: dict[str, list[str]]) -> bool:
    """Whether a thing of `call`'s kind and name was made since the yes by someone else: the workspace did not hold
    it then, and this save did not record making it. Its name is no proof it is ours, so the build stops."""
    from core.routes.agents import load_user_agents
    from core.store.resources import load_orchestrations

    if call["kind"] == "agent":
        rows, held = await load_user_agents(), before.get("agents") or []
    elif call["kind"] == "orchestration" and not call.get("update"):
        rows, held = await load_orchestrations(), before.get("workflows") or []
    else:
        return False
    ours = set(made.values())
    return any(r.get("name") == call["name"] and r.get("id") not in held and r.get("id") not in ours for r in rows)


async def _keep(run: Any, approval_id: str, made: dict[str, str], done: list[tuple[str, str, str]],
                calls_done: list[int]) -> None:
    """Record what this save has made so far, under the approval it saves for, and checkpoint it before the next
    call."""
    from core.orchestration.state import SharedState

    run.step_progress[PROGRESS_KEY] = {"approval": approval_id, "made": dict(made), "done": [list(d) for d in done],
                                       "calls_done": list(calls_done)}
    try:
        await SharedState(run).checkpoint(columns=("step_progress",))
    except Exception as exc:  # noqa: BLE001 — the record stays in the live run; a checkpoint store may be down
        _log.warning("the save's progress for run %s was not checkpointed: %s", run.run_id, exc)


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
            wanted = json.loads(_filled(call["args"]["steps_json"], made))
            shape = lambda steps: sorted(  # noqa: E731
                (s.get("id"), s.get("type"), s.get("forced_tool"), s.get("agent_id"), s.get("workflow_id"))
                for s in steps)
            if saved is None:
                said.append(f"the workflow \"{call['name']}\" is not there")
            elif shape(saved.get("steps") or []) != shape(wanted):
                # Each step's tool, agent and the workflow it runs, not only its id (SAI2's review, 24).
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
    if moved := _gates_moved(plan.gates, now.gates):
        # The card promised what the run asks for (F964; SAI2's point 3): a policy changed since is not what was
        # approved.
        return _stopped(events, "Nothing was built: what the run asks you for changed since your yes — "
                                + "; ".join(moved) + ". Ask again to see the plan as it is now.")
    issues = [i for i in await dry_issues(plan, server_module, await workspace(server_module), state)]
    if issues:
        return _stopped(events, _issues_reason(issues))
    if plan.kind == "update" and (moved := await _moved_since_read(state)):
        return _stopped(events, f"Nothing was changed: the workflow \"{moved}\" was changed since I read it for "
                                "your review, so I left it as it is now. Ask again to change it.")

    calls = save_calls(plan, _prompts(checked), state)
    reviewed = reviewed_of(checked)
    before = (_obj(state.get(APPROVAL_KEY)) or {}).get("before") or {}
    caller = _caller()
    areas.ensure_loaded()
    kept = run.step_progress.get(PROGRESS_KEY) or {}
    if kept.get("approval") != approval.id:
        kept = {}  # another yes's record: this approval's save starts clean (MCP2's note 1)
    made: dict[str, str] = dict(kept.get("made") or {})
    done: list[tuple[str, str, str]] = [tuple(d) for d in kept.get("done") or []]  # type: ignore[misc]
    calls_done: list[int] = list(kept.get("calls_done") or [])
    failure = ""
    with build.using_module(server_module), build.collecting_events(events):
        for n, call in enumerate(calls):
            if n in calls_done:
                continue  # made by an earlier pass of this save, by the id it recorded
            if await _taken_since(call, made, before):
                noun = "a workflow" if call["kind"] == "orchestration" else "an agent"
                # Its own, unrecorded (a worker that stopped between a make and its record), or someone else's:
                # neither is taken, and nothing is blamed (MCP2's note 2).
                failure = (f'{noun} called "{call["name"]}" is there now that was not at your yes; it may be one an '
                           "interrupted save made, so I made nothing more")
                break
            try:
                result = await registry.invoke(call["tool"], caller, _filled(call["args"], made),
                                               confirm_policy="approved_review", review=approval, reviewed=reviewed,
                                               made={real: placeholder for placeholder, real in made.items()})
            except AlreadySpent:
                failure = (f"{call['name']}: an earlier pass of this save made this call and did not record what it "
                           "made")
                break
            except ToolRefusal as why:
                failure = f"{call['name']}: {why}"
                break
            except Exception as exc:  # noqa: BLE001 — anything else a handler raises still cleans up and is said
                _log.warning("build save call %s failed: %s", call["tool"], exc)
                failure = f"{call['name']}: it failed ({type(exc).__name__})"
                break
            parsed = _obj(result)
            if isinstance(parsed, dict) and parsed.get("status") == "needs_confirmation":
                failure = f"{call['name']}: your approval does not cover this call"
                break
            if isinstance(parsed, dict) and parsed.get("error"):
                failure = f"{call['name']}: {parsed['error']}"
                break
            real = _id_of(call, parsed)
            if call.get("makes"):
                if not real:
                    failure = f"{call['name']}: it was not made"
                    break
                made[call["makes"]] = real
            if real and call["kind"] in ("agent", "orchestration", "schedule", "board"):
                done.append(("updated" if call.get("update") else call["kind"], real, call["name"]))
            calls_done.append(n)
            await _keep(run, approval.id, made, done, calls_done)
    if failure:
        reason = " ".join(failure.split()).rstrip(". ")
        await _undo(done, state, events, reason)
        run.step_progress.pop(PROGRESS_KEY, None)  # what it recorded is gone again
        return _stopped(events, f"Nothing was built: {reason}.")
    left = [c for c in calls if c.get("makes")]
    notes = "".join(f" {n}" for n in checked.get("notes") or [])  # what the card said it does not do, said again
    return {"outcome": "saved", "made": {c["name"]: made[c["makes"]] for c in left if c["makes"] in made},
            "effect": await _read_back(calls, made) + notes}


register_builder_tool(SUBMIT_PROMPTS, submit_prompts)
for _name, _handler, _says in (("approve_plan", approve_plan, "Synapse AI's record of the person's yes to a build "
                                                              "(code)."),
                               ("save_build", save_build, "Synapse AI's save of an approved build (code).")):
    register_builder_tool({"type": "function", "function": {
        "name": _name, "description": _says, "parameters": {"type": "object", "properties": {}}}}, _handler)

__all__ = ["MADE", "save_calls"]
