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


def refusal(orch: dict, agents: dict) -> str | None:
    """Why `orch` may not be saved, in one paragraph; `None` when it may."""
    unsafe = expression_problems(orch.get("steps") or [], unsafe_only=True)
    parts = []
    if unsafe:
        parts.append(
            "An expression uses something this engine will not run. " + " ".join(p.rstrip(".") + "." for p in unsafe)
            + " An expression reads `state` with dots, compares and combines values, "
            "and may call str/int/len and the common string methods — nothing else."
        )
    parts += self_calls(orch, agents)
    if not parts:
        return None
    return "Refusing to save: " + " ".join(parts)
