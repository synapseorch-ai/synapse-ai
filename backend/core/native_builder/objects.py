"""Synapse AI's typed objects: one source for the backend, the panel and the tests (SAI2, D300; P1, F774).

A model writes language and judgement once, into one of these; code does everything that is a fact (DESIGN.md,
"Principle"). So each object has two halves, kept apart:

* **what a model writes**, validated here: `Request` (the quick model's classify), `PlanDraft` (the planner's
  `submit_plan`), `Change` (the change planner's `submit_change`);
* **what code fills in**, never accepted from a model: a `Plan`'s tool status, approvals, connections, what is
  missing and the plan room (`fill`), and a `Turn`, which code alone writes.

`PlanDraft.model_json_schema()` is the planner's tool schema; P0's probe measured eight models filling its first form
(`drafts/sai2/probe/plan_schema.py`; every model ≥ 14/15). The `workflow` step type is the engine's own now (E3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.builder_actions import ACTIONS
from core.models_orchestration import StepType

#: The engine's step types, as a Plan names them.
STEP_TYPES = tuple(t.value for t in StepType)

_FIVE_FIELDS = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


# ── what a model writes: the Plan ─────────────────────────────────────────────────────────────────────────────────

class _Written(BaseModel):
    """A model's object: keys it adds that are not ours are dropped, never stored."""

    model_config = ConfigDict(extra="ignore")


class Step(_Written):
    id: str = Field(description="Short snake_case id, unique in the plan, e.g. draft_reply")
    type: Literal[STEP_TYPES] = Field(description="One of the engine's step types")  # type: ignore[valid-type]
    name: str = Field(description="A short name a person reads on the canvas")
    does: str = Field(description="One sentence: what this step does")
    agent: str | None = Field(None, description="agent/llm steps: a new agent's name from `agents`, or an existing "
                                                "agent's id")
    tool: str | None = Field(None, description="tool steps: the one tool it calls, by its exact name")
    prompt: str | None = Field(None, description="agent/llm steps: the instruction; human steps: the question to "
                                                 "the person; workflow steps: what the workflow is given")
    next: str | None = Field(None, description='The step id that follows, or "end"')
    branches: dict[str, str] | None = Field(None, description='if_else: {"true": id, "false": id}; '
                                                              "switch/evaluator: {label: id}")
    condition: str | None = Field(None, description="if_else: the condition in words or as an expression")
    loop_over: str | None = Field(None, description="loop steps: what list it goes through")
    body: list[str] | None = Field(None, description="loop: the step ids run for each item; parallel: the branch "
                                                     "step ids")
    workflow: str | None = Field(None, description="workflow steps: an inner workflow's title from "
                                                   "`inner_workflows`, or an existing workflow's id")
    config: dict[str, Any] | None = Field(None, description="Optional engine settings for this step, by their "
                                                            "names (e.g. human_fields, loop_max_iterations)")


class NewAgent(_Written):
    name: str
    type: Literal["agent", "team"] = Field("agent", description="agent, or team: a lead with members")
    purpose: str = Field(description="One or two sentences: what it is for")
    tools: list[str] = Field(default_factory=list, description="Exact tool names it needs")
    members: list[str] = Field(default_factory=list, description="team: the names or ids of its members")


class Schedule(_Written):
    cron: str = Field(description="Five-field cron, e.g. 0 7 * * *")
    timezone: str | None = None
    says: str = Field(description="The schedule in words, e.g. every day at 7:00")
    input: str | None = Field(None, description="What each scheduled run starts with: the task, with what it is "
                                                "about (F155)")


class InnerWorkflow(_Written):
    title: str
    does: str
    steps: list[Step]


class Caps(_Written):
    credits: float | None = Field(None, description="The most credits one run may use")
    dollars: float | None = Field(None, description="The most model dollars one run may spend")


class BoardPart(_Written):
    name: str
    purpose: str = ""


class Edit(_Written):
    """An update Plan's change to an existing workflow, by step id."""

    op: Literal["add_step", "update_step", "remove_step", "set_meta"]
    step_id: str | None = Field(None, description="update_step / remove_step: the step's id")
    after: str | None = Field(None, description="add_step: the step id it goes after")
    step: Step | None = Field(None, description="add_step: the new step")
    fields: dict[str, Any] = Field(default_factory=dict, description="update_step / set_meta: what changes")


class PlanDraft(_Written):
    """What the planner writes (`submit_plan`)."""

    kind: Literal["create", "update"]
    title: str
    summary: str = Field(description="One or two sentences in the person's words")
    steps: list[Step] = Field(default_factory=list)
    edits: list[Edit] = Field(default_factory=list, description="update: the changes, by step id")
    agents: list[NewAgent] = Field(default_factory=list, description="New agents only")
    reuse_agents: list[str] = Field(default_factory=list, description="Ids of existing agents the steps use")
    schedule: Schedule | None = None
    inner_workflows: list[InnerWorkflow] = Field(default_factory=list, description="Workflows this one runs, "
                                                                                    "saved first")
    caps: Caps | None = None
    boards: list[BoardPart] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, description="Only what the person alone knows")
    not_carried_over: list[str] = Field(default_factory=list, description="A pasted export: what it had that the "
                                                                          "plan does not carry over")
    settings: dict[str, Any] | None = Field(None, description="Optional workflow settings by their engine names: "
                                                              "state_schema, result_key")


# ── what code fills in ─────────────────────────────────────────────────────────────────────────────────────────────

class ToolStatus(BaseModel):
    name: str
    #: present: a run could call it now; found: research found who provides it (connect first); missing: neither.
    status: Literal["present", "found", "missing"]
    provider: str | None = None
    via: Literal["catalog_app", "mcp_server", "http_tool", "none"] = "none"


class Approvals(BaseModel):
    """Every tool the plan uses that changes something outside: each asks first unless the person moved it."""

    asks_first: list[str] = Field(default_factory=list)
    acts_without_asking: list[str] = Field(default_factory=list)


class Plan(PlanDraft):
    """A PlanDraft with what code knows about it. Only `from_draft` and `fill` make one from a model's output."""

    model_config = ConfigDict(extra="ignore")

    tools: list[ToolStatus] = Field(default_factory=list)
    approvals: Approvals = Field(default_factory=Approvals)
    connections_needed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: Why the plan does not fit the workspace's plan (cloud's F154), or None.
    room: str | None = None

    @classmethod
    def from_draft(cls, raw: dict[str, Any] | PlanDraft) -> "Plan":
        """A model's object as a Plan: read as a PlanDraft first, so a code field it wrote is dropped."""
        draft = raw if isinstance(raw, PlanDraft) and not isinstance(raw, Plan) else PlanDraft.model_validate(
            raw if isinstance(raw, dict) else raw.model_dump())
        return cls.model_validate(draft.model_dump())


def plan_schema() -> dict[str, Any]:
    """The planner's tool schema: what a model writes, and nothing code fills in."""
    return PlanDraft.model_json_schema()


# ── a request, a change, a turn ───────────────────────────────────────────────────────────────────────────────────

class Request(_Written):
    """The quick model's classify (`classify_request`): four labels, and what code needs to override one."""

    kind: Literal["answer", "change", "build", "research"]
    providers: list[str] = Field(default_factory=list, description="Outside services the message names")
    also: str | None = Field(None, description="A second request in the same message, in its words, if any")
    language: str | None = Field(None, description="The language the person wrote in, e.g. en, hi")


#: The write ops a change may hold, beside the button acts (`ACTIONS`). Closed: a change with no op that fits is
#: answered "can't" with the nearest thing that works (P0's finding 1). P2 owns their meaning and may extend it.
WRITE_OPS = ("update_workflow", "rename_workflow", "delete_workflow", "set_schedule", "rename_schedule",
             "delete_schedule", "set_agent_model", "set_agent_tools", "set_agent_skills", "set_agent_instructions",
             "rename_agent", "delete_agent", "set_tool_policy")
CHANGE_OPS = (*ACTIONS, *WRITE_OPS)


class Target(_Written):
    kind: Literal["workflow", "agent", "schedule", "tool", "board", "knowledge_base", "run", "card"]
    id: str | None = Field(None, description="Its id when known; else its name")
    name: str | None = None


class ChangeOp(_Written):
    op: Literal[CHANGE_OPS]  # type: ignore[valid-type]
    target: Target
    fields: dict[str, Any] = Field(default_factory=dict)
    says: str = Field("", description="The change in the person's words")

    @property
    def is_act(self) -> bool:
        """A button act: it goes on a card the person presses, never through the plan review (F755)."""
        return self.op in ACTIONS


class Change(_Written):
    """What the change planner writes (`submit_change`)."""

    ops: list[ChangeOp] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @property
    def has_writes(self) -> bool:
        return any(not op.is_act for op in self.ops)


class Turn(BaseModel):
    """Code's reading of a message before any model (`read_turn`): which route, and what it resolved."""

    route: Literal["pick", "card_label", "plan_answer", "next_request", "classify"]
    picked: str | None = None
    card: str | None = None
    target: dict[str, Any] | None = None


# ── the checks ────────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Inventory:
    """The workspace as a check reads it: agents and workflows by id → name, and its tool names."""

    agents: dict[str, str] = field(default_factory=dict)
    workflows: dict[str, str] = field(default_factory=dict)
    tools: set[str] = field(default_factory=set)


def read(raw: dict[str, Any]) -> tuple[Plan | None, list[str]]:
    """A model's `submit_plan` arguments as a Plan, or the errors to send back for a retry (one line each)."""
    try:
        return Plan.from_draft(raw), []
    except ValidationError as e:
        return None, [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:8]]


def _targets(step: Step) -> list[str]:
    return ([step.next] if step.next else []) + list((step.branches or {}).values()) + list(step.body or [])


def tool_names(plan: PlanDraft) -> list[str]:
    """Every tool the plan names: its steps', its inner workflows' and its new agents', in order, once each."""
    names: list[str] = []
    for s in [*plan.steps, *(x for w in plan.inner_workflows for x in w.steps)]:
        if s.tool:
            names.append(s.tool)
    for a in plan.agents:
        names += a.tools
    return list(dict.fromkeys(n for n in names if n))


def settled(steps: list[Step]) -> list[Step]:
    """A Plan's steps with what it leaves unsaid decided, as code can: models list steps in order and leave `next`
    out (P0's plans, often), and a list is an order.

    * A step with no `next` that does not branch and is not inside a Loop or a Parallel goes to the next step in
      the list that is not inside one either — or ends the run when it is the last (`"end"`).
    * A Parallel with no join joins at the merge all its branches lead to, or else as any step would.
    * A container's body steps go nowhere of their own (F124), and an End step goes nowhere at all.
    """
    by_id = {s.id: s for s in steps}
    bodies = {b for s in steps if s.type in ("loop", "parallel") for b in s.body or []}
    out: list[Step] = []
    for i, s in enumerate(steps):
        if s.next is None and s.type != "end" and s.id not in bodies and not (
                s.branches and s.type in ("if_else", "switch", "evaluator")):
            join = None
            if s.type == "parallel":
                joins = {by_id[b].next for b in s.body or [] if b in by_id}
                if len(joins) == 1 and (only := next(iter(joins))) in by_id and by_id[only].type == "merge":
                    join = only
            if join is None:
                join = next((x.id for x in steps[i + 1:] if x.id not in bodies), "end")
            s = s.model_copy(update={"next": join})
        out.append(s)
    return out


def _unreached(steps: list[Step], where: str) -> list[str]:
    """Steps no path from the first one reaches: a plan whose parts are not joined up runs only its first part."""
    steps = settled(steps)
    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    todo = [steps[0].id] if steps else []
    while todo:
        sid = todo.pop()
        if sid in seen or sid not in by_id:
            continue
        seen.add(sid)
        todo += _targets(by_id[sid])
    return [f"{where}step {s.id}: no step leads to it, so it never runs: lead to it from another step, or drop it"
            for s in steps if s.id not in seen and s.type != "end"]


def check_structure(plan: PlanDraft, inventory: Inventory) -> tuple[list[str], list[str]]:
    """(errors, notes). Errors go back to the model for a retry; notes are facts code fills in (a tool nothing
    provides is a note, not an error: finding who does is the lookup's job, not the model's)."""
    errors: list[str] = []
    notes: list[str] = []
    ids = [s.id for s in plan.steps]
    if plan.kind == "create" and not plan.steps:
        errors.append("steps: a new workflow needs at least one step")
    if plan.kind == "update" and not (plan.edits or plan.steps):
        errors.append("edits: an update names at least one change")
    if len(set(ids)) != len(ids):
        errors.append("steps: two steps share an id")
    known = set(ids) | {"end"}
    agents = {a.name for a in plan.agents} | set(inventory.agents) | set(plan.reuse_agents)
    for s in plan.steps:
        for t in _targets(s):
            if t not in known:
                errors.append(f"step {s.id}: goes to '{t}', which is not a step id")
        if s.type == "agent" and not s.agent:
            errors.append(f"step {s.id}: an agent step names its agent")
        if s.agent and s.agent not in agents:
            errors.append(f"step {s.id}: agent '{s.agent}' is neither new in `agents` nor an existing id")
        if s.type == "workflow" and not s.workflow:
            errors.append(f"step {s.id}: a workflow step names the workflow it runs")
        if s.type == "tool" and not s.tool:
            errors.append(f"step {s.id}: a tool step names its tool")
        if s.type == "if_else" and not (s.branches and {"true", "false"} <= set(s.branches)):
            errors.append(f"step {s.id}: if_else needs branches true and false")
        if s.type in ("switch", "evaluator") and not s.branches:
            errors.append(f"step {s.id}: {s.type} needs branches")
        if s.type == "loop" and not s.body:
            errors.append(f"step {s.id}: a loop names its body steps")
        if s.type == "human" and not s.prompt:
            errors.append(f"step {s.id}: a human step says what it asks")
        if s.branches and s.type not in ("if_else", "switch", "evaluator"):
            errors.append(f"step {s.id}: a {s.type} step cannot branch; branch in an if_else, a switch or an "
                          "evaluator step after it")
    errors += _unreached(plan.steps, "")
    flows = {w.title for w in plan.inner_workflows} | set(inventory.workflows)
    for s in plan.steps:
        if s.workflow and s.workflow not in flows:
            errors.append(f"step {s.id}: workflow '{s.workflow}' is neither in `inner_workflows` nor an existing id")
    used = {s.workflow for s in plan.steps if s.workflow}
    for w in plan.inner_workflows:
        if w.title not in used:
            errors.append(f"inner workflow '{w.title}' is never run: add a workflow step that runs it, or drop it")
        inner = {x.id for x in w.steps} | {"end"}
        errors += _unreached(w.steps, f"inner workflow '{w.title}' ")
        for x in w.steps:
            for t in _targets(x):
                if t not in inner:
                    errors.append(f"inner workflow '{w.title}' step {x.id}: goes to '{t}', which is not one of its "
                                  "step ids")
    for name in tool_names(plan):
        if name not in inventory.tools:
            notes.append(f"missing tool: {name}")
    if plan.schedule and not _FIVE_FIELDS.match(plan.schedule.cron.strip()):
        errors.append(f"schedule.cron: '{plan.schedule.cron}' is not five fields")
    return errors, sorted(set(notes))


async def fill(plan: Plan, server_module: Any, *, found: dict[str, ToolStatus] | None = None) -> Plan:
    """What code knows about a plan, written onto it (a copy): each tool's status, its approvals, what is missing,
    the connections it needs, and whether it fits the workspace's plan.

    * **Tool status** is the run's own lookup (`builder_guard`'s resolver, so "present" means a run could call it);
      a tool research found (`found`) needs its connection first; anything else is missing (F489: it is never
      saved as a step's tool).
    * **Approvals**: every present tool that changes something outside asks first (TAG1's tags, as
      `builder_guard.unapproved` reads them), unless the person already moved it to act without asking — a choice
      kept across fills while the tool is still in the plan.
    * **Plan room** is the host's check (cloud's F154; self-hosted has none).
    """
    from core import builder_guard
    from core.builder_tools import _room_check
    from core.routes.tools import load_custom_tools

    out = plan.model_copy(deep=True)
    exists = await builder_guard._resolver(server_module)
    custom = await load_custom_tools()
    statuses: list[ToolStatus] = []
    for name in tool_names(out):
        if await exists(name):
            statuses.append(ToolStatus(name=name, status="present"))
        elif found and name in found:
            statuses.append(found[name].model_copy(update={"name": name, "status": "found"}))
        else:
            statuses.append(ToolStatus(name=name, status="missing"))
    out.tools = statuses
    out.missing = [t.name for t in statuses if t.status == "missing"]
    out.connections_needed = list(dict.fromkeys(t.provider or t.name for t in statuses if t.status == "found"))

    acting = [t.name for t in statuses if t.status == "present"
              and await builder_guard.acting_tool(t.name, server_module, custom) is not None]
    acting += [t.name for t in statuses if t.status == "found"]  # an outside service: it acts until shown otherwise
    kept = set(plan.approvals.acts_without_asking)
    out.approvals = Approvals(asks_first=[n for n in acting if n not in kept],
                              acts_without_asking=[n for n in acting if n in kept])

    out.room = None
    if _room_check is not None:
        out.room = (await _room_check("orchestration", 1 + len(out.inner_workflows))
                    or (await _room_check("agent", len(out.agents)) if out.agents else None)) or None
    return out
