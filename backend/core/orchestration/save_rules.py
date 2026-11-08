"""What every door that saves a workflow refuses — F850, F853 (BF11, D311).

The builder's tools refused an expression the engine will not run before saving
it (S3), and its validator found a step that runs its own workflow (F691). The
editor's save, an import and cloud's save checked neither, so on prod (QA1b) a
Switch whose expression the engine refuses saved with no issue and its run took
the default branch (F850), and an Agent step pointed at its own workflow's agent
saved 200 (F853). Each door now asks this module.

Both checks read only the workflow and the agents it can see — no store — so
cloud, which saves through its own tables, says the same words. A loop through
another workflow (A runs B, B runs A) is the validator's (`_workflows_called`):
it needs to read the other workflow, and a run's caps stop it.
"""
from __future__ import annotations

from typing import Any

#: The five step fields that carry an expression. `core.expressions` decides
#: what one may contain; this is only the list of places to look.
EXPRESSION_FIELDS = (
    "if_condition",
    "switch_expression",
    "loop_over",
    "loop_while_condition",
    "loop_break_condition",
)


def expression_problems(steps: list, *, unsafe_only: bool) -> list[str]:
    """Every expression problem in `steps`, as sentences naming step and field."""
    from core import expressions

    look = expressions.unsafe_reason if unsafe_only else expressions.check_expression
    found = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id") or "?"
        for field in EXPRESSION_FIELDS:
            reason = look(step.get(field))
            if reason:
                found.append(f"step '{sid}' {field}: {reason}")
    return found


def loop_sentence(step: dict, target: dict, looped: str, where: str = "") -> str:
    """F691's words for a step that runs a workflow already on the chain that reached it."""
    if step.get("type") == "workflow":
        return (f"{where}Workflow step '{step.get('id')}' runs {looped}: every run would call itself again, "
                f"without end. Point the step at another workflow.")
    return (f"{where}Agent step '{step.get('id')}' uses workflow agent '{target.get('id')}', which runs "
            f"{looped}: every run would call itself again, without end. Point the step at the agent that "
            f"does the work, not at a workflow agent of this workflow.")


def self_calls(orch: dict, agents: dict) -> list[str]:
    """A sentence for each step of `orch` that runs `orch` itself: a Workflow step
    naming it, or an Agent step whose agent is one of its workflow agents.

    `agents` maps id → agent, every agent the step could name.
    """
    own = orch.get("id")
    if not own:
        return []
    found = []
    for step in orch.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("type") == "workflow":
            target: dict = {}
            runs = step.get("workflow_id")
        else:
            target = agents.get(step.get("agent_id")) or {}
            runs = target.get("orchestration_id") if target.get("type") == "orchestrator" else None
        if runs and runs == own:
            found.append(loop_sentence(step, target, "this workflow"))
    return found


def agent_ids(orch: dict) -> set[str]:
    """The agents `orch`'s steps name: what a door reads to build `agents`."""
    return {s["agent_id"] for s in orch.get("steps") or []
            if isinstance(s, dict) and isinstance(s.get("agent_id"), str) and s["agent_id"]}


def browser_step_problems(orch: dict, agents: dict) -> list[str]:
    """A Browser step (F1146) names its recording, and one whose agent finds a changed page's way names a Browser
    agent: the run would otherwise stop at that step every time."""
    found = []
    for step in orch.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "browser":
            continue
        sid = step.get("id") or "?"
        if not step.get("recording_id"):
            found.append(f"Browser step '{sid}' needs a recording.")
        if step.get("on_miss") == "agent":
            agent = agents.get(step.get("agent_id")) if step.get("agent_id") else None
            if not agent or agent.get("type") != "browser":
                found.append(f"Browser step '{sid}' has an agent find the way when a page changed: pick a Browser "
                             "agent for it, or have it pause for you.")
    return found


def engine_key_sentence(step: Any) -> str:
    """Why a step may not write `step`'s output key, one that starts with an underscore (F1315)."""
    get = step.get if isinstance(step, dict) else (lambda k: getattr(step, k, None))
    return (f"Step '{get('name') or get('id')}' writes '{get('output_key')}', a key that starts with an underscore: "
            "those are the engine's own, and no step may write one. Give it another output key.")


def engine_key_defaults(schema: Any) -> list[str]:
    """A sentence for each key of a workflow's declared state that starts with an underscore and has a default: the
    engine's own keys are never set by a workflow (F1315)."""
    if not isinstance(schema, dict):
        return []
    return [f"The workflow's state gives '{key}' a default, and a key that starts with an underscore is the engine's "
            "own, which no workflow may set. Give it another name." for key, field in schema.items()
            if str(key).startswith("_") and isinstance(field, dict) and "default" in field]


def reserved_problems(steps: list) -> list[str]:
    """A sentence for the Tool steps that name Synapse AI's own steps, which only its own run takes, and one for each
    step that writes an engine key (F1315)."""
    from core.builder_access import run_only_named

    steps = [s for s in steps or [] if isinstance(s, dict)]
    found = [engine_key_sentence(s) for s in steps if str(s.get("output_key") or "").startswith("_")]
    if own := run_only_named([s.get("forced_tool") for s in steps]):
        found.insert(0, own)
    return found


#: What a reference to a step that does not exist is mended with, after the sentences that name each (F1459).
REFERENCE_FIX = "Point each at a step that exists, or clear it to end the workflow there."


def step_reference_problems(orch: dict) -> list[str]:
    """A sentence for each field that names a step `orch` does not have, in the builder's validator's own words
    (`_validate_orchestration`, which checks each step type's fields as this does), and for an entry that names
    none — F1459 (ISS1, GitHub #376).

    The engine noticed such a field only at the step ("Step '…' not found"), after the steps before it had run. Pure,
    as this module is. A workflow with no steps has none: a new one, or a build's skeleton."""
    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict)]
    if not steps:
        return []
    ids = {s.get("id") for s in steps if s.get("id")}
    found: list[str] = []
    entry = orch.get("entry_step_id") or ""
    if entry and entry not in ids:
        found.append(f"entry_step_id '{entry}' does not match any step.")

    def gone(ref) -> bool:  # empty or None is the end of the workflow
        return bool(ref) and ref not in ids

    for s in steps:
        sid, stype = s.get("id", "?"), s.get("type")
        if gone(nxt := s.get("next_step_id")):
            found.append(f"Step '{sid}' next_step_id '{nxt}' points to unknown step.")
        if gone(spent := s.get("on_exhausted_step_id")):
            found.append(f"Step '{sid}' goes to '{spent}' when it reaches its limit (on_exhausted_step_id), "
                         f"but no step '{spent}' exists.")
        for label, target in (s.get("route_map") or {}).items():
            if gone(target):
                found.append(f"Step '{sid}' route_map[{label!r}] \u2192 '{target}' points to unknown step.")
        for lsid in s.get("loop_step_ids") or []:
            if lsid not in ids:
                found.append(f"Step '{sid}' loop body references unknown step '{lsid}'.")
        for branch in s.get("parallel_branches") or []:
            for bsid in branch or []:
                if bsid not in ids:
                    found.append(f"Step '{sid}' parallel branch references unknown step '{bsid}'.")
        if stype == "if_else":
            if gone(s.get("if_true_step_id")):
                found.append(f"If/Else step '{sid}' if_true_step_id points to unknown step.")
            if gone(s.get("if_false_step_id")):
                found.append(f"If/Else step '{sid}' if_false_step_id points to unknown step.")
        if stype == "switch":
            for val, target in (s.get("switch_cases") or {}).items():
                if gone(target):
                    found.append(f"Switch step '{sid}' case '{val}' points to unknown step '{target}'.")
            if gone(s.get("switch_default_step_id")):
                found.append(f"Switch step '{sid}' switch_default_step_id points to unknown step.")
    return found


def refusal(orch: dict, agents: dict, *, references: bool = True) -> str | None:
    """Why `orch` may not be saved, in one paragraph; `None` when it may.

    `references`: a step named that does not exist (F1459). Off where the save it predicts takes a workflow half
    built (undo's restore goes through the builder's `create_orchestration`), or where its caller already says them
    (a plan's build: its compiler runs the validator)."""
    unsafe = expression_problems(orch.get("steps") or [], unsafe_only=True)
    # One that cannot be read as written (a typo, `{{ … }}` inside a longer expression) is refused as well (F1433,
    # Naveen 11-06): at run time it would fail its step, so it is never saved, and check_build says so first.
    unread = [p for p in expression_problems(orch.get("steps") or [], unsafe_only=False) if p not in unsafe]
    parts = []
    if unsafe:
        parts.append(
            "An expression uses something this engine will not run. " + " ".join(p.rstrip(".") + "." for p in unsafe))
    if unread:
        parts.append(
            "An expression cannot be read as written. " + " ".join(p.rstrip(".") + "." for p in unread))
    if unsafe or unread:
        from core.expressions import vocabulary_sentence

        parts.append(vocabulary_sentence())  # what it may use, from the tables that decide it (F957)
    if not orch.get("system"):  # Synapse AI's own workflow is its own run's (F1315)
        parts += reserved_problems(orch.get("steps") or [])
        parts += engine_key_defaults(orch.get("state_schema"))
    parts += self_calls(orch, agents)
    parts += browser_step_problems(orch, agents)
    if references and (gone := step_reference_problems(orch)):
        parts.append(" ".join(gone) + " " + REFERENCE_FIX)
    if not parts:
        return None
    return "Refusing to save: " + " ".join(parts)
