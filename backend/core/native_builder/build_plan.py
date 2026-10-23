"""Synapse AI's build route up to the person's yes, on the typed Plan (SAI2-P3, D300; F936, F938).

The planner writes the Plan once, and code decides every fact about it (DESIGN.md, "Principle"):

* `read_plan_context` (E1, before the planner): what the workspace has, by id — its tools (the workspace's own
  catalogue, F866), agents, workflows, knowledge bases — whether Boards are on and code steps run, and on an update
  the workflow as a Plan (`plan_from_workflow`), whose step ids the planner's `edits` name.
* `submit_plan` (the planner's E2 finish): the Plan as `examine` finds it. What the planner can fix goes back for
  E2's retry; what it cannot (a tool nothing here provides) is the lookup's, not the planner's.
* `check_build` (E1, `step_check_plan`): the outcome the route switches on — ask, said, research, redraft, review —
  with the Plan filled in by code (`objects.fill`: each tool's status, its approvals, the plan room), the card's
  text (`render`), the buttons the card offers, and the digest F778's approval covers. The Connect card for an app
  the lookup found goes up here, before the review: an app's or a server's real tool names are only known once it is
  connected (Naveen, 10-23, Q3), and "Build it" waits for it.
* `read_plan_answer` (E1, on F754's no side): a review answer that is not a yes — "build without X", the Connect
  card's "Connected …", or what to change — read in code.

A dry compile with placeholder ids (`MADE`) checks the Plan against every rule the save doors keep, so a build never
first fails at its save; `step_save` (build_save.py) compiles it again with the same placeholders.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.builder_tools import register_builder_tool
from core.log import get_logger
from core.native_builder import objects as O
from core.native_builder.change_tools import _caller, _run
from core.product_tools.approved import PLACEHOLDER

_log = get_logger(__name__)

#: The run-state keys the route reads.
DRAFT_KEY, CHECKED_KEY, FINDINGS_KEY, EXISTING_KEY, ANSWER_KEY, PROMPTS_KEY = (
    "plan_draft", "plan_checked", "plan_findings", "existing_orch", "plan_answer", "agent_prompts")

#: The prefix of an id a build mints when it saves (Naveen, 10-23, Q1). It never occurs in a real id, so the
#: approval can cover each save call with the placeholders it will be made with (F940).
MADE = PLACEHOLDER

#: Whether the build makes an HTTP tool from the lookup's API spec after the yes (P3b, F944). Until then an
#: API-only provider has no way in here.
MAKES_HTTP_TOOLS = False

#: How many times `submit_plan` sends a Plan back in one planner step before it takes it as it is, issues and all
#: (the card then says them, and offers no Build it).
SENDBACKS = 2
_SENDBACK_KEY = "_plan_sendbacks"

#: How many tools the planner's pack lists by name; the rest are counted.
PACK_TOOLS = 250


def placeholder_mint():
    """compile_plan's `mint`: each id it would mint, as the placeholder the save replaces."""
    n = {"i": 0}

    def mint(prefix: str, length: int = 7) -> str:
        n["i"] += 1
        return f"{MADE}{prefix}:{n['i']}"

    return mint


def _state() -> dict:
    _, run = _run()
    return run.shared_state if run is not None else {}


def _obj(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def checked_in(state: dict) -> dict:
    raw = _obj(state.get(CHECKED_KEY))
    return raw if isinstance(raw, dict) else {}


# ── what the workspace has ────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Workspace:
    agents: dict[str, dict] = field(default_factory=dict)
    workflows: dict[str, dict] = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    kbs: dict[str, Any] | None = None
    boards_on: bool = False
    code_steps: bool = True
    #: The workspace's shared boards, by name: a workflow's agents post to its own board, never to one of these
    #: (giving a workflow access to a shared board has no product tool yet: BOARD's lane, B2b).
    shared_boards: list[str] = field(default_factory=list)

    def inventory(self) -> O.Inventory:
        return O.Inventory(agents={i: a.get("name") or i for i, a in self.agents.items()},
                           workflows={i: w.get("name") or i for i, w in self.workflows.items()},
                           tools={t["name"] for t in self.tools})


async def workspace(server_module: Any) -> Workspace:
    """The workspace as the planner and the check read it. System definitions (Synapse AI's own) are never offered."""
    from core.boards.config import boards_enabled
    from core.builder_tools import _workspace_catalogue
    from core.config import load_settings
    from core.knowledge.config import knowledge_enabled
    from core.routes.agents import load_user_agents
    from core.store.resources import load_orchestrations

    agents = {a["id"]: a for a in await load_user_agents() if a.get("id") and not a.get("system")}
    flows = {o["id"]: o for o in await load_orchestrations()
             if o.get("id") and not o.get("system") and o.get("is_active", True) is not False}
    try:
        catalogue = await _workspace_catalogue(server_module)
    except Exception as exc:  # noqa: BLE001 — a server that cannot list is not a workspace with no tools
        _log.warning("the build's catalogue was not read: %s", exc)
        catalogue = []
    tools = []
    for t in catalogue:
        name = getattr(t, "name", None) or (t.get("function", {}).get("name") if isinstance(t, dict) else None)
        if name:
            does = getattr(t, "description", None) or (t.get("function", {}).get("description")
                                                        if isinstance(t, dict) else "")
            tools.append({"name": name, "does": " ".join(str(does or "").split())[:140]})
    kbs = None
    if knowledge_enabled():
        from core.knowledge import bases

        kbs = {kb.id: kb for kb in await bases.list_bases()}
    shared: list[str] = []
    if boards_enabled():
        from core.store.boards import list_boards

        try:
            shared = [b.name for b in await list_boards("shared")]
        except Exception as exc:  # noqa: BLE001 — no board table yet is a workspace with no shared boards
            _log.warning("the build's shared boards were not read: %s", exc)
    return Workspace(agents=agents, workflows=flows, tools=tools, kbs=kbs, boards_on=boards_enabled(),
                     code_steps=bool(load_settings().get("code_execution_enabled", True)), shared_boards=shared)


def _base(state: dict) -> O.Plan | None:
    """On an update, the workflow as a Plan: what the planner's `edits` apply to."""
    from core.native_builder.compile import plan_from_workflow

    existing = _obj(state.get(EXISTING_KEY))
    if isinstance(existing, dict) and isinstance(existing.get("orchestration"), dict):
        existing = existing["orchestration"]
    if state.get("intent") != "update" or not isinstance(existing, dict) or not existing.get("steps"):
        return None
    try:
        return plan_from_workflow(existing)
    except Exception as exc:  # noqa: BLE001 — a workflow the reader cannot take is planned as a new one
        _log.warning("the workflow to update did not read as a Plan: %s", exc)
        return None


async def read_plan_context(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.builder_help import product_capabilities

    ws = await workspace(server_module)
    state = _state()
    base = _base(state)
    tools = ws.tools[:PACK_TOOLS]
    pack: dict[str, Any] = {
        "kind": "update" if base is not None else "create",
        "tools": tools,
        **({"more_tools": len(ws.tools) - len(tools)} if len(ws.tools) > len(tools) else {}),
        "agents": [{"id": i, "name": a.get("name"), "does": " ".join(str(a.get("description") or "").split())[:140],
                    "type": "team" if a.get("type") == "delegate" else "agent"} for i, a in ws.agents.items()],
        "workflows": [{"id": i, "name": w.get("name")} for i, w in ws.workflows.items()],
        "boards": ("On: every agent step in a workflow has board_list, board_read, board_add, board_update and "
                   "board_claim on its workflow's own board; name them in that agent's tools, never as a Tool step. "
                   "It posts there, not to a shared board."
                   if ws.boards_on else "Off in this workspace: no board tools."),
        **({"shared_boards": ws.shared_boards} if ws.shared_boards else {}),
        "code_steps": ws.code_steps,
        "may_make_agents": state.get("can_create_agents") is not False,
        # What this product does and does not, so a `cannot` names the nearest thing that works (SAI2's review, nit).
        "capabilities": (await product_capabilities({}, server_module, []))["capabilities"],
    }
    if ws.kbs is not None:
        pack["knowledge_bases"] = [{"id": i, "name": kb.name} for i, kb in ws.kbs.items()
                                   if getattr(kb, "status", "active") == "active"]
    if base is not None:
        pack["workflow"] = {"id": (_obj(state.get(EXISTING_KEY)) or {}).get("id") or state.get(
            "current_orchestration_id"), "plan": base.model_dump(exclude_none=True, exclude_defaults=True)}
    return pack


# ── the Plan, examined ────────────────────────────────────────────────────────────────────────────────────────────

#: What a compile issue owes the planner, and the hint that says how (P1's findings, DESIGN.md "P1's findings").
_HINTS = (
    ("loop_over", "A loop goes through what an earlier step wrote: name that step's id, e.g. `fetch_emails`, or "
                  "`state.fetch_emails.items` for a list inside it."),
    ("if_condition", "A condition is an expression over what earlier steps wrote, by their step ids, e.g. "
                     "`state.check_weather.wind_kmh > 40`; a judgement in words is an `evaluator` step with branches."),
    ("switch_expression", "A switch's condition is an expression over what an earlier step wrote, by its step id, "
                          "e.g. `state.classify.label`; its branches are that value's cases."),
    ("body contains", "Routing does nothing inside a loop body: keep the body's steps in a line, and branch after "
                      "the loop on what it wrote."),
    ("transform_code", "A transform step needs its Python in `config.transform_code`; to reshape data without "
                       "code, use an `llm` step."),
    ("is not set up", "A Retrieve or Ingest step names its knowledge bases by an id from `knowledge_bases`."),
)


def _hinted(issue: str) -> str:
    hint = next((h for key, h in _HINTS if key in issue), "")
    return f"{issue} {hint}".strip()


def _not_the_planners(issue: str) -> bool:
    """An issue about a tool nothing here provides (F489): finding who provides it is the lookup's job."""
    return "nothing here provides" in issue


@dataclass
class Examined:
    plan: O.Plan | None
    errors: list[str]
    missing: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _dropped(steps: list[O.Step], gone: set[str]) -> list[O.Step]:
    """`steps` without the Tool steps that call one of `gone`: settled first, so a step that led to a dropped one by
    list order leads where the dropped one led, and the rest of its branch still runs (SAI2's review, 5)."""
    steps = O.settled(steps)
    dropped = {s.id: s for s in steps if s.type == "tool" and s.tool in gone}
    if not dropped:
        return steps

    def onward(target: str | None) -> str | None:
        seen = set()
        while target in dropped and target not in seen:
            seen.add(target)
            target = dropped[target].next or "end"
        return target

    out = []
    for s in steps:
        if s.id in dropped:
            continue
        update: dict[str, Any] = {}
        if s.next in dropped:
            update["next"] = onward(s.next)
        if s.branches and any(v in dropped for v in s.branches.values()):
            update["branches"] = {k: onward(v) for k, v in s.branches.items()}
        if s.body and any(b in dropped for b in s.body):
            update["body"] = [b for b in s.body if b not in dropped]
        out.append(s.model_copy(update=update) if update else s)
    return out


def _without(plan: O.Plan, tools: list[str]) -> O.Plan:
    """The Plan with the steps that call `tools` dropped, in it and its inner workflows, and those tools taken from
    its agents ("Build without X")."""
    gone = set(tools)
    if not gone:
        return plan
    out = plan.model_copy(deep=True)
    out.steps = _dropped(out.steps, gone)
    out.agents = [a.model_copy(update={"tools": [t for t in a.tools if t not in gone]}) for a in out.agents]
    for w in out.inner_workflows:
        w.steps = _dropped(w.steps, gone)
    return out


async def examine(raw: Any, server_module: Any, *, ws: Workspace | None = None, state: dict | None = None,
                  without: list[str] | None = None, alone: list[str] | None = None) -> Examined:
    """The planner's Plan held to every rule, as data: `errors` the planner can fix, `missing` the tools nothing here
    provides (the lookup's), and the Plan filled in by code."""
    from core.native_builder.compile import apply_edits

    state = _state() if state is None else state
    ws = ws or await workspace(server_module)
    raw = _obj(raw)
    if isinstance(raw, dict) and raw.get("finish") == "submit_plan":
        raw = {k: v for k, v in raw.items() if k != "finish"}
    plan, errors = O.read(raw if isinstance(raw, dict) else {})
    if plan is None:
        return Examined(None, errors)
    base = _base(state)
    if plan.kind == "update":
        if base is None:
            return Examined(None, ["kind: an update needs the workflow to change; there is none open here, so plan "
                                   "a new one (kind create)"])
        if plan.edits:
            edited, problems = apply_edits(base, plan.edits)
            if problems:
                return Examined(None, problems)
            plan = edited.model_copy(update={
                "kind": "update", "title": plan.title or edited.title, "summary": plan.summary or edited.summary,
                **{k: getattr(plan, k) for k in ("agents", "schedule", "inner_workflows", "caps", "boards",
                                                 "questions", "not_carried_over")}})
            plan.reuse_agents = list(dict.fromkeys([*edited.reuse_agents, *plan.reuse_agents]))
    plan = _without(plan, without or [])
    from core.native_builder.change_tools import _holds_a_key

    if _holds_a_key(plan.model_dump()):
        # A key pasted into a prompt, an argument or a schedule's input would be saved with the workflow (P2's rule;
        # SAI2's review, 14).
        return Examined(plan, ["The plan holds a key, a token or a password, or the mark where one was removed: "
                               "take it out. A key reaches a tool only through a Connect card."])
    errors, _ = O.check_structure(plan, ws.inventory())
    if base is not None and plan.kind == "update":
        # A step no step leads to in the workflow as it is now is not this change's to fix (SAI2's review, 23).
        before, _ = O.check_structure(base, ws.inventory())
        errors = [e for e in errors if not ("no step leads to it" in e and e in before)]
    errors += await _rules(plan, ws, state)
    if errors:
        return Examined(plan, errors)
    # Only the person lets a tool act without asking, by a press on the card (SAI2's review, 11); never the planner.
    plan.approvals = O.Approvals(acts_without_asking=list(alone or []))
    filled = await O.fill(plan, server_module, found=_found(plan, state))
    issues = [i for i in await dry_issues(filled, server_module, ws, state) if not _not_the_planners(i)]
    return Examined(filled, [_hinted(i) for i in issues], missing=list(filled.missing), issues=issues)


async def _rules(plan: O.Plan, ws: Workspace, state: dict) -> list[str]:
    """The planner's rules a structure check cannot see (P1's findings, F866b)."""
    from core.boards import TOOL_NAMES

    errors = []
    for s in [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)]:
        if s.type == "tool" and s.tool in TOOL_NAMES:
            errors.append(f"step {s.id}: board tools are an agent's, not a Tool step's: give `{s.tool}` to the "
                          "agent of an agent step")
        if s.type == "transform" and not ws.code_steps:
            errors.append(f"step {s.id}: this workspace's plan does not run code, so there are no transform steps: "
                          "use an `llm` step to reshape data")
        if s.type in ("retrieve", "ingest") and ws.kbs is None:
            errors.append(f"step {s.id}: Knowledge is off in this workspace, so there are no {s.type} steps")
    if not ws.boards_on and (used := [t for t in O.tool_names(plan) if t in TOOL_NAMES]):
        # Never looked up as a missing tool: F866b's invented "Board" provider (SAI2's review, 7).
        errors.append("Boards are off in this workspace, so no agent has " + ", ".join(f"`{t}`" for t in used)
                      + ": take the board out of the plan")
    if plan.boards:
        # A workflow's agents post to its own board; a shared board a plan made would have no access for it, as no
        # product tool gives a workflow a shared board yet (SAI2's review, 20).
        errors.append("boards: a plan makes no shared board; the workflow's agents post to its own board while "
                      "Boards are on")
    if state.get("intent") == "update" and plan.kind == "create" and _base(state) is not None:
        errors.append("kind: the person asked to change the workflow open here: write kind update, with edits by its "
                      "step ids")
    if plan.kind == "update" and plan.schedule and (open_id := _orch_id(plan, state)):
        from core.store import schedules

        if any(s.get("target_id") == open_id for s in await schedules.load()):
            # Changing when it runs is a change of its own (the change route's ops), not a second schedule.
            errors.append("schedule: this workflow already runs on a schedule; leave schedule out: moving it is a "
                          "change of its own")
    if plan.agents and state.get("can_create_agents") is False:
        errors.append("agents: this conversation uses existing agents only; give each step one of theirs by id")
    return errors


def _found(plan: O.Plan, state: dict) -> dict[str, O.ToolStatus]:
    """What the lookup found for each tool the Plan names, by the provider's name in the tool's own name or in the
    words of the steps and agents that use it (F763's match, on the Plan's own fields)."""
    from core.builder_research import _words

    findings = _obj(state.get(FINDINGS_KEY))
    options = (findings or {}).get("options") if isinstance(findings, dict) else None
    if not options:
        return {}
    found: dict[str, O.ToolStatus] = {}
    for name in O.tool_names(plan):
        said = " ".join([name, *(f"{s.name} {s.does}" for s in plan.steps if s.tool == name),
                         *(f"{a.name} {a.purpose}" for a in plan.agents if name in a.tools)])
        words = set(_words(said.replace("_", " ")))
        for option in options:
            provider, how = str(option.get("provider") or ""), option.get("how")
            if provider and how in ("mcp", "api") and set(_words(provider)) <= words:
                via = "mcp_server" if how == "mcp" else "http_tool"
                found[name] = O.ToolStatus(name=name, status="found", provider=provider, via=via)
                break
    return found


async def dry_issues(plan: O.Plan, server_module: Any, ws: Workspace, state: dict) -> list[str]:
    """Every issue a save door would find, before anything is saved: the compiler's own, the workspace's
    (`compile.validate`: F489, E1's arguments, BWA1's decision 4), the engine's save rule (F850/F853), F691's
    cross-workflow loops, knowledge bases by id, and the transform gate."""
    from core.builder_tools import _knowledge_step_issues, _refuse_ungated_transform, _workflows_called
    from core.native_builder.compile import compile_plan, validate
    from core.orchestration import save_rules

    existing = {i: a for i, a in ws.agents.items()}
    compiled = compile_plan(plan, existing_agents=existing, mint=placeholder_mint(), orch_id=_orch_id(plan, state))
    await validate(compiled, plan, server_module, existing, own_steps=_changed_steps(plan, state, compiled))
    issues = compiled.all_issues() + inner_order(compiled)[1]
    agents = existing | {a["id"]: a for a in compiled.agents}
    for doc in [compiled.orchestration, *(i.orchestration for i in compiled.inner)]:
        if (refused := _refuse_ungated_transform(doc["steps"])) is not None:
            issues.append(refused["error"])
        if (said := save_rules.refusal(doc, agents)) is not None:
            issues.append(said)
        if ws.kbs is not None:
            for s in doc["steps"]:
                if s.get("type") in ("retrieve", "ingest"):
                    issues += _knowledge_step_issues(s, ws.kbs, False)
        reachable = {**doc, "steps": [s for s in doc["steps"] if not str(s.get("workflow_id") or "").startswith(MADE)]}
        issues += (await _workflows_called(reachable, agents))[1]
    return list(dict.fromkeys(issues))


#: What a changed step's line names, by the Plan field that changed.
_FIELD_WORDS = {"type": "its type", "name": "its name", "does": "what it does", "agent": "its agent",
                "tool": "its tool", "prompt": "its prompt", "next": "what follows it", "branches": "its branches",
                "condition": "its condition", "loop_over": "what it loops over", "body": "its body",
                "workflow": "the workflow it runs", "config": "its settings"}


def changes_of(plan: O.Plan, state: dict) -> list[str]:
    """An update's changes against the workflow as it is, in code's words: what the person approves, step by step
    (SAI2's review, 13). Empty on a create."""
    base = _base(state) if plan.kind == "update" else None
    if base is None:
        return []
    before = {s.id: s for s in O.settled(base.steps)}
    after = {s.id: s for s in O.settled(plan.steps)}
    lines = []
    for sid, s in after.items():
        if sid not in before:
            lines.append(f'Add the step "{s.name}" ({s.type})' + (f": {s.does.strip()}" if s.does.strip() else ""))
            continue
        old = before[sid].model_dump()
        moved = [w for f, w in _FIELD_WORDS.items() if f != "does" and old.get(f) != s.model_dump().get(f)]
        if moved:
            lines.append(f'Change the step "{before[sid].name}": ' + ", ".join(moved))
    lines += [f'Remove the step "{s.name}"' for sid, s in before.items() if sid not in after and s.type != "end"]
    if plan.title != base.title:
        lines.append(f'Rename the workflow "{plan.title}"')
    if (plan.settings or {}) != (base.settings or {}):
        lines.append("Change the workflow's settings: " + ", ".join(sorted(set(plan.settings or {}) ^ set(
            base.settings or {}) | {k for k in (plan.settings or {}) if (plan.settings or {}).get(k) != (
                base.settings or {}).get(k)})))
    return lines


def _unasked(doc: dict) -> set[str]:
    """The steps a run of `doc` reaches without passing a Human step (`builder_guard.unapproved`'s reading)."""
    from core.builder_tools import step_successors

    by_id = {s.get("id"): s for s in doc.get("steps") or []}
    seen: set[str] = set()
    todo = [doc.get("entry_step_id")]
    while todo:
        cur = todo.pop()
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        if by_id[cur].get("type") != "human":
            todo += step_successors(by_id[cur], by_id)
    return seen


_ACTION = ("type", "forced_tool", "tool_arguments", "agent_id", "prompt_template")


def _changed_steps(plan: O.Plan, state: dict, compiled: Any) -> set[str] | None:
    """On an update, the steps that are the build's own for BWA1's decision 4: new ones, ones whose action changed,
    and ones the edits left with no person before them where there was one. What runs today as it did is warned
    of, never refused (SAI2's review, 1). None on a create: every step is its own."""
    from core.native_builder.compile import compile_plan

    base = _base(state) if plan.kind == "update" else None
    if base is None:
        return None
    before_doc = compile_plan(base, mint=placeholder_mint(), orch_id=_orch_id(plan, state)).orchestration
    after_doc = compiled.orchestration
    before = {s["id"]: {k: s.get(k) for k in _ACTION} for s in before_doc["steps"]}
    own = {s["id"] for s in after_doc["steps"] if before.get(s["id"]) != {k: s.get(k) for k in _ACTION}}
    return own | (_unasked(after_doc) - _unasked(before_doc))


def inner_order(compiled: Any) -> tuple[list[Any], list[str]]:
    """A build's inner workflows in the order they can be saved — each before any workflow of the build that runs it
    — and an issue for each loop among them (F691's class, for workflows that do not exist yet: SAI2's review, 2)."""
    docs = {c.orchestration["id"]: c for c in compiled.inner}
    calls = {i: {s.get("workflow_id") for s in c.orchestration["steps"] if s.get("workflow_id") in docs}
             for i, c in docs.items()}
    ordered: list[Any] = []
    issues: list[str] = []
    state: dict[str, str] = {}

    def visit(i: str, chain: list[str]) -> None:
        if state.get(i) == "done":
            return
        if state.get(i) == "on":
            names = [docs[x].orchestration["name"] for x in chain[chain.index(i):]] + [docs[i].orchestration["name"]]
            issues.append("These inner workflows run each other in a loop, so every run would call itself again "
                          "without end: " + " → ".join(f"'{n}'" for n in names) + ". Break the loop.")
            return
        state[i] = "on"
        for j in sorted(calls[i]):
            visit(j, [*chain, i])
        state[i] = "done"
        ordered.append(docs[i])

    for i in docs:
        visit(i, [])
    return ordered, list(dict.fromkeys(issues))


def _orch_id(plan: O.Plan, state: dict) -> str | None:
    """An update saves over its workflow's own id; a new workflow gets a placeholder."""
    if plan.kind != "update":
        return None
    existing = _obj(state.get(EXISTING_KEY)) or {}
    return existing.get("id") or state.get("current_orchestration_id") or None


# ── the planner's finish ──────────────────────────────────────────────────────────────────────────────────────────

SUBMIT = {"type": "function", "function": {
    "name": "submit_plan",
    "description": ("Finish with the plan: the workflow to build (kind create) or the changes to the open one (kind "
                    "update, as `edits` by its step ids). Anything to fix comes back to you."),
    "parameters": O.plan_schema(),
}}


async def submit_plan(args: dict, server_module: Any, events: list[dict]) -> dict:
    found = await examine(args, server_module)
    if found.errors:
        _, run = _run()
        sent = int((run.shared_state.get(_SENDBACK_KEY) or 0) if run is not None else 0)
        if found.plan is None or sent < SENDBACKS:
            if run is not None:
                run.shared_state[_SENDBACK_KEY] = sent + 1
            return {"error": "Fix these, then submit the plan again:\n" + "\n".join(f"- {e}" for e in found.errors)}
    _, run = _run()
    if run is not None:
        run.shared_state.pop(_SENDBACK_KEY, None)
    draft = O.PlanDraft.model_validate(_obj(args) or {})
    return {"finish": "submit_plan", **draft.model_dump(exclude_none=True)}


register_builder_tool(SUBMIT, submit_plan)
register_builder_tool({"type": "function", "function": {
    "name": "read_plan_context",
    "description": "Synapse AI's build pack (code): what this workspace has, by id, for the planner.",
    "parameters": {"type": "object", "properties": {}},
}}, read_plan_context)


# ── the check the route switches on ───────────────────────────────────────────────────────────────────────────────

#: The review's lead-in (`step_human_review`), F754's words when the plan can be built; the panel knows a plan by its
#: first words, "Here is the proposed plan" (`plan.ts`'s BUILD_LEAD).
LEAD_BUILD = ("Here is the proposed plan. Reply **approve** (or 'ok' / 'looks good' / 'yes') to build it as-is, or "
              "describe the changes you want and it will be redrafted.")
LEAD_NOT_YET = ("Here is the proposed plan. It can't be built as it is yet: what it needs is below. Tell me what to "
                "change, or build it without what is missing.")


def reviewed_of(checked: dict) -> dict:
    """What a person approves of a build: the Plan as the card showed it, and the new agents' instructions it showed
    with it (what F778's digest covers; Naveen 10-23, Q2 as revised: the yes covers exactly what is saved)."""
    return {"plan": checked.get("plan"), "prompts": checked.get("prompts") or {}}


def agent_fingerprint(agent: dict) -> str:
    """What a new agent's instructions were written for: its name, kind, purpose, tools and members. A plan revised
    so that one of these changes gets the agent's instructions written again."""
    from core.product_tools.approved import digest

    return digest({k: agent.get(k) for k in ("name", "type", "purpose", "tools", "members")})


def prompts_for(plan: O.Plan, state: dict) -> dict[str, str] | None:
    """The prompt writer's instructions for each of `plan`'s new agents, when it wrote them for these very agents;
    None when any is missing or was written for an agent the plan has since changed."""
    raw = _obj(state.get(PROMPTS_KEY))
    if not isinstance(raw, dict):
        return None
    prompts, written_for = raw.get("prompts") or {}, raw.get("for") or {}
    out = {}
    for agent in plan.agents:
        text = prompts.get(agent.name)
        if not text or written_for.get(agent.name) != agent_fingerprint(agent.model_dump()):
            return None
        out[agent.name] = text
    return out


def _app_for(provider: str, catalog: list[dict]) -> dict | None:
    from core.builder_research import _words

    words = set(_words(provider))
    return next((e for e in catalog if words and words <= set(_words(f"{e.get('name')} {e.get('slug')}"))), None)


async def check_build(args: dict, server_module: Any, events: list[dict]) -> dict:
    """See the module's docstring. `draft` is the planner's finish (E2's object); everything else is the run's."""
    from core.builder_connect import _read_options, prepare_connection
    from core.native_builder.render import render
    from core.product_tools.approved import digest

    state = _state()
    state.pop(_SENDBACK_KEY, None)  # each pass of the planner gets its own sendbacks (SAI2's review, nit)
    prior = checked_in(state)
    draft = _obj(args.get("draft"))
    draft = draft if isinstance(draft, dict) else {}
    finish = draft.get("finish")
    # What earlier passes found is carried through a question round, so the lookup and the Connect cards are not
    # repeated, and every question with its answer reaches the planner, not only the last (SAI2's review, 22).
    asked = list(prior.get("asked") or [])
    if prior.get("outcome") == "ask" and (answered := (_obj(state.get("human_response")) or {}).get("answer")):
        asked.append({"question": prior.get("question") or "", "answer": str(answered)})
    base: dict[str, Any] = {"object": "plan", "plan": None, "review": "", "offers": [], "question": "", "say": "",
                            "issues": [], "checks": int(prior.get("checks") or 0) + 1, "asked": asked,
                            "research": prior.get("research") or [], "offered": prior.get("offered") or []}
    if finish == "ask_person":
        options = draft.get("options") or []
        question = (draft.get("question") or "") + ("\n" + "\n".join(f"{n}. {o}" for n, o in enumerate(options, 1))
                                                    if options else "")
        return {**base, "outcome": "ask", "question": question}
    if finish == "cannot":
        return {**base, "outcome": "said", "say": f"{str(draft.get('why', '')).rstrip('.')}. "
                                                  f"{draft.get('nearest', '')}".strip()}
    if finish != "submit_plan":
        return {**base, "outcome": "said", "say": "I could not work out what to build. Say it again in other words."}
    if state.get("intent") == "update" and _base(state) is None:
        return {**base, "outcome": "said", "say": "I could not read the workflow to change, so I changed nothing. "
                                                  "Open it and ask again."}

    answer = _obj(state.get(ANSWER_KEY)) or {}
    # "Build without X" is an answer to the plan the card showed: it holds while that draft does, and a new draft
    # from the planner starts with none (SAI2's review, 6: a re-plan after "Connected" dropped X again).
    from core.product_tools.approved import digest

    drafted = digest({k: v for k, v in draft.items() if k != "finish"})
    same = prior.get("draft") == drafted
    without = list(dict.fromkeys([*((prior.get("without") or []) if same else []),
                                  *((answer.get("without") or []) if same and answer.get("outcome") == "without"
                                    else [])]))
    base["draft"] = drafted
    # The person's own "let X act without asking": kept while that tool is in the plan, whatever the planner redraws.
    alone = list(dict.fromkeys([*(prior.get("alone") or []),
                                *((answer.get("alone") or []) if answer.get("outcome") == "alone" else [])]))
    base["alone"] = alone
    ws = await workspace(server_module)
    found = await examine(draft, server_module, ws=ws, state=state, without=without, alone=alone)
    plan = found.plan
    if plan is None:
        return {**base, "outcome": "said", "say": "I could not make a plan that holds together: "
                                                  + "; ".join(found.errors[:3])}
    base["without"] = without
    if not [s for s in plan.steps if s.type != "end"]:
        left = ", ".join(f"`{t}`" for t in without)
        return {**base, "outcome": "said", "say": f"Without {left} there is nothing left to build." if without
                else "That plan has no steps to build."}
    if plan.room:
        return {**base, "outcome": "said", "say": plan.room}
    # A shared board the plan names is not made again, and nothing here posts to it: say so plainly, on the card and
    # after the save, so no one reads the build as posting there (SAI2's review, F866's second half).
    shared = {b.lower(): b for b in ws.shared_boards}
    named = [b for b in plan.boards if b.name.lower() in shared]
    plan.boards = [b for b in plan.boards if b.name.lower() not in shared]
    text = " ".join([plan.title, plan.summary, *(f"{s.name} {s.does} {s.prompt or ''}" for s in plan.steps)]).lower()
    named_boards = list(dict.fromkeys([*(shared[b.name.lower()] for b in named),
                                       *(name for low, name in shared.items() if low in text)]))
    board_notes = [f'Its agents post to this workflow\'s own board, not to "{name}": letting a workflow post to a '
                   "shared board is set on that board's access on the Boards screen, which I can't do yet."
                   for name in named_boards]
    findings = _obj(state.get(FINDINGS_KEY))
    looked = isinstance(findings, dict) and bool(findings.get("options"))
    if plan.missing and not looked and not prior.get("research"):
        return {**base, "outcome": "research", "research": list(plan.missing), "plan": plan.model_dump()}

    catalog = (await _read_options()).get("catalog") or []
    offered = list(prior.get("offered") or [])
    no_way, connect, said = [], [], []
    for t in plan.tools:
        if t.status == "missing":
            no_way.append(t.name)
            said.append(f"`{t.name}`: nothing in this workspace provides it" + (
                ", and the lookup found nobody who does" if looked else "")
                + ". Connect an app or service that provides it, or build without it.")
        elif t.status == "found" and t.via == "http_tool" and not MAKES_HTTP_TOOLS:
            no_way.append(t.name)
            said.append(f"`{t.name}`: {t.provider} has a public API, but I can't make a tool for it here yet: build "
                        "without it, or connect a server that provides it.")
        elif t.status == "found":
            connect.append(t.name)
            app = _app_for(t.provider or "", catalog)
            if app is not None:
                t.via = "catalog_app"
                slug = str(app.get("slug"))
                if slug not in offered:
                    shown = await prepare_connection({"kind": "app", "catalog_slug": slug,
                                                      "title": f"Connect {app.get('name')}",
                                                      "why": f"This plan's `{t.name}` needs it."}, None, events)
                    if shown.get("status") == "card_shown":
                        offered.append(slug)
                said.append(f"`{t.name}`: {app.get('name')} is in the Apps catalog. Connect it with the card above; "
                            "I'll use its tools once it is connected.")
            else:
                said.append(f"`{t.name}`: {t.provider} has its own MCP server. Add it under Apps and MCP servers, "
                            "then tell me it is connected.")
    if found.errors:
        said.append("It can't be saved as it is: " + " ".join(found.errors[:3]))
    buildable = not (no_way or connect or found.errors)
    prompts = prompts_for(plan, state) if plan.agents else {}
    if buildable and prompts is None:
        # The new agents' instructions are written before the review, so the person reads them and the yes covers
        # them (Naveen, 10-23, Q2 as revised). Not for a plan that cannot be built yet: no call is spent on it.
        return {**base, "outcome": "prompts", "plan": plan.model_dump(), "without": without, "offered": offered,
                "research": prior.get("research") or [],
                "for": {a.name: agent_fingerprint(a.model_dump()) for a in plan.agents}}
    offers = ["build"] if buildable else []
    offers += [f"without:{n}" for n in [*no_way, *connect]]
    offers += [f"alone:{n}" for n in plan.approvals.asks_first if n not in no_way and n not in connect]
    if no_way and not looked:
        offers.append("find")
    offers += ["change", "stop"]

    names = {i: a.get("name") or i for i, a in ws.agents.items()}
    review = render(plan, agent_names=names)
    changes = changes_of(plan, state)
    if changes:
        review = review.replace("## Steps\n", "## What changes\n" + "\n".join(f"- {c}" for c in changes)
                                + "\n\n## Steps as they will be\n", 1)
    if prompts:
        review += "\n## New agents' instructions\n" + "\n\n".join(
            f"### {name}\n{text.strip()}" for name, text in prompts.items()) + "\n"
    if found.errors:
        review += "\n## Can't be saved yet\n" + "\n".join(f"- {e}" for e in found.errors) + "\n"
    if board_notes:
        review += "\n## Boards\n" + "\n".join(f"- {n}" for n in board_notes) + "\n"
    out = {**base, "outcome": "review", "plan": plan.model_dump(), "prompts": prompts or {}, "review": review,
           "offers": offers, "say": " ".join([*said, *board_notes]), "issues": found.errors, "offered": offered,
           "research": prior.get("research") or [], "notes": board_notes, "changes": changes,
           # The review's lead-in follows what the card offers: no "approve to build it" where it can't be built.
           "lead": LEAD_BUILD if buildable else LEAD_NOT_YET}
    out["digest"] = digest(reviewed_of(out))
    await _card(out)
    return out


async def _card(checked: dict) -> None:
    """Keep this build on Synapse AI's board, as its person's card (F776), waiting for them; a failure to write it
    never stops the build itself."""
    from core.boards import assistant

    run_id, _ = _run()
    if not run_id:
        return
    plan = checked.get("plan") or {}
    stage = "needs_connection" if any(o.startswith("without:") for o in checked.get("offers") or []) else "waiting"
    try:
        await assistant.put(run_id, title=(plan.get("title") or "A build")[:200], stage=stage,
                            owner_id=_caller().user_id, body=checked.get("review") or "",
                            data={"plan": {k: v for k, v in checked.items() if k != "review"}}, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — the board is the record, not the build
        _log.warning("build card for run %s not written: %s", run_id, exc)


# ── a review answer that is not a yes ─────────────────────────────────────────────────────────────────────────────

_WITHOUT = re.compile(r"^\s*build\s+(?:it\s+)?without\s+(?P<what>.+?)\s*[.!]?\s*$", re.I)
#: The card's "Let it act without asking" (`plan.ts` `aloneWords`): the person's own act, never the planner's.
_ALONE = re.compile(r"^\s*let\s+(?P<what>.+?)\s+act\s+without\s+asking\s*[.!]?\s*$", re.I)
#: The Connect card's own words when it is done (`onConnected`, both panels): "Connected X.", "Saved the H for T.",
#: and an app's own sign-in, "Signed in to X." (SAI2's review, 8).
_CONNECTED = re.compile(r"^\s*(?:connected\b|saved the\b|signed in to\b)", re.I)


async def read_plan_answer(args: dict, server_module: Any, events: list[dict]) -> dict:
    """What a review answer that is not F754's yes asks for: `without` (the tools to leave out, by a tool's name or
    its provider's), `connected`, or `revise` (anything else, which goes back to the planner)."""
    state = _state()
    answer = str((_obj(state.get("review_feedback")) or {}).get("feedback") or "")
    if _CONNECTED.match(answer):
        return {"outcome": "connected"}
    let = _ALONE.match(answer)
    if let:
        named = let.group("what").strip(" `'\"").lower()
        asks = ((checked_in(state).get("plan") or {}).get("approvals") or {}).get("asks_first") or []
        picked = [t for t in asks if t.lower() == named]
        if picked:
            return {"outcome": "alone", "alone": picked}
    m = _WITHOUT.match(answer)
    if m:
        tools = (checked_in(state).get("plan") or {}).get("tools") or []
        asked = [w.strip(" `'\"").lower() for w in re.split(r",|\s+and\s+", m.group("what")) if w.strip(" `'\"")]
        picked = [t["name"] for t in tools if t.get("status") != "present"
                  and (t["name"].lower() in asked or str(t.get("provider") or "").lower() in asked)]
        if picked:
            return {"outcome": "without", "without": picked}
    return {"outcome": "revise"}


register_builder_tool({"type": "function", "function": {
    "name": "check_build",
    "description": "Synapse AI's build check (code): the Plan's facts, its card, what the card offers.",
    "parameters": {"type": "object", "properties": {"draft": {"type": "object"}}, "required": ["draft"]},
}}, check_build)
register_builder_tool({"type": "function", "function": {
    "name": "read_plan_answer",
    "description": "Synapse AI's reading of a plan review's answer that is not a yes (code).",
    "parameters": {"type": "object", "properties": {}},
}}, read_plan_answer)
