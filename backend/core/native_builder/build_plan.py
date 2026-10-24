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
from urllib.parse import urlsplit
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
        # A schedule's times are on this clock unless the person names another; never ask them for it (F948).
        "person_timezone": state.get("person_timezone") or "UTC",
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
    #: Tools a policy blocks where the plan uses them, with why: the person's to change, never the planner's (F964).
    blocks: dict[str, str] = field(default_factory=dict)
    #: What code changed in the plan and the card says (F944: a tool name already taken, made as `_2`).
    notes: list[str] = field(default_factory=list)


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
    errors, _ = O.check_structure(plan, ws.inventory())
    if base is not None and plan.kind == "update":
        # A step no step leads to in the workflow as it is now is not this change's to fix (SAI2's review, 23).
        before, _ = O.check_structure(base, ws.inventory())
        errors = [e for e in errors if not ("no step leads to it" in e and e in before)]
    errors += await _rules(plan, ws, state)
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
    filled = await O.fill(plan, server_module, found=_found(plan, state, renamed), agents=ws.agents)
    issues = [i for i in await dry_issues(filled, server_module, ws, state) if not _not_the_planners(i)]
    return Examined(filled, [_hinted(i) for i in issues], missing=list(filled.missing), issues=issues,
                    blocks=blocked(filled, state), notes=room_notes)


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
    errors += _api_names(plan, state)
    return errors


def _api_names(plan: O.Plan, state: dict) -> list[str]:
    """A tool that names a provider whose API was read, but none of its calls: the plan calls a read API's endpoint by
    its tool's name, so code makes exactly that call (A7, F944)."""
    from core.builder_research import _words

    findings = _obj(state.get(FINDINGS_KEY))
    errors = []
    for option in (findings or {}).get("options") or [] if isinstance(findings, dict) else []:
        spec = option.get("api") if isinstance(option.get("api"), dict) else None
        tools = [e.get("tool") for e in (spec or {}).get("endpoints") or [] if e.get("tool")]
        if not tools:
            continue
        provider = set(_words(str(option.get("provider") or "")))
        for name in O.tool_names(plan):
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
            found[name] = O.ToolStatus(name=name, status=status, provider=provider, via=via,
                                       detail=str(option.get("detail") or "") or None, api=api)
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
    no_way, connect, said, leads, makes = [], [], [], [], []
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
            # A lead (A6): an API whose calls the research has not read yet; "Find who can" reads them.
            no_way.append(t.name)
            leads.append(t.name)
            said.append(f"`{t.name}`: {t.provider} has a public API, but I haven't read how to call it yet: find "
                        "who can, to read its docs, or build without it.")
        elif t.status == "found" and t.via == "http_tool" and not MAKES_HTTP_TOOLS:
            no_way.append(t.name)
            said.append(f"`{t.name}`: {t.provider} has a public API, but I can't make a tool for it here yet: build "
                        "without it, or connect a server that provides it.")
        elif t.status == "found" and t.via == "http_tool":
            # The build makes it after the yes, on the provider's own host, and asks for its key then (F944).
            makes.append(t)
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
    said += [f"{why}." for why in found.blocks.values()]
    said += found.notes
    # A reused agent's blocked tools, said too (SAI2's nit on point 1): on the card under "Blocked here", and here.
    said += [f"{render_gate(g)}." for g in plan.gates if g.decision == "deny" and g.tool not in found.blocks]
    buildable = not (no_way or connect or found.errors or found.blocks)
    prompts = prompts_for(plan, state) if plan.agents else {}
    if buildable and prompts is None:
        # The new agents' instructions are written before the review, so the person reads them and the yes covers
        # them (Naveen, 10-23, Q2 as revised). Not for a plan that cannot be built yet: no call is spent on it.
        return {**base, "outcome": "prompts", "plan": plan.model_dump(), "without": without, "offered": offered,
                "research": prior.get("research") or [],
                "for": {a.name: agent_fingerprint(a.model_dump()) for a in plan.agents}}
    offers = ["build"] if buildable else []
    offers += [f"without:{n}" for n in dict.fromkeys([*no_way, *connect, *found.blocks])]
    # Only where a press changes what the run asks (F964, 2): it lifts this build's own confirm, or the Human step a
    # Tool step needs; where only an agent's or the workspace's policy asks, it would change nothing.
    gated = {g.tool for g in plan.gates if g.decision == "confirm"}
    lifts = {g.tool for g in plan.gates if g.decision == "confirm" and g.set_by == "build"}
    offers += [f"alone:{n}" for n in plan.approvals.asks_first if n not in no_way and n not in connect
               and (n in lifts or n not in gated)]
    if no_way and (not looked or leads or any(t.status == "missing" and t.provider for t in plan.tools)):
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
    if makes:
        # What the person says yes to: each call, its host and its path, made only from this approved spec (F944).
        review += "\n## The tools it makes\n" + "\n".join(
            f"- `{t.name}`: {t.api['endpoint']['method']} {t.api['endpoint']['path']} on "
            f"{urlsplit(t.api['base_url']).hostname} ({t.provider}). You give it your key on a card after your yes."
            for t in makes if t.api) + "\n"
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
        checked = checked_in(state)
        asks = ((checked.get("plan") or {}).get("approvals") or {}).get("asks_first") or []
        picked = [t for t in asks if t.lower() == named and f"alone:{t}" in (checked.get("offers") or [])]
        if picked:
            return {"outcome": "alone", "alone": picked}
    m = _WITHOUT.match(answer)
    if m:
        tools = (checked_in(state).get("plan") or {}).get("tools") or []
        asked = [w.strip(" `'\"").lower() for w in re.split(r",|\s+and\s+", m.group("what")) if w.strip(" `'\"")]
        offered = checked_in(state).get("offers") or []
        # A tool with no way in, or one a policy blocks (offered as "Build without X", F964).
        picked = [t["name"] for t in tools if (t.get("status") != "present" or f"without:{t['name']}" in offered)
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
