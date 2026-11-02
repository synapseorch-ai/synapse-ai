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
import unicodedata
import re
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any

from core.builder_tools import register_builder_tool
from core.log import get_logger
from core import feature_census
from core.native_builder import boards_rule, imitation, objects as O, one_writer, own_api, periods
from core.native_builder.change_tools import _caller, _run
from core.product_tools.approved import PLACEHOLDER

_log = get_logger(__name__)

#: The run-state keys the route reads.
DRAFT_KEY, CHECKED_KEY, FINDINGS_KEY, EXISTING_KEY, ANSWER_KEY, PROMPTS_KEY = (
    "plan_draft", "plan_checked", "plan_findings", "existing_orch", "plan_answer", "agent_prompts")
#: The card a revision is held against: the plan last shown, its draft's digest and its run (F1241, F1245).
PLAN_BEFORE_KEY = "plan_before"

#: The prefix of an id a build mints when it saves (Naveen, 10-23, Q1). It never occurs in a real id, so the
#: approval can cover each save call with the placeholders it will be made with (F940).
MADE = PLACEHOLDER

#: Whether the build makes an HTTP tool from the lookup's API spec after the yes (P3b: F944 makes it, F945 connects
#: it, F946 waits for the connection before the save). Off, an API-only provider has no way in here.
MAKES_HTTP_TOOLS = True

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


async def _menu_lines() -> list[str]:
    """The models a new agent can be on, one short line each: what it can do and what it costs (F966)."""
    from core.native_builder import models_menu as MM

    lines = []
    for e in await MM.menu():
        can = ["calls tools" if e.tools != "none" else "calls no tools"]
        can += ["reasons"] if e.reasoning else []
        can += ["reads images"] if e.vision else []
        can += [f"${e.price_out:g} per 1M output tokens"] if e.price_out is not None else []
        can += [] if e.can_pay else ["can't be paid for now"]
        lines.append(f"{e.name}: {', '.join(can)}")
    return lines[:24]


def narrow(a: O.NewAgent, steps: int) -> bool:
    """A small agent with a narrow goal and little work (Naveen, 10-30): the one job the cheapest model that fits gets
    (F1251). One step at most, a simple need, two tools at most, and none of the editor's switches."""
    return (steps <= 1 and a.type == "agent" and a.needs == "simple" and len(a.tools) <= 2 and not a.long_inputs
            and not (a.memory or a.knowledge or a.browser or a.servers))


async def pick_models(plan: O.Plan, stronger: list[str]) -> tuple[dict[str, O.ModelPick], list[str], str]:
    """Each new agent's model, picked in code from the models this workspace can call (F966): from what it needs, or
    from the person's own wish, never from an id the planner wrote. Returns the picks; what no model here can do (said
    on the card, and Build it stays off); and a question when a wish could mean more than one model. `stronger`: the
    agents the person moved to the stronger model the card offered. No menu read: no picks, the workspace's default,
    as before."""
    from core.native_builder import models_menu as MM

    if not plan.agents:
        return {}, [], ""
    menu = await MM.menu()
    if not menu:
        return {}, [], ""
    names = {e.id: e.name for e in menu}
    picks: dict[str, O.ModelPick] = {}
    issues: list[str] = []
    default = MM.workspace_model()
    uses = [s.agent for s in [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)] if s.agent]
    for a in plan.agents:
        # A team's lead hands work to its members through a tool call.
        has_tools = bool(a.tools) or a.type == "team"
        wish = (a.model_wish or "").strip()
        got = (MM.resolve_wish(wish, a.needs, has_tools=has_tools, menu=menu) if wish
               else MM.pick(a.needs, has_tools=has_tools, menu=menu, narrow=narrow(a, uses.count(a.name)),
                            long_inputs=a.long_inputs, default=default))
        if got is None:
            continue
        if isinstance(got, MM.Ask):
            return picks, issues, f"For {a.name}: {got.question}"
        if isinstance(got, MM.NoModel):
            issues.append(f"{a.name}: {got.issue}")
            continue
        up = MM.stronger_than(got.id, a.needs, has_tools=has_tools, menu=menu)
        if up and a.name in stronger:
            picks[a.name] = O.ModelPick(model=up.id, name=names.get(up.id, up.id),
                                        reason=f"the stronger model you chose, {up.reason}")
        else:
            picks[a.name] = O.ModelPick(model=got.id, name=names.get(got.id, got.id), reason=got.reason,
                                        **({"stronger": up.id, "stronger_price": up.reason}
                                           if up and a.stronger_helps else {}))
    return picks, issues, ""


def go_ahead_request(state: dict) -> str | None:
    """What a bare go-ahead right after findings asks for (F1064, from P5's res-02 measure): the findings' goal, in
    code's words. "build it" alone has no request of its own, and a planner given only those words asked what to
    build and went on to plan whatever was said next. A go-ahead that says more is a request of its own, and with no
    findings there is no goal to build: both are None, and the person's words stand alone as before.

    Only the previous turn's findings count (`prior_findings`, as F766's override reads them: the newest run's own card,
    so an unrelated turn in between leaves none), never findings this build's own look-up wrote (`plan_findings`
    mid-run): an old goal is never revived by a later "yes" (SAI2's check)."""
    from core.native_builder.read_turn import _GO_ON, _plain
    from core.native_builder.review import is_yes

    said = str(state.get("user_message") or "")
    if not (is_yes(said) or _plain(said) in _GO_ON):
        return None
    found = _obj(state.get("prior_findings"))
    goal = " ".join(str(found.get("goal") or "").split()) if isinstance(found, dict) else ""
    if not goal:
        return None
    return (f"Build this: {goal.rstrip('.')}. Use what the findings say about each provider; a tool no option provides "
            f"is marked missing. (They said only \"{said.strip()}\" after the findings.)")


#: What the planner sees of a read site (D340 B): the facts, not the fetch's bookkeeping.
_SITE_FIELDS = ("url", "final_url", "title", "what", "kind", "api_spec", "why_not")


async def _named_sites(state: dict) -> list[dict]:
    """The sites the person's message names, as read (`named_sites.read_named`), each kept in the run's state so this
    conversation never reads it again (D340 D2). A read that fails is a fact in the list, and one that raises is none:
    never a pack that fails."""
    from core.native_builder import named_sites

    kept = state.get(named_sites.PAGES_KEY)
    already = kept if isinstance(kept, dict) else {}
    try:
        pages = await named_sites.read_named(str(state.get("user_message") or ""),
                                             already=named_sites.settled(already))
    except Exception as exc:  # noqa: BLE001 — sites unread are a plan without them, as before D340
        _log.warning("the sites the message names were not read: %s", exc)
        return []
    if pages:
        state[named_sites.PAGES_KEY] = {**already, **pages}
    return [{k: page[k] for k in _SITE_FIELDS if page.get(k)} for page in pages.values() if isinstance(page, dict)]


async def read_plan_context(args: dict, server_module: Any, events: list[dict]) -> dict:
    from core.builder_help import product_capabilities
    from core.native_builder import features

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
                   "It posts there, not to a shared board. When the person asks for a board, put one in the plan's "
                   "boards, named as they said (or for what it holds): it is this workflow's own board under that "
                   "name, where its cards go (F1214). " + boards_rule.GUIDANCE
                   if ws.boards_on else
                   "Off in this workspace. Where the request needs a board, still give that agent its board tools as "
                   "if on, and its board in boards: the review says Boards are off and offers to turn them on "
                   "(F1203). " + boards_rule.GUIDANCE),
        **({"shared_boards": ws.shared_boards} if ws.shared_boards else {}),
        "code_steps": ws.code_steps,
        # What is done on a screen, not from here (F1256): asked for, it is said with the screen, never imitated.
        "not_here": feature_census.not_here(),
        # Each feature the person's message names, looked up before any plan (F1257): what is done there from here or
        # not, what imitating it looks like, and the help and docs on it. None named, no key.
        **({"features": named} if (named := features.features(str(state.get("user_message") or ""))) else {}),
        # What a new agent's switches may name (F1223): connected MCP servers, and the sites with a kept session.
        "servers": sorted(_servers(server_module)),
        # A schedule's times are on this clock unless the person names another; never ask them for it (F948).
        "person_timezone": state.get("person_timezone") or "UTC",
        "may_make_agents": state.get("can_create_agents") is not False,
        # What this product does and does not, so a `cannot` names the nearest thing that works (SAI2's review, nit).
        "capabilities": (await product_capabilities({}, server_module, []))["capabilities"],
    }
    try:
        from core import site_sessions
        from core.builder_tools import _acting_caller

        if sites := sorted({r["site"] for r in await site_sessions.listed(_acting_caller()) if r.get("usable")}):
            pack["signed_in_sites"] = sites
    except Exception as exc:  # noqa: BLE001 — no kept sessions read is a pack without them
        _log.warning("the build's kept sessions were not read: %s", exc)
    # The sites the message names, read through the guarded read once a conversation (D340 B, D2): typed facts, never
    # the page. Always present, empty when it names none.
    pack["named_sites"] = await _named_sites(state)
    # A bare "build it" after findings: their goal is the request (F1064); the person's words stay theirs. Always
    # present, empty when there is none, so the planner's template never shows an unfilled reference.
    pack["request"] = go_ahead_request(state) or ""
    if pack["may_make_agents"] and (models := await _menu_lines()):
        # What a new agent's model can be, so the planner says what each needs, or the person's wish (F966).
        pack["models"] = models
    if not evals_reachable():
        # No Evals tools on this host (D3ASM): the planner plans no eval part, which the card would only take out.
        pack["evals"] = "Not available here: plan no `eval`; the card points to the Evals screen."
    if ws.kbs is not None:
        pack["knowledge_bases"] = [{"id": i, "name": kb.name} for i, kb in ws.kbs.items()
                                   if getattr(kb, "status", "active") == "active"]
        pack["new_knowledge"] = ("When the request needs a knowledge base that is not here, put it in the plan's "
                                 "knowledge, with the Vault files the person named; name it, by its name, in its "
                                 "agents' knowledge and its retrieve and ingest steps. It is made with the build "
                                 "(F1224).")
    else:
        pack["knowledge"] = ("Off in this workspace. Where the request needs to search or fill a knowledge base, still "
                             "plan its retrieve or ingest step: the review says Knowledge is off and offers to turn it "
                             "on, and its base is picked once it is (F1203).")
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
    #: Tools a policy blocks where the plan uses them, with why: the person's to change, never the planner's (F964).
    blocks: dict[str, str] = field(default_factory=dict)
    #: What code changed in the plan and the card says (F944: a tool name already taken, made as `_2`).
    notes: list[str] = field(default_factory=list)
    #: Names nothing provides that code can say what they are instead (F950, F1043): never researched.
    misnamed: list[str] = field(default_factory=list)
    #: Features the plan needs that are off (`features_needed`, F1203): blocks, with a switch on the card.
    features: list[str] = field(default_factory=list)
    #: What its structure should be instead, where code can tell (D340 D3, `structure.issues`): sent back once, then
    #: said on the card; never a block.
    structure: list[str] = field(default_factory=list)


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
    # A question the planner wrote anyway (F1253): held to F1093 as before, then asked in the form, never shown open.
    plan.questions = [q.strip() for q in (raw.get("questions") if isinstance(raw, dict) else None) or []
                      if isinstance(q, str) and q.strip()]
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
                **{k: getattr(plan, k) for k in ("agents", "schedule", "inner_workflows", "caps", "boards", "knowledge",
                                                 "apis", "mcp_servers", "eval", "questions", "not_carried_over")}})
            plan.reuse_agents = list(dict.fromkeys([*edited.reuse_agents, *plan.reuse_agents]))
    plan = _without(plan, without or [])
    from core.native_builder.change_tools import _holds_a_key
    from core.secret_scrub import holds_credential

    # What the plan writes, never what the workflow already held (SAI2's re-check: an update of a workflow holding a
    # key-like string must still be possible); `not_carried_over` may name where a pasted export's key was.
    written = _written(plan, base)
    if _holds_a_key({k: v for k, v in written.items() if k != "not_carried_over"}) or holds_credential(
            json.dumps(written.get("not_carried_over") or [], ensure_ascii=False)):
        # A key pasted into a prompt, an argument or a schedule's input would be saved with the workflow (P2's rule;
        # SAI2's review, 14).
        return Examined(plan, ["The plan holds a key, a token or a password, or the mark where one was removed: "
                               "take it out. A key reaches a tool only through a Connect card."])
    if MADE in json.dumps(plan.model_dump(), default=str):
        # The save's own mark for an id it has yet to make: in what is saved, it would leave every call uncovered.
        return Examined(plan, [f"The plan holds the text {MADE}, which the save keeps for ids it makes: take it out."])
    plan, asked = _without_secret_asks(plan, base)
    errors, _ = O.check_structure(plan, ws.inventory())
    if base is not None and plan.kind == "update":
        # A step no step leads to in the workflow as it is now is not this change's to fix (SAI2's review, 23).
        before, _ = O.check_structure(base, ws.inventory())
        errors = [e for e in errors if not ("no step leads to it" in e and e in before)]
    errors += await _rules(plan, ws, state) + asked
    errors += await _knowledge_parts(plan, ws)
    # The person's own server first (F1225): a connected one's name is what its agents' `servers` are held to.
    errors += await own_api.server_parts(plan, server_module)
    errors += await _agent_switches(plan, ws, server_module)
    # Its eval (F1255, D340 F): only where this host offers Evals' tools (cloud withholds them until its own Evals
    # host is in: D3ASM), else left out and said, never a save that fails; then held to the Evals forms' own rules.
    eval_notes = _without_unreachable_eval(plan)
    errors += await _eval_part(plan, base, state)
    # The person's own API (F1225): each operation the plan uses is a tool the build makes, as a read API's is (F944).
    # A spec this conversation read, for a provider the lookup found, wins over the lookup's endpoints (F1298).
    errors += await own_api.from_read_specs(plan, state)
    api_errors, own_tools = await own_api.api_parts(plan)
    errors += api_errors
    if plan.schedule:
        # A schedule runs on its person's clock (F948, Naveen 10-23): the timezone they named, else their own (the
        # browser's, sent with the turn); with neither, UTC.
        from core.models_schedule import _is_timezone

        named = (plan.schedule.timezone or "").strip()
        if named and not _is_timezone(named):
            errors.append(f"schedule.timezone: '{named}' is not a place's timezone; write the place, like "
                          "America/New_York, or leave it out for the person's own")
        elif not named and state.get("person_timezone"):
            plan.schedule = plan.schedule.model_copy(update={"timezone": state["person_timezone"]})
    if errors:
        return Examined(plan, errors)
    # Only the person lets a tool act without asking, by a press on the card (SAI2's review, 11); never the planner.
    plan.approvals = O.Approvals(acts_without_asking=list(alone or []))
    plan, renamed, room_notes = await _make_room(plan, state)
    filled = await O.fill(plan, server_module, found={**_found(plan, state, renamed), **own_tools},
                          agents=ws.agents)
    misnamed = _misnamed(filled, server_module, ws, state)
    if misnamed:
        # What it is instead, the planner's to fix, before any lookup or card (F950, F1043).
        return Examined(filled, list(misnamed.values()), missing=list(filled.missing), misnamed=list(misnamed))
    issues = [i for i in await dry_issues(filled, server_module, ws, state) if not _not_the_planners(i)]
    needed = features_needed(filled, ws)
    from core.native_builder import estimate, structure

    asked = _obj(state.get("request"))
    shaped = await structure.issues(filled, ws, asked if isinstance(asked, dict) else None)
    try:
        filled.run_cost = (await estimate.of(filled, ws)).words()
    except Exception as exc:  # noqa: BLE001 — a cost not worked out is said nowhere, never a plan refused
        _log.warning("run cost not estimated: %s", exc)
    return Examined(filled, [_hinted(i) for i in issues], missing=list(filled.missing), issues=issues,
                    blocks={**blocked(filled, state), **{f: _feature_block(f) for f in needed}},
                    notes=[*room_notes, *eval_notes, *structure.as_notes(shaped)],
                    features=needed, structure=shaped)


async def _knowledge_parts(plan: O.Plan, ws: Workspace) -> list[str]:
    """The knowledge bases the plan makes or uses (F1224), as F1214's board: one of that name already here is used,
    never made again; a new one is checked as `create_knowledge_base` checks it; each file it is given is one the
    person has in the Vault, of a kind an upload takes. A file not there is said and left out, never refused."""
    if ws.kbs is None or not plan.knowledge:
        return []  # Knowledge off: the feature's card (`features_needed`)
    import asyncio

    from core.knowledge import ingest
    from core.product_tools import knowledge as K
    from core.product_tools.errors import ToolRefusal
    from core.storage import get_blob_store
    from core.vault import mention_key

    errors: list[str] = []
    here = {kb.name.lower(): kb for kb in ws.kbs.values()}
    seen: set[str] = set()
    for part in plan.knowledge:
        part.name = " ".join(part.name.split())
        if part.name.lower() in seen:
            errors.append(f"knowledge: '{part.name}' is in the plan twice; name each base once")
            continue
        seen.add(part.name.lower())
        kb = here.get(part.name.lower())
        part.existing = kb is not None
        if kb is not None:
            part.name = kb.name
            plan.knowledge_ids[kb.name] = kb.id
        else:
            try:
                await K._new_base({"name": part.name, "description": part.purpose})
            except ToolRefusal as why:
                errors.append(f"knowledge '{part.name}': {why}")
        part.files_missing = []
        for path in dict.fromkeys(p.strip().lstrip("/") for p in part.files if p.strip()):
            key = mention_key(path)
            stat = await asyncio.to_thread(get_blob_store().stat, key) if key else None
            if stat is None:
                part.files_missing.append(path)
                continue
            try:
                ingest.check_file(path, stat.size)
            except ingest.UploadError as why:
                errors.append(f"knowledge '{part.name}': {path}: {why}")
        errors += _pages_issues(part)
    return errors


def _pages_issues(part: O.KnowledgePart) -> list[str]:
    """A base's web pages held to what `add_knowledge_pages` takes (D340 F, one place): its own request
    (`UrlIngestRequest`: http(s), how many in one batch, the kind); a sitemap's or a site's pages capped by Knowledge's
    crawl limit, which the card says."""
    part.pages_cap = 0
    if not part.urls:
        return []
    from pydantic import ValidationError

    from core.knowledge.api_models import UrlIngestRequest

    try:
        UrlIngestRequest(urls=list(part.urls), kind=part.kind)
    except ValidationError as exc:
        return [f"knowledge '{part.name}': those pages can't be added: {exc.errors()[0].get('msg')}"]
    if part.kind != "page":
        if len(part.urls) != 1:
            return [f"knowledge '{part.name}': a {part.kind} is one URL, its start"]
        try:
            from core.config import load_settings
            from core.knowledge.config import knowledge_settings

            part.pages_cap = knowledge_settings(load_settings()).crawl_max_pages
        except Exception:  # noqa: BLE001 — the limit unread: the card says none
            part.pages_cap = 0
    return []


async def _agent_switches(plan: O.Plan, ws: Workspace, server_module: Any) -> list[str]:
    """Each new agent's switches as the agent editor sets them, held to what its save will check (F1223): its
    knowledge bases by name (resolved to ids here, for the save), its MCP servers (given every tool each has now), its
    Browser settings (the sites with no kept session said, never refused). What the planner can fix, as errors."""
    from core import builder_guard
    from core.browser import save_checks
    from core.builder_tools import _acting_caller

    errors: list[str] = []
    by_name = {kb.name.lower(): kb for kb in (ws.kbs or {}).values()}
    servers = _servers(server_module)
    made = {p.name.lower(): p.name for p in plan.knowledge if not p.existing}
    for a in plan.agents:
        kept: list[str] = []
        for name in a.knowledge if ws.kbs is not None else []:
            kb = by_name.get(name.strip().lower()) or ws.kbs.get(name.strip())
            if kb is None and name.strip().lower() in made:
                kept.append(made[name.strip().lower()])  # one this build makes (F1224): its id comes at the save
            elif kb is None:
                errors.append(f"agent {a.name}: knowledge: there is no knowledge base '{name}' here; name one from "
                              "knowledge_bases")
            elif kb.status != "active":
                errors.append(f"agent {a.name}: knowledge: '{kb.name}' is turned off; name an active one")
            else:
                kept.append(kb.name)
                plan.knowledge_ids[kb.name] = kb.id
        if ws.kbs is not None:
            a.knowledge = list(dict.fromkeys(kept))
        for server in a.servers:
            tools = servers.get(server.strip().lower())
            if not tools:
                errors.append(f"agent {a.name}: servers: '{server}' is no MCP server connected here"
                              + (f"; connected: {', '.join(sorted(servers))}" if servers else ""))
                continue
            named = await builder_guard.canonical_names([f"{server.strip().lower()}__{t}" for t in tools],
                                                        server_module)
            a.tools = list(dict.fromkeys([*a.tools, *named]))
        if a.browser is not None and a.type != "browser":
            errors.append(f"agent {a.name}: browser: only a browser agent has browser settings; make it type browser "
                          "or leave them out")
        elif a.browser is not None:
            a.browser.sessions_missing = await save_checks.sessions_missing(a.browser.sessions,
                                                                            caller=_acting_caller())
        errors += await _settings_issues(plan, a)
    return errors


#: How `plan.refs` keys each kind of thing a new agent names (F1259), as `compile` reads them.
_REF_KINDS = {"repos": "repo", "db_configs": "db", "workflow": "workflow", "recordings": "recording"}


async def _settings_issues(plan: O.Plan, a: O.NewAgent) -> list[str]:
    """The rest of a new agent's settings (F1259, `agent_census`), held to what the agent editor lets a person pick:
    each on the kind of agent that has it, and each thing it names one that is here; what it names is kept as ids in
    `plan.refs` for the compile."""
    from core.routes.db_configs import load_db_configs
    from core.routes.repos import load_repos
    from core.store.resources import load_orchestrations

    errors: list[str] = []
    kinds = {"team": ("team",), "repos": ("code",), "db_configs": ("code",), "workflow": ("orchestrator",)}
    for field, allowed in kinds.items():
        if getattr(a, field) and a.type not in allowed:
            errors.append(f"agent {a.name}: {field}: only a {' or '.join(allowed)} agent has it; make it that type or "
                          "leave it out")
    if a.type == "team":
        # Each member is an agent this plan makes or one that is here (TRI1): never a name nothing answers to.
        from core.store.resources import load_agents

        made = {n.name.lower() for n in plan.agents if n.name != a.name}
        here = {str(x.get("name") or "").lower(): x["id"] for x in await load_agents() if not x.get("system")}
        here_ids = set(here.values())
        for member in a.members:
            if member in here_ids or member.lower() in made:
                continue
            if member.lower() in here:
                plan.refs[f"agent:{member}"] = here[member.lower()]
                continue
            errors.append(f"agent {a.name}: members: '{member}' is no agent here and none this plan makes")
    if a.type == "orchestrator" and not a.workflow:
        errors.append(f"agent {a.name}: an orchestrator agent runs one workflow: name it in `workflow`")
    if a.skills:
        from core import skills as library

        have = {skill["name"].lower(): skill["name"] for skill in await library.list_skills()}
        for name in a.skills:
            if name.lower() not in have:
                errors.append(f"agent {a.name}: skills: there is no skill '{name}' here")
        a.skills = [have[n.lower()] for n in a.skills if n.lower() in have]

    async def named(kind: str, wanted: list[str], rows: list[dict], label: str) -> None:
        by = {str(r.get("name") or "").lower(): r["id"] for r in rows if r.get("id")}
        ids = {r["id"] for r in rows if r.get("id")}
        for name in wanted:
            found = name if name in ids else by.get(name.strip().lower())
            if found is None:
                errors.append(f"agent {a.name}: {kind}: there is no {label} '{name}' here"
                              + (f"; there are {', '.join(sorted(r['name'] for r in rows if r.get('name'))[:10])}"
                                 if rows else ""))
            else:
                plan.refs[f"{_REF_KINDS[kind]}:{name}"] = found

    if a.repos:
        await named("repos", a.repos, await load_repos(), "repository")
    if a.db_configs:
        await named("db_configs", a.db_configs, await load_db_configs(), "database")
    if a.workflow:
        await named("workflow", [a.workflow], await load_orchestrations(), "workflow")
    if a.browser is not None and a.browser.recordings:
        from core.browser import recordings

        await named("recordings", a.browser.recordings, await recordings.all_recordings(), "recording")
    return errors


def features_needed(plan: O.Plan, ws: Workspace) -> list[str]:
    """The features this plan needs that are off here: Boards for an agent's board tools, Knowledge for a Retrieve or
    Ingest step (F1203). The planner plans what the request needs; turning a feature on is the person's."""
    from core.boards import TOOL_NAMES

    steps = [*plan.steps, *(s for w in plan.inner_workflows for s in w.steps)]
    out = []
    # A board the plan names counts too (F1214): it is the workflow's own, and Boards must be on for it.
    if not ws.boards_on and (any(t in TOOL_NAMES for t in O.tool_names(plan)) or plan.boards):
        out.append("boards")
    if ws.kbs is None and (any(s.type in ("retrieve", "ingest") for s in steps)
                           or any(a.knowledge for a in plan.agents) or plan.knowledge):
        out.append("knowledge")
    return out


def _feature_block(feature: str) -> str:
    """The card's line for a feature that is off: what, where its switch is, and the other way."""
    from core.features import off_sentence

    without = "the board" if feature == "boards" else "its knowledge steps"
    return off_sentence(feature).rstrip(".") + f", or build without {without}"


def _may_turn_on() -> bool:
    """Whether this build's person may press a feature's switch (the Settings screen's admin)."""
    from core.builder_access import current_caller
    from core.product_tools.roles import Role

    caller = current_caller()
    return caller is None or caller.role.can_act_as(Role.ADMIN)


def _needs(plan: O.Plan, tools: list[str], state: dict) -> list[dict[str, str]]:
    """What each missing tool is for, as the route's lookup reads it (`{{state.plan_checked.needs}}`, SAI2-P5's
    `look_up_providers`): `{tool, does, provider?}`. `does` is the first step's own sentence that calls it, else the
    purpose of the new agent that holds it. `provider` is a service the person named (the request's `providers`), set
    only on the need whose own words name it: the lookup gives a final only to a named provider, so code never
    guesses one, and one named for one tool is never given to another."""
    from core.builder_research import _words

    request = _obj(state.get("request"))
    named = [p.strip() for p in (request.get("providers") if isinstance(request, dict) else None) or []
             if isinstance(p, str) and p.strip()]
    steps = [*plan.steps, *(s for w in plan.inner_workflows for s in w.steps)]
    out: list[dict[str, str]] = []
    for tool in tools:
        step = next((s for s in steps if s.tool == tool), None)
        agent = next((a for a in plan.agents if tool in a.tools), None)
        does = (step.does or step.name) if step else (agent.purpose if agent else "")
        need = {"tool": tool, **({"does": does.strip()[:200]} if does and does.strip() else {})}
        said = set(_words(" ".join([tool.replace("_", " "), does or "",
                                    *((step.name, step.prompt or "") if step else ())])))
        provider = [p for p in named if set(_words(p)) and set(_words(p)) <= said]
        if len(provider) == 1:
            need["provider"] = provider[0][:80]
        out.append(need)
    return out


def blocked(plan: O.Plan, state: dict) -> dict[str, str]:
    """Each tool a policy blocks where this plan uses it, with why, in the person's words: the run could not call it,
    so the plan cannot be built as it is (F964; SAI2's point 1: a block is not an approval). Only the plan's own
    uses block it: its new agents' tools and the Tool steps it writes; on an update, a step it leaves as it is
    never does (SAI2's review, S2). A reused agent's blocked tools are said on the card, as it cannot use them today
    either. The person's to change, not the planner's: never sent back to it (S1)."""
    new = {a.name for a in plan.agents}
    base = _base(state) if plan.kind == "update" else None
    kept = {x.id: x.model_dump() for x in base.steps} if base is not None else {}
    written = {x.id for x in [*plan.steps, *(y for w in plan.inner_workflows for y in w.steps)]
               if kept.get(x.id) != x.model_dump()}
    out: dict[str, str] = {}
    for g in plan.gates:
        if g.decision == "deny" and (g.kind == "step" and g.step in written or g.kind == "agent" and g.who in new):
            out.setdefault(g.tool, f"{_WHOSE[g.set_by].format(who=g.who)} blocks `{g.tool}` for "
                           + (f'the step "{g.who}"' if g.kind == "step" else g.who)
                           + (": change it in Settings, or build without it" if g.set_by != "agent"
                              else ": change it on the agent, or build without it"))
    return out


#: What set a gate, in the card's words.
_WHOSE = {"workspace": "this workspace's tool policy", "agent": "{who}'s own tool policy",
          "build": "this build"}


def _written(plan: O.Plan, base: O.Plan | None) -> dict:
    """What the plan itself writes: the whole plan on a create; on an update, the steps it adds or changes and its
    own parts, never the workflow's steps left as they are."""
    dumped = plan.model_dump()
    if base is None or plan.kind != "update":
        return dumped
    before = {s.id: s.model_dump() for s in base.steps}
    dumped["steps"] = [s for s in dumped["steps"] if before.get(s["id"]) != s]
    return dumped


#: What goes back to the planner for each part of a plan that asked for a secret (F1093).
NEVER_ASK = ("asks the person for a key, a token or a password: never ask for one. A service's tool is planned as "
             "it is, and its key comes through a Connect card after the yes; ask only what the person alone knows")


def _without_secret_asks(plan: O.Plan, base: O.Plan | None) -> tuple[O.Plan, list[str]]:
    """The plan with every part that asks the person for a secret taken out, and what goes back to the planner for
    each (F1093; H45 res-05's open question asked for the Stripe and SendGrid keys). A question naming one is
    dropped; a sentence of prose that asks for one is cut. Code's floor: a plan reviewed after its sendbacks never
    shows one. On an update, only what this plan writes."""
    from core.secret_scrub import asks_for_a_secret, without_secret_asks

    issues: list[str] = []
    questions = [q for q in plan.questions if not asks_for_a_secret(q)]
    if len(questions) != len(plan.questions):
        issues.append(f"questions: an open question {NEVER_ASK}")

    def prose(where: str, text: str | None) -> str | None:
        cut, was = without_secret_asks(text or "", line=None)
        if not was:
            return text
        issues.append(f"{where} {NEVER_ASK}")
        return cut

    kept = {s.id: s.model_dump() for s in base.steps} if base is not None else {}

    def step(s: O.Step) -> O.Step:
        if kept.get(s.id) == s.model_dump():
            return s
        return s.model_copy(update={"name": prose(f"step {s.id}: its name", s.name) or s.name,
                                    "does": prose(f"step {s.id}: what it does", s.does) or ""})

    cleaned = plan.model_copy(update={
        "questions": questions,
        "summary": prose("summary: it", plan.summary) or "",
        "steps": [step(s) for s in plan.steps],
        "agents": [a.model_copy(update={"purpose": prose(f"agent {a.name}: its purpose", a.purpose) or ""})
                   for a in plan.agents],
        "inner_workflows": [w.model_copy(update={"steps": [step(s) for s in w.steps]})
                            for w in plan.inner_workflows],
    })
    return cleaned, issues


async def _rules(plan: O.Plan, ws: Workspace, state: dict) -> list[str]:
    """The planner's rules a structure check cannot see (P1's findings, F866b)."""
    from core.boards import TOOL_NAMES

    errors = []
    from core.native_builder.change_tools import _ASKS_SECRET, _ASKS_WAY_IN

    # On an update, the workflow's own steps left as they are were not written by this plan: only what it adds or
    # changes is held to these rules (SAI2's check of 80de4238; `_written`'s rule for keys). Inner workflows are new.
    base = _base(state) if plan.kind == "update" else None
    kept = {x.id: x.model_dump() for x in base.steps} if base is not None else {}
    written = [x for x in plan.steps if kept.get(x.id) != x.model_dump()]
    for s in [*written, *(x for w in plan.inner_workflows for x in w.steps)]:
        if s.type == "human":
            # What a run asks its person is kept in the run: never a key, a password or how to connect a service
            # (gpt-oss on dev planned "connect your Xero account by providing the API key" as a step; SAI2, 10-23).
            asked = " / ".join([s.prompt or "", json.dumps((s.config or {}).get("human_fields") or [])])
            if _ASKS_SECRET.search(asked) or _ASKS_WAY_IN.search(asked):
                errors.append(f"step {s.id}: a person is never asked for a key, a password or how to connect a "
                              "service while a workflow runs: a key is added in Settings → Vault or on a Connect "
                              "card. Ask only what the person alone knows.")
        if setup_step(f"{s.name} {s.does}"):
            errors.append(f"step {s.id}: \"{s.name}\" is done once by the person, never by each run: the plan's "
                          "Connect card after the yes covers connecting a service, and a step only does what each run "
                          "does; take it out")
        if s.type == "tool" and s.tool in TOOL_NAMES:
            errors.append(f"step {s.id}: board tools are an agent's, not a Tool step's: give `{s.tool}` to the "
                          "agent of an agent step")
        if s.type == "transform" and not ws.code_steps:
            errors.append(f"step {s.id}: this workspace's plan does not run code, so there are no transform steps: "
                          "use an `llm` step to reshape data")
        elif s.type == "transform" and (reach := reaches_out((s.config or {}).get("transform_code") or "")):
            errors.append(f"step {s.id}: its code does not parse ({reach[1]}): fix it" if reach[0] == "parse" else
                          f"step {s.id}: its code uses `{reach[1]}`, but a step reaches a service through a tool, "
                          "and its key through the Vault, never from code. Plan a tool step for the service (a tool "
                          "nothing here provides is looked up, or built without); code works on the run's own data")
    # A feature that is off, where the plan needs it, is the person's to turn on, never the planner's to take out
    # (F1203): `features_needed`, a block on the card with its switch. Board tools are still never looked up as a
    # missing tool (F866b's invented "Board" provider; SAI2's review, 7).
    # When a board is the right thing at all (F1254): not for a hand-off, and never one nothing reads.
    errors += boards_rule.issues(plan)
    if len(plan.boards) > 1:
        # A workflow's agents post to its own board (no product tool gives a workflow a shared board yet; SAI2's
        # review, 20): the one board a plan names is that board, under the person's name for it (F1214).
        errors.append("boards: a build has one board, the workflow's own; name only the one the person asked for")
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
    errors += _api_names(plan, state)
    errors += _a_second_of_a_name(plan, ws, state)
    return errors


def _a_second_of_a_name(plan: O.Plan, ws: Workspace, state: dict) -> list[str]:
    """A new workflow named like one already here is never saved beside it (F1229, CAP3's stage 1): brief 4's "run it"
    and brief 7's follow-up each saved a second, same-named workflow. Sent back to the planner: an update of that one,
    or, when the person may want two, one question (counted with the request's, F1226) and a name of its own."""
    from core.native_builder.target import LAST_BUILT_KEY

    if plan.kind != "create":
        return []
    def key(name: Any) -> str:
        return "".join(c for c in unicodedata.normalize("NFKC", str(name or "")).casefold() if c.isalnum())
    want = key(plan.title)
    same = next(((i, w) for i, w in ws.workflows.items() if want and key(w.get("name")) == want), None)
    if same is None:
        return []
    ident, row = same
    whose = " (this conversation built it)" if state.get(LAST_BUILT_KEY) == ident else ""
    return [f"title: a workflow called \"{row.get('name')}\" is already in this workspace{whose}, id {ident}. Never "
            "save a second of the same name: when the person means that one, this is a change to it (plan it as kind "
            "update of that one); if they may want two, ask once which with ask_person, and give a second one a name "
            "of its own"]


def _api_names(plan: O.Plan, state: dict) -> list[str]:
    """A tool that names a provider whose API was read, but none of its calls: the plan calls a read API's endpoint by
    its tool's name, so code makes exactly that call (A7, F944). A tool of an API the plan names in `apis` is that
    spec's, by the spec's own names (F1225, F1298): never held to the web lookup's."""
    from core.builder_research import _words
    from core.native_builder.own_api import prefix_of

    findings = _obj(state.get(FINDINGS_KEY))
    own = tuple(prefix_of(p.name) + "_" for p in plan.apis)
    errors = []
    for option in (findings or {}).get("options") or [] if isinstance(findings, dict) else []:
        spec = option.get("api") if isinstance(option.get("api"), dict) else None
        tools = [e.get("tool") for e in (spec or {}).get("endpoints") or [] if e.get("tool")]
        if not tools:
            continue
        provider = set(_words(str(option.get("provider") or "")))
        for name in O.tool_names(plan):
            if own and name.startswith(own):
                continue
            if name not in tools and provider and provider <= set(_words(name.replace("_", " "))):
                errors.append(f"`{name}`: {option.get('provider')}'s API was read; call it by its tools' names: "
                              + ", ".join(f"`{t}`" for t in tools))
    return errors


#: What a build's code may not import: each reaches a service, runs a program, or builds an import from text (F947).
_REACHES = frozenset({"requests", "httpx", "urllib3", "aiohttp", "socket", "socketserver", "subprocess",
                      "ftplib", "smtplib", "poplib", "imaplib", "nntplib", "telnetlib", "websocket", "websockets",
                      "importlib", "ssl", "ctypes", "multiprocessing", "pty", "xmlrpc", "paramiko", "pycurl"})
#: The parts of `urllib` and `http` that reach something; the rest (`urllib.parse`, `http.HTTPStatus`,
#: `http.cookies`) only reads text (SAI2's re-check, 3).
_REACHING_PARTS = {"urllib": frozenset({"request", "robotparser"}),
                   "http": frozenset({"client", "server", "cookiejar"})}
#: `os`'s names that read its environment or run a program, exactly.
_OS_NAMES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv", "system", "popen", "fork",
                       "forkpty", "startfile", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp",
                       "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                       "spawnvpe", "posix_spawn", "posix_spawnp"})
#: The builtins that run text as code.
_RUNS_TEXT = frozenset({"exec", "eval", "compile", "__import__"})
#: Names that open a connection or a process whatever module they come from (`asyncio`'s, chiefly).
_OPENS = frozenset({"open_connection", "start_server", "open_unix_connection", "start_unix_server",
                    "create_subprocess_exec", "create_subprocess_shell"})


def _reaching(module: str, names: set[str] | None = None) -> bool:
    """Whether importing `module` (or `names` from it) reaches something."""
    parts = module.split(".")
    if parts[0] in _REACHES:
        return True
    if parts[0] in _REACHING_PARTS:
        reaching = _REACHING_PARTS[parts[0]]
        if len(parts) > 1:
            return parts[1] in reaching
        # `import urllib` alone could reach `urllib.request`; `import http` and `from http import HTTPStatus` read.
        return parts[0] == "urllib" if names is None else bool(names & reaching)
    return False


def reaches_out(code: str) -> tuple[str, str] | None:
    """`("parse", why)` for code that does not compile as the engine runs it, `("uses", what)` for code that reaches a
    service, runs a program, reads the environment or runs text as code; else None.

    Read with `ast` (an alias is the same import) at module level, as `wrap_transform` runs it (SAI2's review, S6: a
    top-level `return` cannot run; a transform sets `result`). A lint against a model's drift, not a boundary: it
    cannot see a library that fetches for you (`pandas.read_csv(url)`) — the sandbox's network is that (SAI2's row).
    """
    import ast

    try:
        tree = ast.parse(code or "", "<transform>", "exec")
        compile(tree, "<transform>", "exec")
    except SyntaxError as e:
        why = str(e.msg)
        if "return" in why:
            why += "; a transform puts its answer in `result` (result = ...) instead"
        return "parse", why
    os_aliases, imported = {"os"}, set()
    opened = {id(n.args[0]) for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "open" and n.args}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_aliases.add(alias.asname or "os")
                if _reaching(alias.name):
                    return "uses", alias.name
        elif isinstance(node, ast.ImportFrom):
            module, names = node.module or "", {a.name for a in node.names}
            imported |= {a.asname or a.name for a in node.names}
            if _reaching(module, names):
                return "uses", module
            if module == "os" and (bad := sorted(names & _OS_NAMES)):
                return "uses", f"os.{bad[0]}"
            if "__import__" in names:
                return "uses", "__import__"
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
            # `getattr(os, "environ")` is `os.environ`; `getattr(row, "system")` is the row's (SAI2's re-check, 3).
            obj, name = node.args[0], node.args[1].value
            on_os = isinstance(obj, ast.Name) and obj.id in os_aliases
            if name in (_RUNS_TEXT | _OPENS | {"__builtins__"}) or (on_os and name in _OS_NAMES):
                return "uses", str(name)
        if isinstance(node, ast.Attribute):
            base = node.value.id if isinstance(node.value, ast.Name) else None
            if base in os_aliases and node.attr in _OS_NAMES:
                return "uses", f"os.{node.attr}"
            if base in _REACHING_PARTS and node.attr in _REACHING_PARTS[base]:
                return "uses", f"{base}.{node.attr}"
            if node.attr in ("__import__", "__builtins__") or node.attr in _OPENS:
                return "uses", node.attr
        elif isinstance(node, ast.Name) and node.id not in imported and (
                node.id in _RUNS_TEXT or node.id == "__builtins__"):
            # The builtin only: `from re import compile` is re's (SAI2's re-check, 3).
            return "uses", node.id
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text.startswith("/proc/") and (id(node) in opened or not any(c.isspace() for c in text)):
                return "uses", text
            if text in ("__import__", "__builtins__"):
                return "uses", text
    return None


async def _make_room(plan: O.Plan, state: dict) -> tuple[O.Plan, dict[str, str], list[str]]:
    """A read API's tool whose name is already taken here (F944; SAI2's addition 1): the same provider's own HTTP tool
    (its URL on one of the provider's own domains) is the one used; anything else is left alone, never adopted or
    overwritten, and this build makes its own as `<name>_2`, which the plan's steps and agents then call. Returns the
    plan, `{new name: the endpoint's}` and the card's notes."""
    from core.builder_research import _host, _registrable_domain
    from core.store.resources import load_tools

    findings = _obj(state.get(FINDINGS_KEY))
    owned: dict[str, set[str]] = {}
    for option in (findings or {}).get("options") or [] if isinstance(findings, dict) else []:
        spec = option.get("api") if isinstance(option.get("api"), dict) else None
        for e in (spec or {}).get("endpoints") or []:
            if e.get("tool"):
                owned[e["tool"]] = {str(d) for d in option.get("own_domains") or []}
    used = set(O.tool_names(plan))
    if not owned or not used & owned.keys():
        return plan, {}, []
    tools = {t.get("name"): t for t in await load_tools()}
    renames: dict[str, str] = {}
    for name in sorted(used & owned.keys()):
        taken = tools.get(name)
        # The same provider's own tool is used, once connected; one still waiting for its card (another build's) is
        # that build's, never adopted (SAI2's review, 5).
        from core.http_tool import _UNCONNECTED

        if taken is None or (taken.get("tool_type") == "http"
                             and not _UNCONNECTED.search(str(taken.get("base_lock") or ""))
                             and _registrable_domain(_host(str(taken.get("url") or ""))) in owned[name]):
            continue
        n = 2
        while f"{name}_{n}" in tools or f"{name}_{n}" in used:
            n += 1
        renames[name] = f"{name}_{n}"
    if not renames:
        return plan, {}, []
    out = plan.model_copy(deep=True)
    for step in [*out.steps, *(x for w in out.inner_workflows for x in w.steps)]:
        if step.tool in renames:
            step.tool = renames[step.tool]
    out.agents = [a.model_copy(update={"tools": [renames.get(t, t) for t in a.tools]}) for a in out.agents]
    notes = [f"`{old}` is already a tool here, from somewhere else, so this build makes its own as `{new}` and leaves "
             "that one as it is." for old, new in renames.items()]
    return out, {new: old for old, new in renames.items()}, notes


#: A model's own work, written as a tool name (F1043 b): what an agent or an llm step does with words.
_MODEL_WORK = frozenset({
    "summarise", "summarize", "summary", "summaries", "summarising", "summarizing", "summarisation", "summarization",
    "analyse", "analyze", "analysis", "analysing", "analyzing", "classify", "classification", "categorise",
    "categorize", "categorisation", "categorization", "sentiment", "generate", "generation", "write", "draft",
    "compose", "rewrite", "translate", "translation", "reason", "reasoning", "decide", "decision", "judge", "evaluate",
    "evaluation", "interpret", "understand", "compare", "comparison", "nlp", "natural", "language", "processing", "llm",
    "think"})
#: Keeping a step's result, written as a tool name: a step's output is already kept in the run's state.
_KEEP = frozenset({"store", "save", "keep", "remember", "persist", "record"})
#: Words that say nothing of where: a name made only of these and the two sets above names no service.
_FILLER = frozenset({"text", "data", "result", "results", "output", "outputs", "content", "changes", "change", "diff",
                     "info", "information", "report", "the", "a", "an", "of", "to", "and", "for", "with", "from", "it",
                     "this", "that", "new", "model", "step", "what", "changed"})
#: A scraping job's own words (F950), and the built-in scraper's tools that do it.
_SCRAPE_JOB = frozenset({"scrape", "scraper", "scraping", "crawl", "crawler", "crawling", "webpage", "website",
                         "html", "browse", "browser"})
_SCRAPE_WORDS = _SCRAPE_JOB | {"fetch", "web", "page", "pages", "site", "url", "read", "extract"}
_BUILT_IN_SCRAPE = ("scrape_url", "scrape_structured", "crawl_multiple", "search_page", "extract_links")
#: What a request may need beyond a built-in scraper (SAI2's change 3: a code fact, never a model's judgement): each
#: word, when the request says it and no built-in's own description does, lets another service be looked up.
SCRAPE_BEYOND = {"proxy": ("proxy", "proxies"), "javascript": ("javascript", "js"), "rendering": ("rendering",),
                 "thousands": ("thousands", "000")}


#: The person's own setup, done once, written as a step (HOT2's read of the Weekly Twitter Post plan): never a step of
#: each run. Matched on a step's name and what it does; tight, so an act of each run ("create a customer account in
#: Stripe", "connect to the database") is never one.
_SETUP = tuple(re.compile(p, re.I) for p in (
    r"\bsign\s*-?\s*up\b",
    r"\b(?:create|register|open|set\s*up)\b[^.]{0,40}\bdeveloper\s+(?:account|app|portal|access)\b",
    r"\b(?:create|register|open|set\s*up)\s+(?:your|my|our)\b[^.]{0,30}\baccount\b",
    r"\b(?:get|obtain|generate|request|create|copy|fetch)\b[^.]{0,30}\b(?:api\s+key|access\s+token|api\s+token|"
    r"bearer\s+token|credentials?|client\s+secret|secret\s+key)\b",
    r"\b(?:integrate|install|add|plug)\b[^.]{0,30}\b(?:tool|integration|plugin|app|connector)\b[^.]{0,20}"
    r"\b(?:into|to|in)\b[^.]{0,20}\b(?:orchestration|workflow|workspace|synapse)\b",
    r"\binstall\s+(?:the\s+)?\w+\s+(?:app|plugin|integration|connector)\b",
    r"\b(?:connect|link|authori[sz]e|authenticate)\s+(?:your|my|our)\b[^.]{0,30}\b(?:account|app|workspace)\b",
    r"\benable\s+(?:the\s+)?\w+(?:\s+\w+)?\s+(?:api|setting|permission|integration|access)\b",
))


def setup_step(text: str) -> bool:
    """Whether a step's name and what it does say the person's own setup, done once: a developer account, a key or
    a token, installing or integrating a tool, connecting an account, enabling a setting."""
    return any(p.search(text or "") for p in _SETUP)


def model_work(name: str) -> bool:
    """Whether a tool name says only a model's own work, or keeping a result (F1043 b): `store_result`,
    `natural_language_processing`, `summarize_text`; never a name that says where (`save_to_google_sheet`)."""
    words = set(re.findall(r"[a-z]+", name.lower()))
    return bool(words) and words <= (_MODEL_WORK | _KEEP | _FILLER) and bool(words & (_MODEL_WORK | _KEEP))


def imitates_eval(plan: O.Plan, ws: Workspace) -> str | None:
    """Why a plan is Evals made by hand, or None (F1255): the imitation checker's Evals row (F1257 step 3)."""
    return next((f["say"] for f in imitation.findings(plan, ws) if f["screen"] == "evals"), None)


#: The tools a build's eval is saved through (`build_save._eval_calls`): without them here, no eval is offered.
EVAL_TOOLS = ("save_rubric", "attach_rubric")
#: What the card says when this host offers no Evals tools, as the feature census says a feature it cannot reach.
EVALS_ELSEWHERE = ("Its eval is left out: setting a rubric and test cases is not something I can do here yet. Open "
                   "Evals (/evals) to score this workflow's runs.")


def evals_reachable() -> bool:
    """Whether this host offers the tools a build's eval is saved through: a structural check on the registry, never
    words (a host withholds an area it has not wired; cloud's evals until its Evals host)."""
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    return all(registry.get(name) is not None for name in EVAL_TOOLS)


def _without_unreachable_eval(plan: O.Plan) -> list[str]:
    """A plan's eval taken out where this host cannot save it, with the census's words for the card."""
    if plan.eval is None or evals_reachable():
        return []
    plan.eval = None
    return [EVALS_ELSEWHERE]


async def _eval_part(plan: O.Plan, base: O.Plan | None, state: dict) -> list[str]:
    """A plan's eval held to the Evals screen's own rules (`core.evals.service`) and to TRI1's shape (D340 F): a rubric
    with at least one criterion; 3 to 10 cases when the workflow takes an input (every one is on the card), none when
    it takes none (its scores then come from its real runs). An update keeps the rubric its workflow is scored by,
    unless the ask is about how it is judged (`replace_rubric`: its score trend then starts again)."""
    part = plan.eval
    if part is None:
        return []
    from core.evals import service
    from core.native_builder.models_menu import workspace_model

    part.keeps = part.replaces = ""
    existing = (_obj(state.get(EXISTING_KEY)) or {}) if plan.kind == "update" and base is not None else {}
    if existing.get("rubric_id"):
        rubric = await service.evals_store.get_rubric(existing["rubric_id"])
        named = str((rubric or {}).get("name") or existing["rubric_id"])
        if part.replace_rubric:
            part.replaces = named
        else:
            part.keeps = named
    errors = []
    if not part.keeps:
        if not part.criteria:
            errors.append("eval: a rubric needs at least one criterion: what a good result is, from what the workflow "
                          "is for")
        else:
            try:
                service.rubric_body(rubric_of(part))
            except service.EvalsRefused as exc:
                errors.append(f"eval: {exc.detail}")
    if _takes_input(plan):
        if not 3 <= len(part.cases) <= 10:
            errors.append("eval.cases: give 3 to 10 cases (a typical one, an edge case, a hard one); more are added "
                          "later on Evals")
    elif part.cases:
        errors.append("eval.cases: this workflow takes no input, so a case has nothing to start a run with: leave "
                      "cases out; its scores come from its real runs")
    if part.cases and not errors:
        try:
            service.suite_body({"name": "golden set", "orchestration_id": "orch_check",
                                "cases": [c.model_dump() for c in part.cases]}, whole=True)
        except service.EvalsRefused as exc:
            errors.append(f"eval.cases: {exc.detail}")
    if isinstance(part.judge_every, bool) or not 1 <= part.judge_every <= service.MAX_JUDGE_EVERY:
        errors.append(f"eval.judge_every: a whole number from 1 (every run) to {service.MAX_JUDGE_EVERY}")
    try:
        from core.config import load_settings

        part.judge = str(load_settings().get("judge_model") or "").strip() or workspace_model()
    except Exception:  # noqa: BLE001 — no settings read: the card names none
        part.judge = ""
    part.judge_cost = await judge_cost_words(part.judge)
    return errors


#: A judge call's size, roughly (`judge.build_prompt`: the rubric, the run's output, its steps and state, capped;
#: a short JSON verdict back): what one judged run costs is priced from these.
JUDGE_TOKENS_IN, JUDGE_TOKENS_OUT = 4_000, 400


async def judge_cost_words(model: str) -> str:
    """About what one judged run costs on `model`, from the rate card the model menu reads (D340 F); "" unknown."""
    if not model:
        return ""
    from core.native_builder import models_menu as MM

    entry = next((e for e in await MM.menu() if e.id == model), None)
    if entry is None or entry.price_in is None or entry.price_out is None:
        return ""
    dollars = (entry.price_in * JUDGE_TOKENS_IN + entry.price_out * JUDGE_TOKENS_OUT) / 1_000_000
    return f"about ${dollars:.4f}" if dollars < 0.01 else f"about ${dollars:.2f}"


#: How a run's own input is read (F1232: what the person gives when it starts, or its schedule's message).
_RUN_INPUT = re.compile(r"state\.(?:user_input|user_query|input)\b")


def _takes_input(plan: O.Plan) -> bool:
    """Whether the workflow reads what each run starts with: a golden set's case is that."""
    return bool(_RUN_INPUT.search(json.dumps([s.model_dump() for s in [*plan.steps,
                                                                        *(x for w in plan.inner_workflows
                                                                          for x in w.steps)]], default=str)))


def rubric_of(part: O.EvalPart) -> dict:
    """A plan's eval as the rubric the Evals screen saves (`RubricIn`'s fields)."""
    return {"name": part.name, "criteria": [c.model_dump() for c in part.criteria],
            "scale": {"min": 0.0, "max": part.scale_max}, **({"floor": part.floor} if part.floor is not None else {})}


def _servers(server_module: Any) -> dict[str, list[str]]:
    """Each tool server's name and its tools, from the router's `server__tool` keys."""
    router = getattr(server_module, "tool_router", None) or {}
    out: dict[str, list[str]] = {}
    for key in list(dict.keys(router)) if isinstance(router, dict) else []:
        server, sep, tool = str(key).partition("__")
        if sep and tool:
            out.setdefault(server.lower(), []).append(tool)
    return out


def _misnamed(plan: O.Plan, server_module: Any, ws: Workspace, state: dict) -> dict[str, str]:
    """Each name nothing provides that code can say what it is instead, and what to write (F950, F1043): a tool
    server's name, a model's own work, or a job the built-in scraper does under a service the person never named.
    Research runs only for what is left: a named service, or a real gap."""
    servers = _servers(server_module)
    here = {t["name"] for t in ws.tools}
    described = " ".join(t.get("does") or "" for t in ws.tools if t["name"] in _BUILT_IN_SCRAPE).lower()
    said = set(re.findall(r"[a-z0-9]+", " ".join(str(state.get(k) or "") for k in ("user_message", "chat_history"))
                          .lower()))
    out: dict[str, str] = {}
    for t in plan.tools:
        if t.status != "missing":
            continue
        name = t.name
        words = set(re.findall(r"[a-z0-9]+", name.lower()))
        if name.lower() in servers:
            tools = sorted(servers[name.lower()])
            out[name] = (f"`{name}` is a tool server, not a tool; its tools are "
                         + ", ".join(f"`{x}`" for x in tools[:12]) + ": name the one this step uses")
        elif model_work(name):
            out[name] = (f"`{name}` is a model's own work, not a tool: write it as an `agent` or `llm` step; a step's "
                         "output is kept in the run's state, so nothing stores it")
        elif words & _SCRAPE_JOB and (built := [b for b in _BUILT_IN_SCRAPE if b in here]):
            others = words - _SCRAPE_WORDS - _FILLER - _MODEL_WORK - _KEEP
            beyond = [need for need, says in SCRAPE_BEYOND.items()
                      if set(says) & said and not any(w in described for w in says)]
            if not (others & said) and not beyond:
                # Built-in always, unless named (Naveen, 10-24): another service only when the person named it.
                out[name] = (f"`{name}`: this workspace's own `{built[0]}` does that, built in (also "
                             + ", ".join(f"`{b}`" for b in built[1:]) + "); use it. Another service only when the "
                             "person names it") if len(built) > 1 else (
                    f"`{name}`: this workspace's own `{built[0]}` does that, built in; use it. Another service only "
                    "when the person names it")
    return out


#: An API whose only way in is a person's OAuth sign-in (Gmail, Google Calendar): a token pasted on a card expires
#: within the hour, so no tool is made from its spec, however well it reads (F1121). A key it also takes is a way in.
_OAUTH = re.compile(r"\boauth\b|\bconsent screen\b", re.IGNORECASE)
_A_KEY = re.compile(r"\b(?:api[ -]?(?:key|token)s?|personal access tokens?|secret keys?|private app (?:token|key)s?)\b",
                    re.IGNORECASE)


def _signs_in_only(option: dict) -> bool:
    """Whether an option's own words say its API takes an OAuth sign-in and no key (F1121)."""
    said = f"{option.get('needs') or ''}\n{option.get('detail') or ''}"
    return bool(_OAUTH.search(said)) and not _A_KEY.search(said)


def _found(plan: O.Plan, state: dict, renamed: dict[str, str] | None = None) -> dict[str, O.ToolStatus]:
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
            spec = option.get("api") if isinstance(option.get("api"), dict) else None
            if spec is not None and how == "api" and _signs_in_only(option):
                spec = None  # F1121: no tool from a spec whose key is a sign-in; the card says so plainly
            # A7: a read API's call is named exactly, by its endpoint's tool; the status carries that one endpoint.
            endpoint = next((e for e in (spec or {}).get("endpoints") or []
                             if e.get("tool") == (renamed or {}).get(name, name)), None)
            if endpoint is None and (not provider or not set(_words(provider)) <= words):
                continue
            # The hand-off's table (P3B-HANDOFF.md, A6): a way in is found; a verdict of none is missing, with why.
            status, via = {"mcp": ("found", "mcp_server"), "catalog_app": ("found", "catalog_app"),
                           "api": ("found", "http_tool" if endpoint else "none")}.get(how, ("missing", "none"))
            api = ({**{k: v for k, v in spec.items() if k != "endpoints"}, "endpoint": endpoint}
                   if spec and endpoint else None)
            detail = str(option.get("detail") or "") or None
            if how == "partner":
                # F1094: the research's own words say only approved partners get in; its line on what the API does
                # would read as a way in, so the card says the gate instead.
                words = ", ".join(str(w) for w in option.get("partner_words") or [] if str(w).strip())
                detail = f"{provider}'s API is for approved partners only" + (f" ({words})" if words else "")
            found[name] = O.ToolStatus(name=name, status=status, provider=provider, via=via, detail=detail, api=api)
            break
    return found


async def dry_issues(plan: O.Plan, server_module: Any, ws: Workspace, state: dict) -> list[str]:
    """Every issue a save door would find, before anything is saved: the compiler's own, the workspace's
    (`compile.validate`: F489, E1's arguments, BWA1's decision 4), the engine's save rule (F850/F853), F691's
    cross-workflow loops, knowledge bases by id, the transform gate, and a Browser step's recording, inputs, sends
    and sessions as every builder door checks them (F1189, F1193)."""
    from core.builder_tools import (_knowledge_step_issues, _refuse_browser_steps, _refuse_ungated_transform,
                                    _workflows_called)
    from core.store.resources import get_orchestration
    from core.native_builder.compile import compile_plan, validate
    from core.orchestration import save_rules

    existing = {i: a for i, a in ws.agents.items()}
    compiled = compile_plan(plan, existing_agents=existing, mint=placeholder_mint(), orch_id=_orch_id(plan, state))
    await validate(compiled, plan, server_module, existing, own_steps=_changed_steps(plan, state, compiled))
    issues = compiled.all_issues() + inner_order(compiled)[1]
    agents = existing | {a["id"]: a for a in compiled.agents}
    from core.native_builder.agent_census import editor_problem

    for a in compiled.agents:
        # Each new agent as the agent editor's own model takes it (F1259): what a person couldn't save, a plan can't.
        if (said := editor_problem(a)) is not None:
            issues.append(f"agent {a.get('name')}: {said}")
    for doc in [compiled.orchestration, *(i.orchestration for i in compiled.inner)]:
        if (refused := _refuse_ungated_transform(doc["steps"])) is not None:
            issues.append(refused["error"])
        if (said := save_rules.refusal(doc, agents)) is not None:
            issues.append(said)
        if not save_rules.browser_step_problems(doc, agents):  # its own sentences are the save rule's, said above
            before = ((await get_orchestration(doc["id"])) or {}).get("steps") if doc.get("id") else None
            if (refused := await _refuse_browser_steps(doc["steps"], before, wired=True, schema=doc.get("state_schema"),
                                                       agents=agents)) is not None:
                issues.append(refused["error"])
        if ws.kbs is not None:
            # A base this build makes is there by the time its steps are saved (F1224): made first, active.
            from types import SimpleNamespace

            kbs = {**ws.kbs, **{k["id"]: SimpleNamespace(name=k["name"], status="active")
                                for k in compiled.knowledge if not k["existing"]}}
            for s in doc["steps"]:
                if s.get("type") in ("retrieve", "ingest"):
                    issues += _knowledge_step_issues(s, kbs, False)
        reachable = {**doc, "steps": [s for s in doc["steps"] if not str(s.get("workflow_id") or "").startswith(MADE)]}
        issues += (await _workflows_called(reachable, agents))[1]
    return list(dict.fromkeys(issues))


#: What a changed step's line names, by the Plan field that changed.
#: (Keyword arguments, not a literal: `"type": "…"` in code reads as an event type to F854's event census.)
_FIELD_WORDS = dict(type="its type", name="its name", does="what it does", agent="its agent", tool="its tool",
                    prompt="its prompt", next="what follows it", branches="its branches", condition="its condition",
                    loop_over="what it loops over", body="its body", workflow="the workflow it runs",
                    config="its settings")


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


def plan_before_of(state: dict, prior: dict) -> dict | None:
    """The plan the person last saw, which a re-plan is held against (F1245): this run's last card, the newest they
    saw; else the one a revision was asked of (`plan_before`: the card on "Change something", or the parked plan
    CAP3's F1241 carries into the next turn). None when there is none: a first plan, or a question in between."""
    if prior.get("outcome") in ("review", "same") and isinstance(prior.get("plan"), dict):
        return prior["plan"]
    before = _obj(state.get(PLAN_BEFORE_KEY))
    if isinstance(before, dict) and isinstance(before.get("plan"), dict):
        return before["plan"]
    return None


def _revising(state: dict) -> dict:
    """Back to the planner with the card it revises kept as `plan_before` (F1245), its draft's digest and its run:
    what the next plan changed from it is said on the next card."""
    checked = checked_in(state)
    if isinstance(checked.get("plan"), dict):
        run_id, _ = _run()
        state[PLAN_BEFORE_KEY] = {"plan": checked["plan"], "draft": checked.get("draft"), "run_id": run_id}
    return {"outcome": "revise"}


def since_last(plan: O.Plan, before: dict | None) -> list[str]:
    """What a re-plan changed from the plan the person last saw, in code's words (F1245: turn 6's re-plan swapped the
    site it scrapes, unseen): a step added or removed, the tool a step calls, an input the step shows
    (`parts_words.shown_inputs`). A step is matched by its id, else by its name. Empty with no plan before."""
    from core.native_builder.parts_words import shown_inputs

    if not isinstance(before, dict) or not before.get("steps"):
        return []
    old = [s for s in before.get("steps") or [] if isinstance(s, dict) and s.get("type") != "end"]
    by_id = {s.get("id"): s for s in old}
    by_name = {str(s.get("name") or "").casefold(): s for s in old}
    lines, matched = [], set()
    for s in (x for x in plan.steps if x.type != "end"):
        was = by_id.get(s.id) or by_name.get(s.name.casefold())
        if was is None:
            lines.append(f'Added the step "{s.name}"')
            continue
        matched.add(id(was))
        if s.type == "tool" and was.get("tool") != s.tool:
            lines.append(f'"{s.name}" now uses `{s.tool}`, not `{was.get("tool")}`')
        then = shown_inputs((was.get("config") or {}).get("tool_arguments"))
        now = shown_inputs((s.config or {}).get("tool_arguments"))
        # Each value as code: a plan card never makes an outside address clickable (8a, 10-30), here the wrong site.
        def code(value: str) -> str:
            return f"`{value.replace('`', '')}`"
        for key in [*then, *(k for k in now if k not in then)]:
            if then.get(key) == now.get(key):
                continue
            moved = (f"{code(then[key])} → {code(now[key])}" if key in then and key in now
                     else f"now {code(now[key])}" if key in now else f"{code(then[key])} is gone")
            lines.append(f'"{s.name}": {key} {moved}')
    lines += [f'Removed the step "{s.get("name")}"' for s in old if id(s) not in matched]
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


#: The structure findings already sent back to the planner in this build (D340 D3): each once per plan, never again
#: after a question round's re-plan (TRI1); after that only the card says it.
_STRUCTURE_SAID_KEY = "_plan_structure_said"


async def submit_plan(args: dict, server_module: Any, events: list[dict]) -> dict:
    found = await examine(args, server_module)
    _, here = _run()
    said = list(here.shared_state.get(_STRUCTURE_SAID_KEY) or []) if here is not None else []
    new = [f for f in found.structure if f not in said]
    if not found.errors and new and here is not None:
        here.shared_state[_STRUCTURE_SAID_KEY] = [*said, *new]
        return {"error": "Before this goes to the person, a better shape (said once; send it again as it is to keep "
                         "yours):\n" + "\n".join(f"- {e}" for e in new)}
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
    # A plan carries no question (F1253): one written anyway goes on to the check, which asks it before any plan.
    questions = [q.strip() for q in (_obj(args) or {}).get("questions") or [] if isinstance(q, str) and q.strip()]
    return {"finish": "submit_plan", **draft.model_dump(exclude_none=True), **({"questions": questions}
                                                                               if questions else {})}


register_builder_tool(SUBMIT, submit_plan)


# ── the planner's own check of a draft ────────────────────────────────────────────────────────────────────────────

CHECK_DRAFT = {"type": "function", "function": {
    "name": "check_draft",
    "description": ("Check a draft plan before you submit it: what would come back (errors, names nothing here has, "
                    "what a policy blocks), a better structure where code can tell, a screen the plan rebuilds by "
                    "hand, and what a run would cost. Nothing is saved, asked or shown to the person."),
    "parameters": {"type": "object", "properties": {"draft": O.plan_schema()}, "required": ["draft"]},
}}


async def check_draft(args: dict, server_module: Any, events: list[dict]) -> dict:
    """examine's pure rules on a draft, with the census's imitation findings (GUIDE1's `imitation.findings`) and the
    structure rules (D340 D3): what `submit_plan` and `check_build` would say, in the planner's own turn. It writes
    nothing to the run (no sendback, no checked plan), shows no card, asks nothing and looks nothing up."""
    from core.native_builder import imitation

    draft = _obj(args.get("draft"))
    ws = await workspace(server_module)
    found = await examine(draft if isinstance(draft, dict) else {}, server_module, ws=ws)
    if found.plan is None:
        return {"ok": False, "errors": found.errors}
    copied = imitation.findings(found.plan, ws) if found.plan.kind == "create" else []
    return {"ok": not (found.errors or found.blocks or copied), "errors": found.errors,
            "missing": found.missing, "misnamed": found.misnamed, "blocks": found.blocks,
            "features_off": found.features, "structure": found.structure,
            "imitations": [f["say"] for f in copied], "estimate": found.plan.run_cost}


register_builder_tool(CHECK_DRAFT, check_draft)
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


#: The lead-in when a reply leaves the plan as the card above shows it (F1121).
SAME = "The plan above is the same."


def no_tool_line(provider: str, names: list[str]) -> str:
    """The one plain line for an API the build can't make a tool for here yet (F1121, SAI2's words)."""
    tools = " and ".join(f"`{n}`" for n in names)
    return (f"I can't build {tools} here yet: {provider} has an API, but I can't make a tool for it yet. Add an MCP "
            "server for it on the MCP servers screen and ask again, or say \"build without it\".")


async def check_build(args: dict, server_module: Any, events: list[dict]) -> dict:
    """See the module's docstring. `draft` is the planner's finish (E2's object); everything else is the run's."""
    from core.builder_connect import _read_options, prepare_connection
    from core.native_builder.render import gate_words as render_gate, render
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
    reply = _obj(state.get("human_response")) or {}
    reply = reply if isinstance(reply, dict) else {}
    filled = reply.get("form") if isinstance(reply.get("form"), dict) else {}
    if prior.get("outcome") == "ask" and prior.get("form") and filled:
        # A form's answers, each with its own question (F1253): what the planner reads, and what is never asked again.
        from core.native_builder.change_tools import form_answer

        asked += [{"question": f.get("label") or "", "answer": form_answer(filled.get(f.get("name")))}
                  for f in prior["form"] if isinstance(f, dict)]
    elif prior.get("outcome") == "ask" and (answered := reply.get("answer")):
        entry = {"question": prior.get("question") or "", "answer": str(answered)}
        if prior.get("asking_periods"):
            # Code's own question (F1210): the answer is a rule each run works out from its own date. It lives in
            # `asked` alone, so whatever carries the questions asked carries it, and it is never asked again.
            entry["means"] = periods.meanings(prior["asking_periods"], str(answered), state.get("person_timezone"))
        asked.append(entry)
    periods_said = [rule for a in asked if isinstance(a, dict) for rule in a.get("means") or []]
    base: dict[str, Any] = {"object": "plan", "plan": None, "review": "", "offers": [], "question": "", "say": "",
                            "issues": [], "checks": int(prior.get("checks") or 0) + 1, "asked": asked,
                            "research": prior.get("research") or [], "offered": prior.get("offered") or [],
                            "periods": periods_said}
    if finish in ("submit_plan", "ask_person") and not periods_said and not prior.get("asking_periods") and (
            q := periods.question(f"{state.get('user_message') or ''} {go_ahead_request(state) or ''}",
                                  state.get("person_timezone"))):
        # A period the person did not pin down is asked once, before any plan is shown, with its dates on their
        # clock (F1210): a planner picked one of two weeks without saying so.
        return {**base, "outcome": "ask", "question": q.text, "asking_periods": q.dump()}
    if finish == "ask_person" and isinstance(draft.get("form"), list) and draft["form"]:
        from core.native_builder.change_tools import FORM_LEAD, form_text

        return {**base, "outcome": "ask", "question": form_text(draft.get("question") or FORM_LEAD, draft["form"]),
                "form": draft["form"]}
    if finish == "ask_person":
        from core.native_builder.change_tools import asked_text

        return {**base, "outcome": "ask", "question": asked_text(draft.get("question") or "",
                                                                 list(draft.get("options") or []))}
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
    # The person's own "use a stronger model for X" (F966): kept while that agent is in the plan.
    stronger = list(dict.fromkeys([*(prior.get("stronger") or []),
                                   *((answer.get("stronger") or []) if answer.get("outcome") == "stronger" else [])]))
    base["stronger"] = stronger
    ws = await workspace(server_module)
    found = await examine(draft, server_module, ws=ws, state=state, without=without, alone=alone)
    plan = found.plan
    if plan is None:
        return {**base, "outcome": "said", "say": "I could not make a plan that holds together: "
                                                  + "; ".join(found.errors[:3])}
    base["without"] = without
    if plan.kind == "create" and (copied := imitation.findings(plan, ws)):
        # A screen rebuilt by hand, said from its census row (F1257 step 3): first, Evals (F1255).
        return {**base, "outcome": "said", "say": copied[0]["say"]}
    if not [s for s in plan.steps if s.type != "end"]:
        left = ", ".join(f"`{t}`" for t in without)
        return {**base, "outcome": "said", "say": f"Without {left} there is nothing left to build." if without
                else "That plan has no steps to build."}
    if plan.room:
        return {**base, "outcome": "said", "say": plan.room}
    asking, unsaid_said = await _asked_first(base, draft, plan.questions, ws, state)
    if asking is not None:
        return asking
    plan.questions = []  # each was asked, or answered before: a plan's card carries no open question (F1253)
    if waiting := [m for m in plan.mcp_servers if not m.connected]:
        # The person's own MCP server, by its URL (F1225): its tools are known only once it is connected, so its card
        # comes before any plan, and its "Connected …" plans again on them (Naveen, 10-29; the exception to F949).
        return await _connect_first(base, plan, waiting, events)
    if plan.kind == "update" and (held := one_writer.asked(await one_writer.holds_now(), _orch_id(plan, state) or "",
                                                           events)):
        return {**base, "outcome": "said", "say": held}  # one chat writes a workflow at a time (TABS2, F1063)
    # The board the person asked for is a shared board this workflow is given to work (F1214): the one the plan names,
    # or the one already here its steps name. One already here is not made again; its new agents post there.
    shared = {b.lower(): b for b in ws.shared_boards}
    text = " ".join([plan.title, plan.summary, *(f"{s.name} {s.does} {s.prompt or ''}" for s in plan.steps)]).lower()
    mentioned = [name for low, name in shared.items() if low in text]
    if not plan.boards and len(mentioned) == 1:
        plan.boards = [O.BoardPart(name=mentioned[0])]
    for b in plan.boards:
        b.existing = b.name.lower() in shared
        b.name = shared.get(b.name.lower(), b.name)
    board_notes = [f'Its cards go on the shared board "{b.name}" (on the Boards screen)'
                   + (", which this workflow is given to work" if b.existing else ", made with it, for it to work")
                   + ". Its new agents post there; an agent already here posts there when its instructions name it."
                   for b in plan.boards]
    findings = _obj(state.get(FINDINGS_KEY))
    looked = isinstance(findings, dict) and bool(findings.get("options"))
    # What code could name instead is the planner's, never research's (F950, F1043).
    to_research = [t for t in plan.missing if t not in found.misnamed]
    if to_research and not looked and not prior.get("research"):
        return {**base, "outcome": "research", "research": to_research, "needs": _needs(plan, to_research, state),
                "plan": plan.model_dump()}

    plan.models, model_issues, model_question = await pick_models(plan, stronger)
    if model_question:
        # A wish that could mean more than one model is asked, with the menu's names; never guessed (F966).
        return {**base, "outcome": "ask", "question": model_question}
    found.errors += model_issues

    catalog = (await _read_options()).get("catalog") or []
    offered = list(prior.get("offered") or [])
    no_way, connect, said, makes, later, after_yes = [], [], [], [], [], []
    no_tool: dict[str, list[str]] = {}
    for t in plan.tools:
        if t.status == "missing" and t.provider:
            # The research's verdict, with why (A6): no way in that it could find.
            no_way.append(t.name)
            said.append(f"`{t.name}`: {t.provider}: {(t.detail or 'no public way in that I could find').rstrip('.')}. "
                        "Connect an app or service that provides it, or build without it.")
        elif t.status == "missing":
            no_way.append(t.name)
            said.append(f"`{t.name}`: nothing in this workspace provides it" + (
                ", and the lookup found nobody who does" if looked else "")
                + ". Connect an app or service that provides it, or build without it.")
        elif t.status == "found" and t.via == "none":
            # An API the research read and could make no tool from: no spec it could use, an OAuth sign-in, a token
            # in the URL's path (F1121, Gmail, Calendar, Telegram's Bot API). Said once, by provider, below.
            no_way.append(t.name)
            no_tool.setdefault(t.provider or t.name, []).append(t.name)
        elif t.status == "found" and t.via == "http_tool" and not MAKES_HTTP_TOOLS:
            no_way.append(t.name)
            said.append(f"`{t.name}`: {t.provider} has a public API, but I can't make a tool for it here yet: build "
                        "without it, or connect a server that provides it.")
        elif t.status == "found" and t.via == "http_tool":
            # The build makes it after the yes, on the provider's own host, and asks for its key then (F944).
            makes.append(t)
        elif t.status == "found":
            # Plan first, connect after the yes (F949, Naveen 10-24): the review shows the plan alone, and its Connect
            # card comes after the yes, one per provider, before the save.
            later.append(t.name)
            app = _app_for(t.provider or "", catalog)
            if app is not None:
                t.via = "catalog_app"
                after_yes.append(f"`{t.name}`: after your yes, I'll ask you to connect {app.get('name')} "
                                 "(in the Apps catalog).")
            else:
                after_yes.append(f"`{t.name}`: after your yes, I'll ask you to connect {t.provider}'s own MCP "
                                 "server, under Apps and MCP servers.")
    said += [no_tool_line(provider, names) for provider, names in no_tool.items()]
    if found.errors:
        said.append("It can't be saved as it is: " + " ".join(found.errors[:3]))
    said += [f"{why}." for why in found.blocks.values()]
    said += found.notes
    said += unsaid_said
    # A reused agent's blocked tools, said too (SAI2's nit on point 1): on the card under "Blocked here", and here.
    said += [f"{render_gate(g)}." for g in plan.gates if g.decision == "deny" and g.tool not in found.blocks]
    buildable = not (no_way or connect or found.errors or found.blocks)
    prompts = prompts_for(plan, state) if plan.agents else {}
    if prompts:
        # Each board agent is told how to work its board, in code's words the card shows (F1254).
        prompts = boards_rule.with_board_lines(plan, prompts)
    if buildable and prompts is None:
        # The new agents' instructions are written before the review, so the person reads them and the yes covers
        # them (Naveen, 10-23, Q2 as revised). Not for a plan that cannot be built yet: no call is spent on it.
        return {**base, "outcome": "prompts", "plan": plan.model_dump(), "without": without, "offered": offered,
                "research": prior.get("research") or [],
                "for": {a.name: agent_fingerprint(a.model_dump()) for a in plan.agents}}
    offers = ["build"] if buildable else []
    offers += [f"without:{n}" for n in dict.fromkeys([*no_way, *connect, *later, *found.blocks])]
    # Only where a press changes what the run asks (F964, 2): it lifts this build's own confirm, or the Human step a
    # Tool step needs; where only an agent's or the workspace's policy asks, it would change nothing.
    gated = {g.tool for g in plan.gates if g.decision == "confirm"}
    lifts = {g.tool for g in plan.gates if g.decision == "confirm" and g.set_by == "build"}
    offers += [f"alone:{n}" for n in plan.approvals.asks_first if n not in no_way and n not in connect
               and (n in lifts or n not in gated)]
    # A stronger model where the planner said it helps, at its price: the person's choice, never made for them (F966).
    offers += [f"stronger:{n}" for n, p in plan.models.items() if p.stronger]
    # Not for an API the research read and could make no tool from (F1121): another look reads the same pages.
    if no_way and (not looked or any(t.status == "missing" and t.provider for t in plan.tools)):
        offers.append("find")
    # A feature that is off where the plan needs it: its switch, for one who may press it (F1203).
    offers += [f"turn_on:{f}" for f in found.features if _may_turn_on()]
    offers += ["change", "stop"]

    names = {i: a.get("name") or i for i, a in ws.agents.items()}
    # What this plan changed from the one the person last saw (F1245), on the card and the panel alike.
    plan.since_last = since_last(plan, plan_before_of(state, prior))
    review = render(plan, agent_names=names)
    if answer.get("outcome") == "replan" and answer.get("refused"):
        # A save refused after the yes (F1295): why it is asked again, in the save's own words.
        review = (f"Before I saved it, the build needed one change: {answer['refused']}. Here is the plan with it, "
                  "for one more yes.\n\n" + review)
    elif answer.get("outcome") == "replan":
        # The second review after connecting (F949): why it is asked again, before the plan on the real names.
        who = " and ".join(answer.get("connected") or []) or "it"
        review = (f"After connecting {who}, its tools are "
                  + ", ".join(f"`{n}`" for n in (answer.get("tools") or [])[:12])
                  + ": here is the plan on them, for one more yes.\n\n" + review)
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
    if periods_said:
        # What each period means, as the person said (F1210): every run works it out from its own date.
        review += "\n## Dates\n" + "\n".join(f"- {p.split('; never write')[0]}." for p in periods_said) + "\n"
    if after_yes:
        # What it asks after the yes, one card per provider (F949): never beside the plan.
        review += "\n## After your yes\n" + "\n".join(f"- {n}" for n in after_yes) + "\n"
    if makes:
        # What the person says yes to: each call, its host and its path, made only from this approved spec (F944).
        review += "\n## The tools it makes\n" + "\n".join(
            f"- `{t.name}`: {t.api['endpoint']['method']} {t.api['endpoint']['path']} on "
            f"{urlsplit(t.api['base_url']).hostname} ({t.provider}). "
            + ("It takes no key." if t.api.get("keyless") else "You give it your key on a card after your yes.")
            for t in makes if t.api) + "\n"
    out = {**base, "outcome": "review", "plan": plan.model_dump(), "prompts": prompts or {}, "review": review,
           "offers": offers, "say": " ".join([*said, *board_notes]), "issues": found.errors, "offered": offered,
           "research": prior.get("research") or [], "notes": board_notes, "changes": changes,
           "after_yes": after_yes,
           # The review's lead-in follows what the card offers: no "approve to build it" where it can't be built.
           "lead": LEAD_BUILD if buildable else LEAD_NOT_YET,
           "no_tool": [n for names in no_tool.values() for n in names],
           "same_say": f"{SAME} {' '.join(said)}".strip()}
    out["digest"] = digest(reviewed_of(out))
    if not buildable and prior.get("outcome") in ("review", "same") and prior.get("digest") == out["digest"]:
        # The same plan as the card above, and still not buildable (F1121): what stands in the way, at its own
        # question, never the plan drawn again (bld-01/02/07 went round until "we went back and forth").
        out["outcome"], out["review"] = "same", out["same_say"]
    await _card(out)
    return out


#: The form's line when code asks about addresses it was not given (F1253).
UNSAID_LEAD = "Before I show you the plan, check what I filled in that you haven't told me:"
#: What the run was given, by state key: the person's words and answers, what is open, the workspace's own profile,
#: what research read, a pasted export and the recordings. Never a model's own reading of the message (`request`,
#: `turn`), and never the plan or its check, which hold the guess itself.
GIVEN_KEYS = ("user_message", "chat_history", "org_profile", "plan_context", "existing_orch", "plan_findings",
              "plan_research", "plan_lookup", "findings", "prior_findings", "export_source", "knowledge_steps",
              "browser_steps", "current_page", "human_response", "review_feedback", "connect_answer", "plan_answer",
              "change_clarify", "change_heard",
              # What the planner's own research found (D340 D2): an address it read there was given, not guessed.
              "plan_answers")


async def _given(state: dict, ws: Workspace, asked: list[dict]) -> list[Any]:
    """Everything the run was given that an address may come from (F1253): its state's (`GIVEN_KEYS`), the answers
    to the request's questions (the seam's, and the check's `asked`), and the workspace (its agents, workflows, tools
    and connected servers' URLs)."""
    from core.native_builder.change_tools import _with_answers
    from core.store.resources import load_mcp_servers

    try:
        servers = [s.get("url") for s in await load_mcp_servers() if isinstance(s, dict) and s.get("url")]
    except Exception as exc:  # noqa: BLE001 — no servers read is no server's host given, never a failed build
        _log.warning("the build's MCP servers were not read: %s", exc)
        servers = []
    answers = [a.get("answer") for a in _with_answers(state) if a.get("answer")]
    answers += [a.get("answer") for a in asked if isinstance(a, dict) and a.get("answer")]
    return [*(state.get(k) for k in GIVEN_KEYS), *answers, list(ws.agents.values()), list(ws.workflows.values()),
            ws.tools, servers]


async def _asked_first(base: dict, draft: dict, carried: list[str], ws: Workspace,
                        state: dict) -> tuple[dict | None, list[str]]:
    """What only the person can say, asked before any plan, research or Connect card (F1253), in one form, one round
    of the request's seam (F1226): each address the plan uses that the run was never given, the planner's value
    filled in, and a question the plan carries anyway (`carried`, as `examine` kept it). Returns the ask, or None and, once the request has asked
    enough, the review's words for each address it goes on with."""
    from core.native_builder import addresses
    from core.native_builder.change_tools import (_ASKS_WAY_IN, FORM_LEAD, FORM_MAX, QUESTIONS, asked_before,
                                                  form_text, note_question, questions_asked)

    _, run = _run()
    unsaid = [a for a in addresses.unsaid(draft, await _given(state, ws, base.get("asked") or []))
              if asked_before(state, "what", a.subject, a.label()) is None]
    # The plan's own questions as `examine` kept them: one that asked for a key was cut there (F1093).
    # How a service is reached is looked up, never asked, as `ask_person` refuses it.
    questions = [q for q in carried if isinstance(q, str) and q.strip() and not _ASKS_WAY_IN.search(q)
                 and asked_before(state, "what", "", q) is None]
    if not (unsaid or questions):
        return None, []
    if questions_asked(state, run) >= QUESTIONS:
        return None, [a.said() for a in unsaid]
    form = []
    for a in unsaid[:FORM_MAX]:
        form.append({"name": f"q{len(form) + 1}", "type": addresses.FIELD_TYPE[a.kind], "label": a.label(),
                     "value": a.value})
        note_question(state, "what", a.subject, a.label(), field=form[-1]["name"])
    for q in questions[:max(0, FORM_MAX - len(form))]:
        form.append({"name": f"q{len(form) + 1}", "type": "string", "label": q.strip()})
        note_question(state, "what", "", q.strip(), field=form[-1]["name"])
    lead = UNSAID_LEAD if unsaid else FORM_LEAD
    return {**base, "outcome": "ask", "question": form_text(lead, form), "form": form}, []


async def _connect_first(base: dict, plan: O.Plan, waiting: list[O.McpServerPart], events: list[dict]) -> dict:
    """Each of the person's own MCP servers not connected yet, on its Connect card, and no plan beside it: what a
    plan does with its tools is drawn once they are known (F1225). Nothing is offered to build."""
    from core.builder_connect import prepare_connection
    from core.native_builder.render import connect_first_lead, connect_first_text
    from core.product_tools.approved import digest

    for m in waiting:
        await prepare_connection({"kind": "mcp_server", "url": m.url, "server_name": m.name,
                                  "title": f"Connect {m.name}",
                                  "why": f"This build uses {m.name}'s tools: I plan it on them once it is connected."},
                                 None, events)
    out = {**base, "outcome": "review", "plan": plan.model_dump(), "review": connect_first_text(waiting),
           "offers": ["change", "stop"], "say": "", "lead": connect_first_lead(waiting), "connect_first": [m.name for m in waiting],
           "same_say": connect_first_text(waiting)}
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

#: A short lead-in before it ("ok, build without them", "please build without Telegram") is still the answer (F1121).
_WITHOUT = re.compile(r"^\s*(?:(?:ok|okay|yes|yeah|sure|fine|alright|then|please|just)[,.!]?\s+)*"
                      r"build\s+(?:it\s+)?without\s+(?P<what>.+?)\s*[.!]?\s*$", re.I)
#: The card's "Let it act without asking" (`plan.ts` `aloneWords`): the person's own act, never the planner's.
_ALONE = re.compile(r"^\s*let\s+(?P<what>.+?)\s+act\s+without\s+asking\s*[.!]?\s*$", re.I)
#: The Connect card's own words when it is done (`onConnected`, both panels): "Connected X.", "Saved the H for T.",
#: and an app's own sign-in, "Signed in to X." (SAI2's review, 8).
#: The card's "Use a stronger model for X" (`plan.ts` `strongerWords`, F966): the person's own act.
_STRONGER = re.compile(r"^\s*use\s+a\s+stronger\s+model\s+for\s+(?P<who>.+?)\s*[.!]?\s*$", re.I)
_CONNECTED = re.compile(r"^\s*(?:connected\b|saved the\b|signed in to\b)", re.I)
#: The card's "Turn on Boards" (`plan.ts` `turnOnWords`, F1203): the person's own press, only for what it offered.
_TURN_ON = re.compile(r"^\s*turn\s+on\s+(?P<what>boards|knowledge)\s*[.!]?\s*$", re.I)


async def read_plan_answer(args: dict, server_module: Any, events: list[dict]) -> dict:
    """What a review answer that is not F754's yes asks for: `without` (the tools to leave out, by a tool's name or
    its provider's), `connected`, `alone` and `stronger` (the card's own presses, only for what it offered), or `revise`
    (anything else, which goes back to the planner)."""
    state = _state()
    answer = str((_obj(state.get("review_feedback")) or {}).get("feedback") or "")
    if _CONNECTED.match(answer):
        return {"outcome": "connected"}
    on = _TURN_ON.match(answer)
    if on and f"turn_on:{on.group('what').lower()}" in (checked_in(state).get("offers") or []):
        # Turned on as its switch in Settings does, for this person (an admin's), then planned again on it, as after a
        # connection (F1203).
        from core.builder_access import current_caller
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.errors import ToolRefusal
        from core.product_tools.roles import Role

        areas.ensure_loaded()
        try:
            await registry.invoke("turn_on_feature", current_caller() or Caller(role=Role.OWNER, via="assistant"),
                                  {"feature": on.group("what").lower()})
        except ToolRefusal:
            return _revising(state)
        return {"outcome": "connected", "turned_on": on.group("what").lower()}
    let = _ALONE.match(answer)
    if let:
        named = let.group("what").strip(" `'\"").lower()
        checked = checked_in(state)
        asks = ((checked.get("plan") or {}).get("approvals") or {}).get("asks_first") or []
        picked = [t for t in asks if t.lower() == named and f"alone:{t}" in (checked.get("offers") or [])]
        if picked:
            return {"outcome": "alone", "alone": picked}
    up = _STRONGER.match(answer)
    if up:
        named = up.group("who").strip(" `'\"").lower()
        offered = checked_in(state).get("offers") or []
        picked = [o.split(":", 1)[1] for o in offered
                  if o.startswith("stronger:") and o.split(":", 1)[1].lower() == named]
        if picked:
            return {"outcome": "stronger", "stronger": picked}
    checked = checked_in(state)
    no_tool = [str(n) for n in checked.get("no_tool") or []]
    from core.native_builder.review import is_yes

    if no_tool and "build" not in (checked.get("offers") or []) and is_yes(answer):
        # A yes to a plan that can't be built: what stands in the way, again (F1121). It never reaches the approval
        # from here: `step_plan_same` answers into this step, past no check of the plan (F778's binding).
        return await _no_tool_unless_here(no_tool, checked, server_module)
    m = _WITHOUT.match(answer)
    if m:
        tools = (checked.get("plan") or {}).get("tools") or []
        asked = [w.strip(" `'\".").lower() for w in re.split(r",|\s+and\s+", m.group("what")) if w.strip(" `'\".")]
        if no_tool and set(asked) <= _THOSE:
            # "Build without it", as the line about what the build can't make says it (F1121).
            return {"outcome": "without", "without": no_tool}
        offered = checked_in(state).get("offers") or []
        # A tool with no way in, or one a policy blocks (offered as "Build without X", F964).
        picked = [t["name"] for t in tools if (t.get("status") != "present" or f"without:{t['name']}" in offered)
                  and (t["name"].lower() in asked or str(t.get("provider") or "").lower() in asked)]
        if picked:
            return {"outcome": "without", "without": picked}
    if no_tool and _ADD_IT.search(answer) and _names_one(answer, no_tool, checked):
        # Asked to add an integration the build can't make (bld-01/02/07's every round): the line again, at its own
        # question, with no planner call and no plan drawn again (F1121, P5's read).
        return await _no_tool_unless_here(no_tool, checked, server_module)
    return _revising(state)


async def _no_tool_unless_here(no_tool: list[str], checked: dict, server_module: Any) -> dict:
    """The line again, unless one of those providers is here now (SAI2's review of F1121): the line says to add an MCP
    server and ask again, so what the workspace provides is read again first (a server named for the provider, or
    tools named for it), and one now here is a re-plan on its real tools, as the Connect card's "Connected …" is."""
    from core.native_builder.build_save import _provider_tools, _servers_of
    from core.store.resources import load_tools

    tools = {t["name"]: t for t in (checked.get("plan") or {}).get("tools") or [] if isinstance(t, dict)}
    servers = _servers_of(server_module)
    current = {t.get("name"): t for t in await load_tools()}
    for name in no_tool:
        provider = str((tools.get(name) or {}).get("provider") or name)
        if _provider_tools(provider, servers, current):
            return {"outcome": "connected"}
    return {"outcome": "no_tool"}


#: What "build without it" names: the tools the line about what can't be made named (F1121).
_THOSE = frozenset({"it", "them", "that", "those", "this", "these", "that one", "those ones"})
#: An ask to add, connect or set up an integration (bld-07: "I'll need the Telegram bot token … can be configured").
_ADD_IT = re.compile(r"\b(?:add|connect|set ?up|install|enable|integrate|hook (?:it )?up|configur\w*|link)\b",
                     re.IGNORECASE)


def _names_one(answer: str, no_tool: list[str], checked: dict) -> bool:
    """Whether `answer` names one of the tools the build can't make, or its provider."""
    from core.builder_research import _words

    said = set(_words(answer.replace("_", " ")))
    tools = {t["name"]: t for t in (checked.get("plan") or {}).get("tools") or [] if isinstance(t, dict)}
    for name in no_tool:
        provider = str((tools.get(name) or {}).get("provider") or "")
        if set(_words(name.replace("_", " "))) <= said or (provider and set(_words(provider)) <= said):
            return True
    return False


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
