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

_log = get_logger(__name__)

#: The run-state keys the route reads.
DRAFT_KEY, CHECKED_KEY, FINDINGS_KEY, EXISTING_KEY, ANSWER_KEY = (
    "plan_draft", "plan_checked", "plan_findings", "existing_orch", "plan_answer")

#: The prefix of an id a build mints when it saves (Naveen, 10-23, Q1). It never occurs in a real id, so the
#: approval can cover each save call with the placeholders it will be made with (F940).
MADE = "@made:"

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
    return Workspace(agents=agents, workflows=flows, tools=tools, kbs=kbs, boards_on=boards_enabled(),
                     code_steps=bool(load_settings().get("code_execution_enabled", True)))


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
                   "board_claim on its workflow's own board; name them in that agent's tools, never as a Tool step."
                   if ws.boards_on else "Off in this workspace: no board tools."),
        "code_steps": ws.code_steps,
        "may_make_agents": state.get("can_create_agents") is not False,
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
    ("loop body contains", "An evaluator's branches do nothing inside a loop: route with a switch on an expression, "
                           "or have an `llm` step write a label and switch on it."),
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


def _without(plan: O.Plan, tools: list[str]) -> O.Plan:
    """The Plan with the steps that use `tools` dropped and those tools taken from its agents ("Build without X"):
    what led to a dropped step leads where it led (`apply_edits`' remove)."""
    from core.native_builder.compile import apply_edits

    gone = set(tools)
    if not gone:
        return plan
    drop = [O.Edit(op="remove_step", step_id=s.id) for s in plan.steps if s.tool in gone]
    out, _ = apply_edits(plan, drop)
    out.agents = [a.model_copy(update={"tools": [t for t in a.tools if t not in gone]}) for a in out.agents]
    for w in out.inner_workflows:
        w.steps = [s for s in w.steps if s.tool not in gone]
    return out


async def examine(raw: Any, server_module: Any, *, ws: Workspace | None = None, state: dict | None = None,
                  without: list[str] | None = None) -> Examined:
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
                                                 "questions", "not_carried_over", "acts_without_asking")}})
            plan.reuse_agents = list(dict.fromkeys([*edited.reuse_agents, *plan.reuse_agents]))
    plan = _without(plan, without or [])
    errors, _ = O.check_structure(plan, ws.inventory())
    errors += _rules(plan, ws, state)
    if errors:
        return Examined(plan, errors)
    filled = await O.fill(plan, server_module, found=_found(plan, state))
    issues = [i for i in await dry_issues(filled, server_module, ws, state) if not _not_the_planners(i)]
    return Examined(filled, [_hinted(i) for i in issues], missing=list(filled.missing), issues=issues)


def _rules(plan: O.Plan, ws: Workspace, state: dict) -> list[str]:
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
    await validate(compiled, plan, server_module, existing)
    issues = compiled.all_issues()
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

def reviewed_of(checked: dict) -> dict:
    """What a person approves of a build: the Plan as the card showed it (what F778's digest covers)."""
    return {"plan": checked.get("plan")}


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
    prior = checked_in(state)
    draft = _obj(args.get("draft"))
    draft = draft if isinstance(draft, dict) else {}
    finish = draft.get("finish")
    base: dict[str, Any] = {"object": "plan", "plan": None, "review": "", "offers": [], "question": "", "say": "",
                            "issues": [], "checks": int(prior.get("checks") or 0) + 1}
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

    answer = _obj(state.get(ANSWER_KEY)) or {}
    without = list(dict.fromkeys([*(prior.get("without") or []),
                                  *((answer.get("without") or []) if answer.get("outcome") == "without" else [])]))
    ws = await workspace(server_module)
    found = await examine(draft, server_module, ws=ws, state=state, without=without)
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
                ", and the lookup found nobody who does." if looked else "."))
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
    offers = ["build"] if not (no_way or connect or found.errors) else []
    offers += [f"without:{n}" for n in [*no_way, *connect]]
    if no_way and not looked:
        offers.append("find")
    offers += ["change", "stop"]

    names = {i: a.get("name") or i for i, a in ws.agents.items()}
    review = render(plan, agent_names=names)
    if found.errors:
        review += "\n## Can't be saved yet\n" + "\n".join(f"- {e}" for e in found.errors) + "\n"
    out = {**base, "outcome": "review", "plan": plan.model_dump(), "review": review, "offers": offers,
           "say": " ".join(said), "issues": found.errors, "offered": offered, "research": prior.get("research") or
           []}
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
#: The Connect card's own words when it is done (`onConnected`, both panels): "Connected X." / "Saved the H for T."
_CONNECTED = re.compile(r"^\s*(?:connected\b|saved the\b)", re.I)


async def read_plan_answer(args: dict, server_module: Any, events: list[dict]) -> dict:
    """What a review answer that is not F754's yes asks for: `without` (the tools to leave out, by a tool's name or
    its provider's), `connected`, or `revise` (anything else, which goes back to the planner)."""
    state = _state()
    answer = str((_obj(state.get("review_feedback")) or {}).get("feedback") or "")
    if _CONNECTED.match(answer):
        return {"outcome": "connected"}
    m = _WITHOUT.match(answer)
    if m:
        tools = (checked_in(state).get("plan") or {}).get("tools") or []
        asked = [w.strip(" `'\"").lower() for w in re.split(r",|\band\b", m.group("what")) if w.strip(" `'\"")]
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
