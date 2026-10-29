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

import contextlib
import json
import re
from typing import Any

from core import http_tool
from core.builder_tools import register_builder_tool
from core.log import get_logger
from core.native_builder import objects as O, one_writer
from core.native_builder.build_plan import (FINDINGS_KEY, MADE, _obj, _orch_id, _state, agent_fingerprint, checked_in,
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
    # A read API's tools first (F944): `step_make_tools` makes them, and the person connects them, before the rest.
    calls: list[dict] = [_tool_call(t) for t in plan.tools if t.status == "found" and t.via == "http_tool" and t.api]
    for kb in compiled.knowledge:
        # The knowledge base the plan needs (F1224): made unless one of its name is here, then given the files the
        # person has in the Vault, as the Knowledge screen makes and fills one. Before the agents and steps naming it.
        if not kb["existing"]:
            calls.append({"tool": "create_knowledge_base", "args": {"name": kb["name"],
                                                                    "description": kb.get("description") or ""},
                          "makes": kb["id"], "kind": "knowledge_base", "name": kb["name"]})
        calls += [{"tool": "add_knowledge_file", "args": {"knowledge_base": kb["name"], "path": path},
                   "makes": None, "kind": "knowledge_file", "name": f"{kb['name']}: {path}"} for path in kb["files"]]
    for agent in sorted(compiled.agents, key=lambda a: a.get("type") == "delegate"):  # members before their team
        args: dict[str, Any] = {"name": agent["name"], "description": agent.get("description") or "",
                                "type": agent["type"], "tools": list(agent.get("tools") or []),
                                "system_prompt": prompts.get(agent["name"]) or agent.get("description") or ""}
        # The model code picked (F966), which the card showed and the yes covers.
        args |= {k: agent[k] for k in ("delegate_agent_ids", "team", "tool_policy", "model", "boards", "memory",
                                       "knowledge", "browser") if agent.get(k)}
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
    if plan.eval is not None:
        calls += _eval_calls(plan, main)
    for board in compiled.boards:
        # The board the person asked for (F1214): a shared board, made unless one of that name is here, and this
        # workflow given it to work, through the access list the Boards screen edits. Its new agents post there.
        if not board.get("existing"):
            calls.append({"tool": "create_board", "args": {"name": board["name"],
                                                           "description": board.get("description") or ""},
                          "makes": None, "kind": "board", "name": board["name"]})
        calls.append({"tool": "give_board_access", "args": {"board": board["name"], "kind": "workflow",
                                                            "id": main["id"], "access": "work"},
                      "makes": None, "kind": "board_access", "name": board["name"]})
    if compiled.schedule:
        calls.append({"tool": "create_schedule",
                      "args": {k: v for k, v in compiled.schedule.items() if k in _SCHEDULE_ARGS and v is not None},
                      "makes": None, "kind": "schedule", "name": compiled.schedule["name"]})
    return calls


#: The rubric a build's eval makes, as its later calls name it before it is made (F1255).
EVAL_RUBRIC = f"{MADE}rubric:eval"


def _eval_calls(plan: O.Plan, main: dict) -> list[dict]:
    """A plan's eval, made after its workflow through the Evals screen's own tools (F1255, D340 F): the rubric, then
    the golden set of the cases the card listed, and the rubric attached last, so a stop never leaves the workflow
    scored by a rubric that was removed."""
    from core.native_builder.build_plan import rubric_of

    part, workflow = plan.eval, main["id"]
    # An update keeps the rubric its workflow is scored by unless the ask was about how it is judged (D340 F).
    calls = [] if part.keeps else [{"tool": "save_rubric", "args": rubric_of(part), "makes": EVAL_RUBRIC,
                                    "kind": "rubric", "name": part.name}]
    if part.cases:
        calls.append({"tool": "save_eval_cases", "args": {
            "workflow": workflow, "name": f"{main['name']} golden set",
            "cases": [c.model_dump() for c in part.cases]}, "makes": None, "kind": "eval_suite",
            "name": f"{main['name']} golden set"})
    if not part.keeps:
        calls.append({"tool": "attach_rubric", "args": {"workflow": workflow, "rubric": EVAL_RUBRIC}, "makes": None,
                      "kind": "rubric_attach", "name": part.name})
    return calls


#: A host part a made tool keeps until its Connect card fills it: never filled from arguments (A8).
def _unfilled(base_url: str) -> str:
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: f"__{m.group(1).lower()}__", base_url, count=1)


def _tool_call(status: O.ToolStatus) -> dict:
    """One endpoint of a read API as the `create_http_tool` call that makes it (F944): its URL on the provider's own
    host with its account part kept as `__dc__`, its base locked (A8), its arguments the path's placeholders and, for a
    call that sends, a `body`; its tags from its method, which claims only tighten."""
    api, endpoint = status.api or {}, (status.api or {}).get("endpoint") or {}
    base = _unfilled(str(api.get("base_url") or "")).rstrip("/")
    method, path = str(endpoint.get("method") or "GET").upper(), str(endpoint.get("path") or "")
    if own := api.get("tool"):
        # The person's own API (F1225): the operation as the spec's import makes it, every argument it has (a GET
        # sends the rest as its query, a call that sends as its JSON), on the spec's own server, its base locked.
        args = {"name": status.name, "method": method, "url": own["url"], "base_lock": base,
                "group": status.provider or "", "input_schema_json": json.dumps(own["inputSchema"]),
                "description": f"{own['description']} ({status.provider}: {method} {path})"}
        if method == "GET":
            args["read_only"] = True
        elif method == "DELETE":
            args["destructive"] = True
        return {"tool": "create_http_tool", "args": args, "makes": None, "kind": "tool", "name": status.name}
    params = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", path)
    properties: dict[str, Any] = {p: {"type": "string"} for p in params}
    args: dict[str, Any] = {"name": status.name, "method": method, "url": base + path, "base_lock": base,
                            "group": status.provider or "",
                            "description": f"{endpoint.get('does') or status.name} ({status.provider}: {method} {path})"}
    if method in ("POST", "PUT", "PATCH"):
        properties["body"] = {"type": "object", "description": f"The JSON it sends, as {api.get('docs_url')} says"}
        args["body_template"] = "{{body}}"
    if method == "GET":
        args["read_only"] = True
    elif method == "DELETE":
        args["destructive"] = True
    args["input_schema_json"] = json.dumps({"type": "object", "properties": properties, "required": params})
    return {"tool": "create_http_tool", "args": args, "makes": None, "kind": "tool", "name": status.name}


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
    return {"outcome": "stopped", "reason": reason, "effect": "", "does": ""}


async def _models_moved(plan: O.Plan, checked: dict) -> list[str]:
    """Each new agent whose model code would pick now is not the one the yes saw (F966), in words. A menu that can't
    be read now keeps what was approved."""
    from core.native_builder.build_plan import pick_models

    now, issues, question = await pick_models(plan, list(checked.get("stronger") or []))
    if not now and not issues and not question:
        return []
    out = []
    for a in plan.agents:
        was, picked = plan.models.get(a.name), now.get(a.name)
        if (was.model if was else None) != (picked.model if picked else None):
            out.append(f"{a.name} was to run on {was.name if was else 'the workspace default'}, and would now run "
                       f"on {picked.name if picked else 'no model here'}")
    return out


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

    if call["kind"] == "tool":
        # A tool named so since the check (where a taken name was made `_2`) is someone else's: never overwritten.
        from core.store.resources import load_tools

        return any(t.get("name") == call["name"] and t.get("name") not in made.values() for t in await load_tools())
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
    nested = parsed.get({"agent": "agent", "rubric": "rubric", "eval_suite": "golden_set"}.get(call["kind"],
                                                                                               "orchestration"))
    for record in (nested, parsed):
        if isinstance(record, dict) and record.get("id"):
            return str(record["id"])
    return call["args"].get("orch_id")


async def _undo(done: list[tuple[str, str, str]], state: dict, events: list[dict], why: str, *,
                caller: Any = None) -> None:
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
            elif kind == "board_access":
                # Only the entry this pass gave: the rest of the list stays, and an entry it replaced comes back.
                given = json.loads(real)
                rows = [(a.principal_kind, a.principal_id, a.access)
                        for a in await board_store.list_access(given["board"])
                        if (a.principal_kind, a.principal_id) != (given["kind"], given["id"])]
                if given.get("was"):
                    rows.append((given["kind"], given["id"], given["was"]))
                await board_store.set_access(given["board"], rows)
            elif kind == "knowledge_base":
                # As its Delete does (F1218): its index and records go; its Vault folder and files stay.
                from core.product_tools import knowledge as K

                await (K._host.delete_base or K._oss_delete_base)(caller, real)
                _emit(events, "knowledge_base", "deleted", real, name, reason=reason)
            elif kind in ("rubric", "eval_suite"):
                # As the Evals screen's Delete does (F1255): a golden set's past runs stay; it has none yet.
                from core.product_tools import evals

                await evals.remove(caller, kind, real)
                _emit(events, kind, "deleted", real, name, reason=reason)
            elif kind == "tool":
                from core.store.resources import delete_tool_named

                await delete_tool_named(real)
                _emit(events, "tool", "deleted", real, name, reason=reason)
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

    # The workspace again, before the first write: what the yes saw may have changed since. A read API's tools are
    # this build's own to make (F944), so they are found, not gone.
    only = str(args.get("only") or "")
    making = {t.name: t for t in plan.tools if t.status == "found" and t.via == "http_tool" and t.api}
    # An app's or an MCP server's tools are connected after the yes (F949): not here yet while the tools are made;
    # the save itself needs every one of them here.
    later = {t.name for t in plan.tools if t.status == "found" and t.via in ("catalog_app", "mcp_server")}
    now = await O.fill(plan, server_module, found=making)
    gone = [t.name for t in now.tools if t.status != "present" and t.name not in making
            and not (only == "tools" and t.name in later)]
    if gone:
        return _stopped(events, _why_missing(gone, plan, state))
    if moved := _gates_moved(plan.gates, now.gates):
        # The card promised what the run asks for (F964; SAI2's point 3): a policy changed since is not what was
        # approved.
        return _stopped(events, "Nothing was built: what the run asks you for changed since your yes — "
                                + "; ".join(moved) + ". Ask again to see the plan as it is now.")
    if moved := await _models_moved(plan, checked):
        # The card said which model each new agent runs on, and the yes covered it: one this workspace can no longer
        # call, or a better fit since, is not what was approved.
        return _stopped(events, "Nothing was built: the models this workspace can use changed since your yes — "
                                + "; ".join(moved) + ". Ask again to see the plan as it is now.")
    issues = [i for i in await dry_issues(plan, server_module, await workspace(server_module), state)
              # A tool this build makes is not there until `step_make_tools` makes it (F944), nor one connected after
              # the yes until it is (F949).
              if not ("nothing here provides" in i and any(f"'{n}'" in i or f"`{n}`" in i for n in
                                                           [*making, *(later if only == "tools" else [])]))]
    if issues:
        return _stopped(events, _issues_reason(issues))
    if plan.kind == "update" and (moved := await _moved_since_read(state)):
        return _stopped(events, f"Nothing was changed: the workflow \"{moved}\" was changed since I read it for "
                                "your review, so I left it as it is now. Ask again to change it.")
    # One chat writes a workflow at a time (TABS2, F1063): a chat whose turn began earlier and still holds this one goes
    # first, checked before anything is made and again under the workflow's lock at its own write.
    target = (_orch_id(plan, state) or "") if plan.kind == "update" else ""
    if target and (held := await one_writer.held_by(target, run.session_id, run_id=run_id)):
        return _stopped(events, one_writer.refusal(held, _caller().user_id)["say"])

    calls = save_calls(plan, _prompts(checked), state)
    reviewed = reviewed_of(checked)
    before = (_obj(state.get(APPROVAL_KEY)) or {}).get("before") or {}
    caller = _caller()
    areas.ensure_loaded()
    kept = run.step_progress.get(PROGRESS_KEY) or {}
    if kept.get("approval") != approval.id:
        # Another yes's record: this approval's save starts clean (MCP2's note 1), but for the tools an earlier yes of
        # this build made and never saved (F949's second yes): carried, so a stop still removes them, and the save
        # removes those the new plan no longer uses (SAI2's change 2).
        carried = [d for d in kept.get("done") or [] if d[0] == "tool"] if not kept.get("saved") else []
        kept = {"done": carried} if carried else {}
    made: dict[str, str] = dict(kept.get("made") or {})
    done: list[tuple[str, str, str]] = [tuple(d) for d in kept.get("done") or []]  # type: ignore[misc]
    calls_done: list[int] = list(kept.get("calls_done") or [])
    failure = ""
    with build.using_module(server_module), build.collecting_events(events):
        for n, call in enumerate(calls):
            if n in calls_done:
                continue  # made by an earlier pass of this save, by the id it recorded
            if only == "tools" and call["kind"] != "tool":
                break  # `step_make_tools`: the tools only; the rest after the person connects them (F944)
            if await _taken_since(call, made, before):
                noun = {"orchestration": "a workflow", "tool": "a tool"}.get(call["kind"], "an agent")
                # Its own, unrecorded (a worker that stopped between a make and its record), or someone else's:
                # neither is taken, and nothing is blamed (MCP2's note 2).
                failure = (f'{noun} called "{call["name"]}" is there now that was not at your yes; it may be one an '
                           "interrupted save made, so I made nothing more")
                break
            # A read API's tool takes its lock here, and only here (F944; SAI2's review, blocker 1).
            making = http_tool.MAKING_TOOLS.set(call["kind"] == "tool")
            writing = (one_writer.writing(target, run_id=run_id, session_id=run.session_id)
                       if target and call["kind"] == "orchestration" else contextlib.nullcontext(None))
            try:
                async with writing as held:
                    if held is not None:
                        failure = one_writer.refusal(held, caller.user_id)["say"]
                        break
                    result = await registry.invoke(call["tool"], caller, _filled(call["args"], made),
                                                   confirm_policy="approved_review", review=approval,
                                                   reviewed=reviewed,
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
            finally:
                http_tool.MAKING_TOOLS.reset(making)
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
            if real and call["kind"] in ("agent", "orchestration", "schedule", "board", "tool", "knowledge_base",
                                         "rubric", "eval_suite"):
                done.append(("updated" if call.get("update") else call["kind"], real, call["name"]))
            if real and call["kind"] == "board_access":
                given = _filled(call["args"], made)
                done.append(("board_access", json.dumps({"board": real, "kind": given["kind"], "id": given["id"],
                                                         "was": parsed.get("was")}), call["name"]))
            calls_done.append(n)
            await _keep(run, approval.id, made, done, calls_done)
    if failure:
        reason = " ".join(failure.split()).rstrip(". ")
        await _undo(done, state, events, reason, caller=caller)
        run.step_progress.pop(PROGRESS_KEY, None)  # what it recorded is gone again
        return _stopped(events, f"Nothing was built: {reason}.")
    if only == "tools":
        made_tools = [c["name"] for c in calls if c["kind"] == "tool"]
        waits, say = await _connect_cards(plan, made_tools, events)
        return {"outcome": "tools_made", "tools": made_tools, "waits": waits, "say": say}
    # A tool an earlier yes made that the plan saved no longer uses is never left behind (F949; SAI2's change 2).
    unused = [real for kind, real, _ in done if kind == "tool" and real not in O.tool_names(plan)]
    if unused:
        await _undo([("tool", n, n) for n in unused], state, events, "the plan you approved no longer uses it")
    # Saved: what it made is the build's now, never removed by a later cancel (F946).
    run.step_progress[PROGRESS_KEY] = {**(run.step_progress.get(PROGRESS_KEY) or {}), "saved": True}
    from core.orchestration.state import SharedState

    try:
        await SharedState(run).checkpoint(columns=("step_progress",))
    except Exception as exc:  # noqa: BLE001 — the mark is still on the run; its next checkpoint writes it
        _log.warning("build save of run %s not marked saved: %s", run_id, exc)
    left = [c for c in calls if c.get("makes")]
    notes = "".join(f" {n}" for n in checked.get("notes") or [])  # what the card said it does not do, said again
    from core.native_builder.parts_words import summary

    # What it does, part by part, and what is missing or off: the plan's own words as code read it at the save
    # (F1228), never a model's account of what it built.
    return {"outcome": "saved", "made": {c["name"]: made[c["makes"]] for c in left if c["makes"] in made},
            "effect": await _read_back(calls, made) + notes, "does": summary(now)}


async def _connect_cards(plan: O.Plan, names: list[str], events: list[dict]) -> tuple[list[str], str]:
    """One Connect card per provider after the yes (F945, F949): for the tools this build made for it, from the
    approved spec, its key once, its header and scheme, and what its host needs; for an app in the catalog, its own
    card; for an MCP server, the Apps screen's. Returns the providers it waits for, and what the wait says."""
    from core.builder_connect import _read_options, prepare_connection
    from core.native_builder.build_plan import _app_for

    by: dict[str, list[O.ToolStatus]] = {}
    later: dict[str, list[O.ToolStatus]] = {}
    for t in plan.tools:
        if t.name in names and t.api and not t.api.get("keyless"):
            by.setdefault(t.provider or t.name, []).append(t)
        elif t.status == "found" and t.via in ("catalog_app", "mcp_server"):
            later.setdefault(t.provider or t.name, []).append(t)
    if later:
        catalog = (await _read_options()).get("catalog") or []
        for provider, tools in later.items():
            app = _app_for(provider, catalog)
            uses = ", ".join(f"`{t.name}`" for t in tools)
            if app is not None:
                await prepare_connection({"kind": "app", "catalog_slug": str(app.get("slug")),
                                          "title": f"Connect {app.get('name')}",
                                          "why": f"This build uses {uses}."}, None, events)
            else:
                await prepare_connection({"kind": "settings", "screen": "apps", "title": f"Connect {provider}",
                                          "why": f"Add {provider}'s MCP server under Apps and MCP servers: this "
                                                 f"build uses {uses}.", "waits_for": provider}, None, events)
    for provider, tools in by.items():
        api = tools[0].api or {}
        auth = api.get("auth") or {}
        await prepare_connection({
            "kind": "tool_secret", "title": f"Connect {provider}", "provider": provider,
            "why": f"This build made {len(tools)} tool{'s' if len(tools) > 1 else ''} for {provider}. Give "
                   f"{'them' if len(tools) > 1 else 'it'} your {provider} key here; it is kept with the "
                   f"tool{'s' if len(tools) > 1 else ''}, never in this conversation.",
            "tools": [t.name for t in tools], "header": auth.get("name") or "Authorization",
            "scheme": auth.get("scheme") or "", "host_params": api.get("host_params") or []}, None, events)
    waits = list(dict.fromkeys([*by, *later]))
    if not waits:
        return [], ""
    who = " and ".join(waits)
    made = f"I made the tools for {' and '.join(by)}. " if by else ""
    return waits, (f"{made}Connect {who} on the card{'s' if len(waits) > 1 else ''} above and tell me when it's "
                   f"done, and I'll save the rest of the build. Or say \"build without {waits[0]}\", or \"stop\".")


#: Ending words at the wait for a connection (F946), read in code like the review's.
_STOP = re.compile(r"^\s*(?:stop|cancel|never ?mind|don'?t build( it)?|forget it)\s*[.!]?\s*$", re.I)
#: A reply at the wait that says it is done (F1104; P5's res-04/05: "done" after adding a server on the Apps screen
#: was asked again, in a loop). It claims nothing: only the exact check of what is here decides. A "not yet" is not
#: one.
_DONE = re.compile(r"(?i)\b(?:done|finished|all\s+set|set\s+up|added|connected|installed|ready|did\s+it|ok(?:ay)?|"
                   r"yes|yep|there)\b")
_NOT_YET = re.compile(r"(?i)\b(?:not|no|never|cannot|unable|yet)\b|n't\b|n’t\b")
#: What the wait says when it is still waiting, the same each time.
_STILL = "connect it on its card above, then tell me when it's done"


def says_done(said: str) -> bool:
    """Whether a reply at the wait says it is done: the card's own words, or the person's (F1104)."""
    from core.native_builder.build_plan import _CONNECTED

    # A question asks, never says it is done ("how do I set it up?", "is it added?"; SAI2's tightening).
    return bool(_CONNECTED.match(said)) or ("?" not in said and _DONE.search(said) is not None
                                             and _NOT_YET.search(said) is None)


def _made_tools(run: Any) -> list[str]:
    """The tools this build made and has not saved yet (its save's record)."""
    kept = run.step_progress.get(PROGRESS_KEY) or {} if run is not None else {}
    if kept.get("saved"):
        return []
    return [real for kind, real, _ in kept.get("done") or [] if kind == "tool"]


async def undo_made_tools(run: Any, events: list[dict], why: str, names: list[str] | None = None) -> list[str]:
    """Remove the tools a build made and never saved (all, or `names`), and forget them (SAI2's addition 2): never
    left behind."""
    names = [n for n in _made_tools(run) if names is None or n in names]
    if not names:
        return []
    await _undo([("tool", n, n) for n in names], run.shared_state, events, why)
    kept = run.step_progress.get(PROGRESS_KEY) or {}
    run.step_progress[PROGRESS_KEY] = {**kept, "calls_done": [],
                                       "done": [d for d in kept.get("done") or [] if not (d[0] == "tool" and d[1] in names)]}
    return names


def _scrub(value: Any, said: str) -> Any:
    """`value` with every text holding `said` replaced: a key typed at the wait leaves no copy in the run."""
    if isinstance(value, str):
        return "[a key, removed]" if said and said in value else value
    if isinstance(value, dict):
        return {k: _scrub(v, said) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, said) for v in value]
    return value


def _connected(tool: dict | None) -> bool:
    from core.http_tool import _UNCONNECTED

    return bool(tool) and not _UNCONNECTED.search(str(tool.get("base_lock") or "")) and bool(tool.get("headers"))


async def read_connect_answer(args: dict, server_module: Any, events: list[dict]) -> dict:
    """What the person said at the wait for a connection (F946), in code: the card's words with every made tool
    connected go on to the save; "build without X" removes X's tools and goes back to the check with X left out;
    "stop" removes them all; anything else is asked again."""
    from core.http_tool import _UNCONNECTED
    from core.native_builder.build_plan import _WITHOUT
    from core.store.resources import load_tools

    _, run = _run()
    state = run.shared_state if run is not None else {}
    said = "stop" if args.get("give_up") else str((_obj(state.get("connect_answer")) or {}).get("feedback") or "")
    plan = _plan_of(checked_in(state))
    made = _made_tools(run)
    # A tool of the person's own API that takes no key is connected as it is made (F1225).
    keyless = {t.name for t in plan.tools if (t.api or {}).get("keyless")}
    # An app's or an MCP server's tools, connected after the yes (F949), and every tool's provider.
    later = [t for t in plan.tools if t.status == "found" and t.via in ("catalog_app", "mcp_server")]
    providers = {t.name: t.provider or t.name for t in plan.tools if t.name in made or t in later}
    from core.secret_scrub import holds_credential

    if holds_credential(said):
        # A key typed at the wait, not on its card (SAI2's review, 6): never kept in the run, said where it goes.
        state["connect_answer"] = {"feedback": "[a key, removed]"}
        if run is not None:
            run.step_history[:] = [_scrub(entry, said) for entry in run.step_history]
        return {"outcome": "again", "say": "That looks like a key, so I didn't keep it: paste it on the card above, "
                                           "which keeps it with the tool, never in this conversation."}
    if not _WITHOUT.match(said) and not _STOP.match(said) and says_done(said):
        current = {t.get("name"): t for t in await load_tools()}
        waiting = [n for n in made if n not in keyless and not _connected(current.get(n))]
        # Each tool the approved plan names, compared exactly with what is here now (SAI2: never a partial save).
        from core import builder_guard

        exists = await builder_guard._resolver(server_module)
        absent = [t for t in later if not await exists(t.name)]
        servers = _servers_of(server_module)
        unconnected = [t for t in absent if not _provider_tools(t.provider or t.name, servers, current)]
        if not waiting and not unconnected and not absent:
            return {"outcome": "connected"}
        if waiting or unconnected:
            who = " and ".join(dict.fromkeys([*(providers.get(n, n) for n in waiting),
                                              *(t.provider or t.name for t in unconnected)]))
            return {"outcome": "again", "say": f"{who} is not connected yet: {_STILL}."}
        # Connected, but its tools are named otherwise (or only some are here): the plan is drawn again on the real
        # names, and the person says yes once more (Naveen, in SAI2-P3's tab).
        who = " and ".join(dict.fromkeys(t.provider or t.name for t in absent))
        real = sorted({n for t in absent for n in _provider_tools(t.provider or t.name, servers, current)})
        return {"outcome": "replan", "connected": list(dict.fromkeys(t.provider or t.name for t in absent)),
                "tools": real,
                "say": f"After connecting {who}, its tools are " + ", ".join(f"`{n}`" for n in real[:12])
                       + ": I'll draw the plan on them, and ask you for one more yes before I save it."}
    m = _WITHOUT.match(said)
    if m:
        # Only the provider named loses its tools (SAI2's review, 3); another's tool not yet connected goes too, as
        # the new yes makes it again; one already connected stays, and the new check finds it present with its key.
        asked = {w.strip(" `'\"").lower() for w in re.split(r",|\s+and\s+", m.group("what")) if w.strip(" `'\"")}
        named = [n for n in [*made, *(t.name for t in later)]
                 if n.lower() in asked or providers.get(n, "").lower() in asked]
        if not named:
            return {"outcome": "again", "say": "Which one? Say \"build without\" and the provider's name, or "
                                               "\"stop\"."}
        current = {t.get("name"): t for t in await load_tools()}
        gone = [n for n in made if n in named or not (n in keyless or _connected(current.get(n)))]
        removed = await undo_made_tools(run, events, "the build went on without them", gone)
        kept = [n for n in made if n not in removed]
        # The planner's own names, not the `_2` this build made under (the endpoint's tool, A7).
        draft_names = {t.name: str((t.api or {}).get("endpoint", {}).get("tool") or t.name) for t in plan.tools}
        say = " ".join(x for x in (
            f"I removed the {' and '.join(dict.fromkeys(providers.get(n, n) for n in removed))} tools I'd made."
            if removed else "",
            f"I kept the {' and '.join(dict.fromkeys(providers.get(n, n) for n in kept))} tools you connected."
            if kept else "") if x)
        return {"outcome": "without", "without": [draft_names.get(n, n) for n in named], "say": say}
    if _STOP.match(said):
        who = " and ".join(dict.fromkeys(providers.values())) or "the"
        removed = await undo_made_tools(run, events, "the build stopped")
        say = f"I removed the {who} tools I'd made." if removed else ""
        return {"outcome": "stop", "say": f"{say} Nothing else was built.".strip()}
    return {"outcome": "again", "say": f"I'm waiting: {_STILL}. Or say \"build without …\", or \"stop\"."}


def _servers_of(server_module: Any) -> dict[str, list[str]]:
    from core.native_builder.build_plan import _servers

    return _servers(server_module)


def _provider_tools(provider: str, servers: dict[str, list[str]], current: dict[str, dict]) -> list[str]:
    """The tools here that are this provider's: those of a server named for it (`notion`), or tools named for it."""
    from core.builder_research import _words

    words = set(_words(provider))
    named = [tool for server, tools in servers.items() if words and words <= set(_words(server.replace("_", " ")))
             for tool in tools]
    return named or [n for n in current if words and words <= set(_words(str(n).replace("_", " ")))]


async def _cancelled(run: Any) -> None:
    """A builder run cancelled or expired while it waits never leaves the tools it made (SAI2's addition 2)."""
    from core.native_builder.identity import is_builder_run

    if _made_tools(run) and await is_builder_run(run):
        await undo_made_tools(run, [], "the build was cancelled before it was saved")


async def _ended(run: Any) -> None:
    """A builder run that ended failed or cancelled before its save never leaves the tools it made (SAI2's review,
    4): the cancel hook sees a paused run; this sees one that was running."""
    if getattr(run, "status", "") in ("failed", "cancelled"):
        await _cancelled(run)


from core.orchestration.cancel import on_cancel, on_end  # noqa: E402 — registered with the module, as its tools are

on_cancel(_cancelled)
on_end(_ended)
register_builder_tool({"type": "function", "function": {
    "name": "read_connect_answer", "description": "Synapse AI's reading of the answer at a build's wait for a "
                                                  "connection (code).", "parameters": {"type": "object", "properties": {}}}},
                      read_connect_answer)
register_builder_tool(SUBMIT_PROMPTS, submit_prompts)
for _name, _handler, _says in (("approve_plan", approve_plan, "Synapse AI's record of the person's yes to a build "
                                                              "(code)."),
                               ("save_build", save_build, "Synapse AI's save of an approved build (code).")):
    register_builder_tool({"type": "function", "function": {
        "name": _name, "description": _says, "parameters": {"type": "object", "properties": {}}}}, _handler)

__all__ = ["MADE", "save_calls"]
