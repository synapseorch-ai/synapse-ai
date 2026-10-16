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

**What is left of the run caps every step** (PL1a). It used to cap only a
step that runs a workflow, so any other step could outlast its run's budget by
its own `timeout_seconds`, which has no ceiling: a 30-minute run could work for
an hour and a half in one step started at minute 29.

**A host may set a ceiling on how long one run works** (`set_run_ceiling`,
PL1a). Cloud's plans each allow a run a length (30 minutes on Free, 6 hours on
Business), counted as the run's own budget is — working time, not time waiting
for a person. The tighter of the two holds, and a run the host's ceiling stops
says so in the host's words. The self-hosted product sets none.

Imported by the engine and by the Loop and Parallel executors, which time their
body steps the same way; it imports neither.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.config import ORCH_GLOBAL_TIMEOUT_MIN, ORCH_STEP_TIMEOUT
from core.log import get_logger
from core.models_orchestration import StepConfig, StepType

if TYPE_CHECKING:
    from .engine import OrchestrationEngine

_log = get_logger(__name__)

#: How the capped step's message names what ran out, when no host ceiling binds.
OWN_BUDGET = "this run's own timeout"

#: The steps that run other steps inside themselves.
CONTAINERS = (StepType.LOOP, StepType.PARALLEL)

#: The step this task runs inside its `fail_after`: when its limit ends (`time.monotonic()`) and how many seconds it
#: was given. A Team step reads it: its lead is told the time left, and near the end no more work is handed out
#: (TEAM1e, 20 Q2).
_DEADLINE: ContextVar[tuple[float, float] | None] = ContextVar("synapse_step_deadline", default=None)


@contextmanager
def deadline_scope(seconds: float | None) -> Iterator[None]:
    """Beside a step's `fail_after(seconds)`: the deadline its work can read (`step_deadline`)."""
    token = _DEADLINE.set((time.monotonic() + float(seconds), float(seconds)) if seconds else None)
    try:
        yield
    finally:
        try:
            _DEADLINE.reset(token)
        except ValueError:  # an abandoned generator closed from another task: nothing of its to put back
            pass


def step_deadline() -> tuple[float, float] | None:
    """`(ends_at, seconds)` of the step running now, by `time.monotonic()`; None outside one."""
    return _DEADLINE.get()


@dataclass(frozen=True)
class RunCeiling:
    """A host's limit on how long one run may work.

    `name` is how the run's own words name it — "the 30-minute run limit of this
    workspace's plan" — in the message of a run it stops ("Stopped at …") and
    of a step it cuts short ("what was left of …").
    """

    seconds: float
    name: str


_run_ceiling: Callable[[str], Awaitable[RunCeiling | None]] | None = None


def set_run_ceiling(hook: Callable[[str], Awaitable[RunCeiling | None]] | None) -> None:
    """Register (or, with None, clear) the host's per-run ceiling, looked up by run id."""
    global _run_ceiling
    _run_ceiling = hook


async def run_ceiling(run_id: str) -> RunCeiling | None:
    """The host's ceiling for this run, or None. A hook that fails sets none."""
    if _run_ceiling is None:
        return None
    try:
        return await _run_ceiling(run_id)
    except Exception:  # noqa: BLE001 — the run's own budget still holds
        _log.warning("run ceiling lookup failed for %s; the run's own timeout holds", run_id,
                     exc_info=True)
        return None


def _is_default(seconds) -> bool:
    """A step timeout nobody chose: absent, or the default every save writes."""
    return not seconds or seconds in (300, ORCH_STEP_TIMEOUT)


async def step_time_limit(
    step: StepConfig, engine: "OrchestrationEngine", remaining: float | None = None,
    budget: str = OWN_BUDGET,
) -> tuple[float, str | None]:
    """`(seconds, why)`: how long `step` may run, and — when it is not simply the
    step's own timeout — why, for the message a step that runs out says.

    `remaining` is what is left of the run's budget, and caps every step;
    `budget` names that budget in the message. A Loop's or a Parallel's body step
    passes none, since the container's own limit already holds it (see
    `body_step_limit`).
    """
    limit, why = await _step_limit(step, engine)
    if remaining is not None and remaining < limit:
        limit, why = max(1.0, remaining), f"what was left of {budget}"
    return limit, why


async def body_step_limit(
    step: StepConfig, engine: "OrchestrationEngine",
) -> tuple[float | None, str | None]:
    """`(seconds, why)` for a Loop's or a Parallel's body step: its own limit,
    or None for one that is itself a Loop or a Parallel.

    A container has to cover every step it runs inside itself, and each of those
    has its own limit; held to one step's 300 s, a Loop inside a Loop died
    after 300 s of iterations, nowhere near the run's own budget (F411). The
    container around it holds it, and the one at the top of the run holds what
    is left of the run (`engine._execute_loop`), so the run's budget bounds it all
    the way down. A Timeout set on a nested container is not read, as it is not
    at the top level.
    """
    if step.type in CONTAINERS:
        return None, None
    return await step_time_limit(step, engine)


async def _step_limit(step: StepConfig, engine: "OrchestrationEngine") -> tuple[float, str | None]:
    """The step's own limit: its `timeout_seconds`, or for a step that runs a
    workflow, that workflow's own budget (F291)."""
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
    return limit, why


def timed_out(seconds: float, why: str | None) -> str:
    """The end of a step's "timed out" message: `after 1800s (…)`."""
    shown = int(round(seconds)) if seconds >= 10 else round(seconds, 1)
    return f"after {shown}s" + (f" ({why})" if why else "")
