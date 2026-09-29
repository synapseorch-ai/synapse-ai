"""How long one step may run.

Every step is given its own `timeout_seconds` (300 s unless set), inside a
`fail_after` — except a step that runs a whole workflow. An Agent step whose
agent is a workflow agent runs a child orchestration inside that one step, and
the child has a budget of its own, `timeout_minutes` (30 by default). The step
used to cut it off at 300 s regardless: a child doing more than five minutes of
work failed its parent step while its own timeout said it could go on (F291).

Naveen's call (2026-09-28): the child's budget wins. `timeout_seconds` defaults
to 300, and every save path writes it — the editor's new step, the builder, the
API's dump — so "unset" cannot be told from 300 on any stored step. On a step
that runs a workflow, 300 (or the configured default) therefore reads as unset,
and the step waits on the child's `timeout_minutes`. Any other value still caps
it — 299 or 301 caps it at about five minutes — and so does what is left of the
parent run's own budget, which is checked only between steps and would not stop
a long child otherwise.

Imported by the engine and by the Loop and Parallel executors, which time their
body steps the same way; it imports neither.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from core.config import ORCH_GLOBAL_TIMEOUT_MIN, ORCH_STEP_TIMEOUT
from core.models_orchestration import StepConfig, StepType

if TYPE_CHECKING:
    from .engine import OrchestrationEngine


def _is_default(seconds) -> bool:
    """A step timeout nobody chose: absent, or the default every save writes."""
    return not seconds or seconds in (300, ORCH_STEP_TIMEOUT)


async def step_time_limit(
    step: StepConfig, engine: "OrchestrationEngine", remaining: float | None = None,
) -> tuple[float, str | None]:
    """`(seconds, why)`: how long `step` may run, and — when it is not simply the
    step's own timeout — why, for the message a step that runs out says.

    `remaining` is what is left of the run's own budget; a Loop's or a Parallel's
    body step passes none, since the container's own limit already holds it.
    """
    own = step.timeout_seconds or ORCH_STEP_TIMEOUT  # as it always was: an int when set
    if step.type != StepType.AGENT or not step.agent_id:
        return own, None

    from core.scale.context import resolve_orchestration
    from .resolved import resolve_for_run

    resolved = await resolve_for_run(engine, step.agent_id, tools=False)
    agent = resolved.agent if resolved else None
    if not agent or agent.get("type") != "orchestrator" or not agent.get("orchestration_id"):
        return own, None
    child = await resolve_orchestration(agent["orchestration_id"])
    if not child:
        return own, None  # the step fails on the missing workflow, as it always has

    try:
        minutes = int(child.get("timeout_minutes") or ORCH_GLOBAL_TIMEOUT_MIN)
    except (TypeError, ValueError):
        minutes = ORCH_GLOBAL_TIMEOUT_MIN
    name = child.get("name") or agent["orchestration_id"]
    limit, why = float(minutes * 60), f"the workflow '{name}''s own {minutes}-minute timeout"
    if not _is_default(step.timeout_seconds) and own < limit:
        limit, why = own, None
    if remaining is not None and remaining < limit:
        limit, why = max(1.0, remaining), "what was left of this run's own timeout"
    return limit, why


def timed_out(seconds: float, why: str | None) -> str:
    """The end of a step's "timed out" message: `after 1800s (…)`."""
    shown = int(round(seconds)) if seconds >= 10 else round(seconds, 1)
    return f"after {shown}s" + (f" ({why})" if why else "")
