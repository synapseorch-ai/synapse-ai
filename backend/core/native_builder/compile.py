"""A Plan into the workflow, agents and inner workflows it describes, and a saved workflow back into a Plan
(SAI2-P1, F775). No save here: P3's `step_save` saves what this returns, inner workflows first.

`compile_plan` maps each part of a Plan onto the engine's own fields — the mapping is one table, `_ENGINE_OF`, read
both ways, so `plan_from_workflow(compile_plan(p))` reads `p` back and the shipped templates round-trip — then fills
the defaults the builder's savers fill (`_fill_step_defaults`) and checks the result with the builder's own validator
(`_validate_orchestration`). What a model cannot know, code decides:

* ids, minted as the builder mints them; a step's output key is its id unless the Plan's `config` names one, and a
  step reads the run's input and what the steps before it wrote;
* `"end"` is an End step the compiler adds, so every path ends at one (the validator's rule);
* a for-each loop's body prompts lead with their item (F767);
* inner workflows are compiled first, each run by a Workflow step (E3), and validated as a called workflow
  (`allow_dead_ends`, F429);
* a schedule is made paused (F149/F150, F155); every tool in the Plan's "asks you first" is set to confirm on a new
  agent that holds it; caps are the workflow's (CAP1);
* only allowlisted workflow settings are taken from a Plan (`settings`): never a rubric, the `system` marker or
  whether it is active (F784's class);
* a step whose tool nothing provides is refused (F489).

`validate` runs the checks that need the workspace (a tool's schema, F489, the approvals rule against the Plan's own
"acts without asking", BWA1's decision 4) and is async; `compile_plan` is pure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from core.models_orchestration import Orchestration, StepConfig
from core.native_builder import prompt_lint
from core.native_builder.objects import Edit, Plan, PlanDraft, Step, settled

#: The workflow settings a Plan may carry (`PlanDraft.settings`). Anything else is refused by name.
SETTINGS = ("state_schema", "result_key", "max_total_turns", "timeout_minutes", "strict_state_types")
#: The step types whose output is kept: each gets an output key (the validator's `_RESULT_STEP_TYPES`).
_RESULT_TYPES = frozenset({"agent", "llm", "tool", "merge", "transform", "extract_json", "human", "retrieve",
                           "workflow"})
#: The step types a model's prompt reaches, which read the run's input and what came before.
_READING_TYPES = frozenset({"agent", "llm", "tool", "evaluator", "workflow"})
#: Fields the mapping writes itself, never copied from `config`, and never read back into it.
_MAPPED = frozenset({"id", "name", "type", "agent_id", "prompt_template", "evaluator_prompt", "human_prompt",
                     "print_content", "forced_tool", "next_step_id", "route_map", "if_condition", "if_true_step_id",
                     "if_false_step_id", "switch_expression", "switch_cases", "switch_default_step_id", "loop_over",
                     "loop_step_ids", "parallel_branches", "workflow_id"})
#: Canvas fields: placed by the builder's zig-zag, never a Plan's business.
_PLACED = frozenset({"position_x", "position_y"})
_DEFAULTS = StepConfig(id="_", name="_", type="print").model_dump(mode="json")


@dataclass
class Compiled:
    """One workflow and what comes with it, ready for P3 to save (inner workflows first)."""

    orchestration: dict[str, Any]
    agents: list[dict[str, Any]] = field(default_factory=list)
    inner: list["Compiled"] = field(default_factory=list)
    schedule: dict[str, Any] | None = None
    boards: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and all(i.ok for i in self.inner)

    def all_issues(self) -> list[str]:
        out = list(self.issues)
        for inner in self.inner:
            out += [f"In the inner workflow '{inner.orchestration.get('name')}': {i}" for i in inner.all_issues()]
        return out


def _mint(prefix: str, length: int = 7) -> str:
    from core.store.ids import new_id

    return new_id(prefix, length=length)


# ── Plan → engine ────────────────────────────────────────────────────────────────────────────────────────────────

def _engine_step(step: Step, *, agent_ids: dict[str, str], workflow_ids: dict[str, str], end_id: str | None,
                 issues: list[str]) -> dict[str, Any]:
    def to(target: str | None) -> str | None:
        return end_id if target == "end" else target

    out: dict[str, Any] = {"id": step.id, "name": step.name, "type": step.type}
    t = step.type
    if step.agent and t in ("agent", "evaluator"):
        out["agent_id"] = agent_ids.get(step.agent, step.agent)
    if t in ("agent", "llm", "tool", "workflow") and step.prompt is not None:
        out["prompt_template"] = step.prompt
    if t == "evaluator":
        out["evaluator_prompt"] = step.prompt
        out["route_map"] = {label: to(target) for label, target in (step.branches or {}).items()}
    if t == "human":
        out["human_prompt"] = step.prompt
    if t == "print":
        out["print_content"] = step.prompt if step.prompt is not None else step.does
    if t == "tool":
        out["forced_tool"] = step.tool
    if t == "if_else":
        out["if_condition"] = step.condition
        out["if_true_step_id"] = to((step.branches or {}).get("true"))
        out["if_false_step_id"] = to((step.branches or {}).get("false"))
    if t == "switch":
        out["switch_expression"] = step.condition
        cases = dict(step.branches or {})
        out["switch_default_step_id"] = to(cases.pop("default", None))
        out["switch_cases"] = {value: to(target) for value, target in cases.items()}
    if t == "loop":
        out["loop_step_ids"] = list(step.body or [])
        if step.loop_over:
            out["loop_over"] = step.loop_over
    if t == "parallel":
        out["parallel_branches"] = [[b] for b in step.body or []]
    if t == "workflow":
        out["workflow_id"] = workflow_ids.get(step.workflow or "", step.workflow)
    if step.next is not None:
        out["next_step_id"] = to(step.next)
    for key, value in (step.config or {}).items():
        if key in _MAPPED and key not in ("parallel_branches", "loop_over") or key in _PLACED:
            continue
        if key not in StepConfig.model_fields:
            issues.append(f"step {step.id}: '{key}' is not a step setting")
            continue
        out[key] = value
    if t == "parallel" and (step.config or {}).get("parallel_branches"):
        out["parallel_branches"] = step.config["parallel_branches"]  # chains of more than one step
    return out


def _wire(steps: list[dict[str, Any]], explicit: dict[str, set[str]]) -> None:
    """Output keys (a result step keeps its id's unless the Plan named one) and input keys (a step a model reads
    gets the run's input and what the steps leading to it wrote), each only where the Plan's `config` did not
    already say."""
    from core.builder_tools import step_successors

    by_id = {s["id"]: s for s in steps}
    for s in steps:
        if "output_key" not in explicit[s["id"]] and s["type"] in _RESULT_TYPES:
            s["output_key"] = s["id"]
    before: dict[str, list[str]] = {s["id"]: [] for s in steps}
    for s in steps:
        for nxt in step_successors(s, by_id):
            if nxt in before and s.get("output_key"):
                before[nxt].append(s["output_key"])
    for s in steps:
        if "input_keys" in explicit[s["id"]]:
            continue
        if s["type"] in _READING_TYPES:
            s["input_keys"] = list(dict.fromkeys(["user_input", *before[s["id"]]]))
        elif s["type"] == "merge":
            # What its branches wrote: each Parallel joining here hands on its branches' outputs.
            branch_keys = [by_id[b]["output_key"] for p in steps if p["type"] == "parallel" and
                           p.get("next_step_id") == s["id"] for chain in p.get("parallel_branches") or []
                           for b in chain if b in by_id and by_id[b].get("output_key")]
            s["input_keys"] = list(dict.fromkeys([*before[s["id"]], *branch_keys]))


def _item_first(orch: dict[str, Any]) -> None:
    """F767: a for-each loop's body prompt that does not lead with its item gets the item as its first line."""
    by_id = {s["id"]: s for s in orch["steps"]}
    for sid in prompt_lint.loops_not_item_first(orch):
        loop = next(s for s in orch["steps"] if s.get("type") == "loop" and sid in (s.get("loop_step_ids") or []))
        key = loop.get("loop_item_key") or "loop_item"
        by_id[sid]["prompt_template"] = f"This item: {{state.{key}}}\n\n{by_id[sid]['prompt_template']}"


def _workflow(title: str, summary: str, steps: list[Step], settings: dict[str, Any] | None, *,
              orch_id: str, agent_ids: dict[str, str], workflow_ids: dict[str, str], end_steps: bool,
              issues: list[str]) -> dict[str, Any]:
    from core.builder_tools import _fill_step_defaults

    if end_steps:
        steps = settled(steps)
    ids = {s.id for s in steps}
    ends = [s.id for s in steps if s.type == "end"]
    needs_end = any(t == "end" for s in steps for t in _plan_targets(s))
    end_id = None
    if needs_end:
        end_id = ends[0] if ends else ("end" if "end" not in ids else "end_of_run")
    engine = [_engine_step(s, agent_ids=agent_ids, workflow_ids=workflow_ids,
                           end_id=end_id if end_steps else None, issues=issues) for s in steps]
    if needs_end and end_steps and not ends:
        engine.append({"id": end_id, "name": "End", "type": "end"})
    explicit = {s.id: set(s.config or {}) for s in steps}
    if end_id is not None:
        explicit.setdefault(end_id, set())
    _wire(engine, explicit)
    # A loop over a step names that step (P0's plans: `loop_over: "fetch_emails"`, `"fetch_emails.output"`); what it
    # goes through is what the step wrote, which code knows. Anything else is the loop's own expression, checked as
    # one.
    written = {s["id"]: s.get("output_key") for s in engine if s.get("output_key")}
    for s in engine:
        over = s.get("loop_over").strip() if s["type"] == "loop" and isinstance(s.get("loop_over"), str) else ""
        head, _, rest = over.partition(".")
        if head in written:
            rest = "" if rest in ("output", "result", "results") else rest
            s["loop_over"] = f"state.{written[head]}" + (f".{rest}" if rest else "")
    orch: dict[str, Any] = {"id": orch_id, "name": title, "description": summary,
                            "entry_step_id": steps[0].id if steps else "", "steps": engine}
    for key, value in (settings or {}).items():
        if key in SETTINGS:
            orch[key] = value
        else:
            issues.append(f"a plan cannot set '{key}': it sets {', '.join(SETTINGS)}")
    _item_first(orch)
    orch["steps"] = _fill_step_defaults(orch["steps"])
    return orch


def _plan_targets(step: Step) -> list[str]:
    return ([step.next] if step.next else []) + list((step.branches or {}).values())


def _validated(orch: dict[str, Any], agents: dict[str, dict], *, called: bool) -> tuple[list[str], list[str]]:
    from core.builder_tools import _validate_orchestration

    verdict = _validate_orchestration(orch, agents=agents, allow_dead_ends=called)
    try:
        Orchestration.model_validate(orch)
    except Exception as e:  # noqa: BLE001 — the engine would refuse it the same way
        verdict["issues"] = [*verdict["issues"], f"The workflow does not load: {str(e).splitlines()[0]}"]
    return list(verdict["issues"]), list(verdict.get("warnings") or [])


def compile_plan(plan: Plan, *, existing_agents: dict[str, dict] | None = None,
                 mint: Callable[..., str] = _mint, end_steps: bool = True, orch_id: str | None = None) -> Compiled:
    """The Plan as the documents P3 saves, with every issue the builder's validator finds. Pure: nothing is read
    from or written to the store. `existing_agents` are the workspace's, by id, for the validator."""
    existing = dict(existing_agents or {})
    issues: list[str] = []

    # New agents first: their ids are what the steps name.
    asks = set(plan.approvals.asks_first)
    agent_ids: dict[str, str] = {}
    agents: list[dict[str, Any]] = []
    for new in plan.agents:
        agent_ids[new.name] = mint("agent", length=10)
    for new in plan.agents:
        agent: dict[str, Any] = {
            "id": agent_ids[new.name], "name": new.name, "description": new.purpose, "avatar": "default",
            "type": "delegate" if new.type == "team" else "conversational", "tools": list(new.tools),
            "repos": [], "db_configs": [], "system_prompt": "", "orchestration_id": None, "model": None,
            "provider": None, "max_turns": None,
            "delegate_agent_ids": [agent_ids.get(m, m) for m in new.members] if new.type == "team" else [],
        }
        if new.type == "team":
            from core.models import AgentTeam

            agent["team"] = AgentTeam(mode="team").model_dump()
        if confirm := {f"tool:{t}": "confirm" for t in new.tools if t in asks}:
            agent["tool_policy"] = confirm
        agents.append(agent)
    known_agents = existing | {a["id"]: a for a in agents}

    # F489: a step whose tool nothing provides is never saved.
    for name in plan.missing:
        issues.append(f"'{name}' is a tool nothing here provides: connect who provides it first, or leave it out")

    # Inner workflows, each compiled and validated as a called workflow.
    workflow_ids: dict[str, str] = {}
    inner: list[Compiled] = []
    for w in plan.inner_workflows:
        workflow_ids[w.title] = mint("orch")
    for w in plan.inner_workflows:
        inner_issues: list[str] = []
        doc = _workflow(w.title, w.does, w.steps, None, orch_id=workflow_ids[w.title], agent_ids=agent_ids,
                        workflow_ids=workflow_ids, end_steps=end_steps, issues=inner_issues)
        found, warned = _validated(doc, known_agents, called=True)
        inner.append(Compiled(orchestration=doc, issues=inner_issues + found, warnings=warned))

    orch_id = orch_id or mint("orch")
    orch = _workflow(plan.title, plan.summary, plan.steps, plan.settings, orch_id=orch_id, agent_ids=agent_ids,
                     workflow_ids=workflow_ids, end_steps=end_steps, issues=issues)
    if plan.caps:
        if plan.caps.credits is not None:
            orch["max_run_credits"] = plan.caps.credits
        if plan.caps.dollars is not None:
            orch["max_run_usd"] = plan.caps.dollars
    found, warned = _validated(orch, known_agents, called=False)
    issues += found

    schedule = None
    if plan.schedule:
        schedule = {"name": plan.title, "description": plan.schedule.says, "enabled": False,
                    "target_type": "orchestration", "target_id": orch_id,
                    "prompt": plan.schedule.input or plan.summary, "schedule_type": "cron",
                    "cron_expression": plan.schedule.cron.strip(), "missed_run_policy": "skip",
                    # Read on the person's clock (F948): the one they named, else their own; none is UTC.
                    "timezone": plan.schedule.timezone or None}
        from core.models_schedule import ScheduleCreate, ScheduleInvalid, validate_timing

        try:
            validate_timing(ScheduleCreate.model_validate(schedule).model_dump())
        except (ScheduleInvalid, ValueError) as e:
            issues.append(f"The schedule cannot run: {e}")
    boards = [{"name": b.name, "description": b.purpose,
               "access": [{"kind": "workflow", "id": orch_id, "access": "work"}]} for b in plan.boards]
    return Compiled(orchestration=orch, agents=agents, inner=inner, schedule=schedule, boards=boards,
                    issues=issues, warnings=warned)


async def validate(compiled: Compiled, plan: Plan, server_module: Any,
                   existing_agents: dict[str, dict] | None = None, *, own_steps: set[str] | None = None) -> list[str]:
    """The checks that need the workspace, over the workflow and its inner ones: a tool nothing provides on a step
    (F489), set arguments that do not fit their tool (E1), and BWA1's decision 4 — nothing that changes something
    outside runs with nobody approving it, except what the Plan's "acts without asking" lets act. The issues are
    added to `compiled` and returned."""
    # A read API's tools the build makes after the yes, by method: held to decision 4 now, not after (F944).
    to_make = {t.name: str((t.api or {}).get("endpoint", {}).get("method") or "POST").upper()
               for t in plan.tools if t.status == "found" and t.via == "http_tool" and t.api}
    from core import builder_guard

    workflows = [compiled.orchestration, *(i.orchestration for i in compiled.inner)]
    agents = dict(existing_agents or {}) | {a["id"]: a for a in compiled.agents}
    found, warned = await builder_guard.missing_in(workflows, agents, server_module)
    found += await builder_guard.argument_issues(workflows, server_module)
    made = {a["id"] for a in compiled.agents}
    exempt = set(plan.approvals.acts_without_asking)
    # `own_steps` scopes an update's own workflow only: a build's inner workflows are new, so wholly its own (SAI2's
    # re-check of blocker 1).
    gaps, gap_warnings = await builder_guard.unapproved(
        workflows[:1], agents, server_module, made_agents=made, exempt=exempt, strict=True, own_steps=own_steps,
        to_make=to_make)
    inner_gaps, inner_warnings = await builder_guard.unapproved(
        workflows[1:], agents, server_module, made_agents=made, exempt=exempt, strict=True, to_make=to_make)
    gaps, gap_warnings = [*gaps, *inner_gaps], [*gap_warnings, *inner_warnings]
    added = [*found, *gaps]
    compiled.issues += added
    compiled.warnings += [*warned, *gap_warnings]
    return added


# ── engine → Plan ────────────────────────────────────────────────────────────────────────────────────────────────

def _plan_step(step: dict[str, Any], *, end_ids: set[str], bodies: set[str]) -> Step:
    s = StepConfig.model_validate(step).model_dump(mode="json")
    t = s["type"]

    def back(target: str | None) -> str | None:
        return "end" if target is None or target in end_ids else target

    out: dict[str, Any] = {"id": s["id"], "type": t, "name": s["name"], "does": s["name"]}
    if t in ("agent", "evaluator") and s.get("agent_id"):
        out["agent"] = s["agent_id"]
    if t in ("agent", "llm", "tool", "workflow"):
        out["prompt"] = s.get("prompt_template")
    if t == "evaluator":
        out["prompt"] = s.get("evaluator_prompt")
        out["branches"] = {label: back(target) for label, target in (s.get("route_map") or {}).items()}
    if t == "human":
        out["prompt"] = s.get("human_prompt")
    if t == "print":
        out["prompt"] = s.get("print_content")
    if t == "tool":
        out["tool"] = s.get("forced_tool")
    if t == "if_else":
        out["condition"] = s.get("if_condition")
        out["branches"] = {"true": back(s.get("if_true_step_id")), "false": back(s.get("if_false_step_id"))}
    if t == "switch":
        out["condition"] = s.get("switch_expression")
        branches = {value: back(target) for value, target in (s.get("switch_cases") or {}).items()}
        if s.get("switch_default_step_id") is not None:
            branches["default"] = back(s["switch_default_step_id"])
        out["branches"] = branches
    if t == "loop":
        out["body"] = list(s.get("loop_step_ids") or [])
        out["loop_over"] = s.get("loop_over")
    if t == "parallel":
        branches = s.get("parallel_branches") or []
        out["body"] = [chain[0] for chain in branches if chain]
    if t == "workflow":
        out["workflow"] = s.get("workflow_id")
    if t != "end":
        if s.get("next_step_id") is not None:
            out["next"] = back(s["next_step_id"])
        elif t not in ("if_else", "switch", "evaluator") and s["id"] not in bodies:
            out["next"] = "end"  # it ends the run; a branching step or a container's body step has no next at all
    # What the workflow chose for its data flow is kept as it is, a choice of none included: the compiler's
    # defaults are for a model's Plan, which never says.
    config = {k: v for k, v in s.items()
              if k not in _MAPPED and k not in _PLACED and (v != _DEFAULTS.get(k) or k in ("output_key", "input_keys"))}
    if t == "parallel" and any(len(c) > 1 for c in s.get("parallel_branches") or []):
        config["parallel_branches"] = s["parallel_branches"]
    if config:
        out["config"] = config
    return Step.model_validate(out)


def plan_from_workflow(orch: dict[str, Any], *, agents: dict[str, dict] | None = None) -> Plan:
    """A saved workflow as a Plan: the base an update Plan's edits apply to (P2/P3), and how a template is checked to
    round-trip. Its agents are existing ones, by id; what the mapping does not carry rides in each step's
    `config`, and the allowlisted workflow settings in `settings`."""
    steps = list(orch.get("steps") or [])
    end_ids = {s["id"] for s in steps if s.get("type") == "end"}
    entry = orch.get("entry_step_id")
    steps.sort(key=lambda s: s.get("id") != entry)  # the entry first: a Plan starts where the workflow does
    bodies = {b for s in steps for b in [*(s.get("loop_step_ids") or []),
                                          *(x for chain in s.get("parallel_branches") or [] for x in chain)]}
    plan_steps = [_plan_step(s, end_ids=end_ids, bodies=bodies) for s in steps]
    reuse = list(dict.fromkeys(s.agent for s in plan_steps if s.agent))
    settings = {k: orch[k] for k in SETTINGS if orch.get(k) not in (None, "", {}, [])
                and orch.get(k) != Orchestration.model_fields[k].default}
    draft = PlanDraft(kind="update", title=orch.get("name") or orch.get("id") or "", summary=orch.get("description")
                      or "", steps=plan_steps, reuse_agents=reuse, settings=settings or None)
    return Plan.from_draft(draft)


# ── an update Plan's edits ───────────────────────────────────────────────────────────────────────────────────────

def apply_edits(base: Plan, edits: list[Edit]) -> tuple[Plan, list[str]]:
    """A workflow's Plan (`plan_from_workflow`) with an update's edits applied, and why any could not be."""
    plan = base.model_copy(deep=True)
    issues: list[str] = []
    by_id = {s.id: s for s in plan.steps}
    for edit in edits:
        if edit.op == "add_step":
            if edit.step is None or edit.after not in by_id:
                issues.append(f"add_step: needs the new step and an existing step to go after ('{edit.after}')")
                continue
            if edit.step.id in by_id:
                issues.append(f"add_step: a step '{edit.step.id}' is already there")
                continue
            before = by_id[edit.after]
            new = edit.step.model_copy(update={"next": edit.step.next or before.next})
            before.next = new.id
            plan.steps.insert(plan.steps.index(before) + 1, new)
            by_id[new.id] = new
        elif edit.op == "update_step":
            current = by_id.get(edit.step_id or "")
            if current is None:
                issues.append(f"update_step: there is no step '{edit.step_id}'")
                continue
            unknown = [k for k in edit.fields if k not in Step.model_fields or k == "id"]
            if unknown:
                issues.append(f"update_step: '{edit.step_id}' cannot set {', '.join(unknown)}")
                continue
            updated = Step.model_validate({**current.model_dump(), **edit.fields})
            plan.steps[plan.steps.index(current)] = updated
            by_id[updated.id] = updated
        elif edit.op == "remove_step":
            gone = by_id.pop(edit.step_id or "", None)
            if gone is None:
                issues.append(f"remove_step: there is no step '{edit.step_id}'")
                continue
            plan.steps.remove(gone)
            for s in plan.steps:  # what led to it now leads where it led
                if s.next == gone.id:
                    s.next = gone.next
                if s.branches:
                    s.branches = {k: (gone.next or "end") if v == gone.id else v for k, v in s.branches.items()}
                if s.body and gone.id in s.body:
                    s.body = [b for b in s.body if b != gone.id]
        elif edit.op == "set_meta":
            for key, value in edit.fields.items():
                if key in ("title", "summary"):
                    setattr(plan, key, value)
                elif key in SETTINGS:
                    plan.settings = {**(plan.settings or {}), key: value}
                else:
                    issues.append(f"set_meta cannot set '{key}': it sets title, summary, {', '.join(SETTINGS)}")
    return plan, issues
