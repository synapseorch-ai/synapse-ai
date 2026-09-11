"""
Golden sets: a saved list of inputs, re-run on demand and scored together.

The reason to have one is that scoring live runs measures quality against
whatever customers happened to ask that week. A golden set holds the inputs
still, so a change in the score is a change in the *workflow* rather than a
change in the questions.

**A suite run is not a new kind of execution.** Each case is an ordinary run of
the orchestration, started through the same seam every other caller uses, and
scored by the same terminal hook — because the orchestration already carries a
rubric. Nothing here judges anything; it starts runs and keeps a tally. That is
why this file is short.

Three constraints that shaped it
--------------------------------
**Sequential, not parallel.** N cases at once would be N concurrent runs of the
same workflow, which is a self-inflicted load test against whatever the
orchestration talks to, and in a metered deployment it is also N admissions
competing with the customer's real work.

**Cancellable, and the cancel flag lives in the row rather than in memory.**
A suite is the most expensive button in the product — N full runs plus N judge
calls — so someone who starts one by accident must be able to stop it before it
finishes spending. On a fleet the process serving that click is never the
process running the suite, so a module-level set would be a stop button that
does nothing and says it worked. The status column is the one place both can
see, which is the same reason the notification dedup lives in the database.

**The tally is written as it goes.** A suite that only recorded its result at
the end would show nothing for the minutes it takes, and nothing at all if the
process died holding the count.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from core.log import get_logger

_log = get_logger(__name__)

#: The status a suite run is moved to when somebody asks it to stop. Checked
#: between cases — a case already in flight finishes, because killing a run
#: mid-step leaves state nobody asked for.
CANCELLING = "cancelling"


def new_suite_run_id() -> str:
    return f"esr_{uuid.uuid4().hex[:12]}"


async def cancel(suite_run_id: str) -> bool:
    """Ask a suite run to stop. Returns whether there was one to ask.

    Writes the flag where the worker will see it. Nothing is rolled back: the
    runs that already happened are real runs that really cost money, and
    pretending otherwise would make the bill unexplainable.
    """
    from core.store import evals as store

    row = await store.get_suite_run(suite_run_id)
    if row is None or row["status"] != "running":
        return False
    await store.update_suite_run(suite_run_id, status=CANCELLING)
    return True


async def running_for(suite_id: str) -> str | None:
    """The id of this set's run in flight, if one is.

    Read from the store rather than a process-local set, so a second Start is
    refused whichever worker picked the first one up.
    """
    from core.store import evals as store

    for row in await store.suite_runs_for(suite_id, limit=1):
        if row["status"] in ("running", CANCELLING):
            return row["id"]
    return None


async def _stop_requested(suite_run_id: str) -> bool:
    from core.store import evals as store

    row = await store.get_suite_run(suite_run_id)
    return bool(row and row["status"] == CANCELLING)


async def run_suite(suite: dict, server_module, suite_run_id: str | None = None) -> dict:
    """Run every case in `suite`, in order, and score each one.

    Returns the finished `eval_suite_runs` row. Never raises for a case that
    fails: a workflow that errors on one input is a result, not an interruption
    — and quite often the result the suite exists to catch.
    """
    from core.evals import store as evals_store
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import stream_engine_events
    from core.store import evals as store
    from core.store.resources import get_orchestration

    suite_id = suite["id"]
    orch_data = await get_orchestration(suite.get("orchestration_id") or "")
    if not orch_data:
        raise ValueError("The orchestration this set points at no longer exists.")

    rubric = await evals_store.get_rubric(orch_data.get("rubric_id") or "")
    cases = suite.get("cases") or []
    suite_run_id = suite_run_id or new_suite_run_id()

    await store.create_suite_run({
        "id": suite_run_id,
        "suite_id": suite_id,
        "orchestration_id": orch_data["id"],
        "status": "running",
        "case_count": len(cases),
        "rubric_id": (rubric or {}).get("id", ""),
        "rubric_version": int((rubric or {}).get("version") or 1),
    })

    scored = 0
    stopped = False
    for index, case in enumerate(cases):
        if await _stop_requested(suite_run_id):
            stopped = True
            break
        run_id = f"suite_{suite_run_id}_{index}"
        try:
            orch = Orchestration.model_validate(orch_data)
            engine = OrchestrationEngine(orch, server_module)
            # The seam `_score_run` reads, so this case's verdict is
            # findable as part of this suite rather than only as a run.
            engine.eval_suite_run_id = suite_run_id
            async for _event in stream_engine_events(
                engine.run(case.get("input", ""), run_id, session_id=f"suite_{suite_run_id}"),
                run_id,
            ):
                pass
        except Exception as exc:
            _log.warning(f"suite case '{case.get('id')}' failed: {exc}")

        rows = await store.scores_for_suite_run(suite_run_id)
        scored = len(rows)
        mean = (sum(r["score"] for r in rows) / scored) if scored else None
        # Deliberately not written through a status change: a cancel
        # arriving mid-case has already set `cancelling`, and overwriting
        # it here would lose the request.
        await store.update_suite_run(
            suite_run_id, scored_count=scored, mean_score=mean
        )

    rows = await store.scores_for_suite_run(suite_run_id)
    mean = (sum(r["score"] for r in rows) / len(rows)) if rows else None
    await store.update_suite_run(
        suite_run_id,
        status="cancelled" if (stopped or await _stop_requested(suite_run_id)) else "completed",
        scored_count=len(rows),
        mean_score=mean,
        ended_at=_utcnow(),
    )

    return await store.get_suite_run(suite_run_id) or {}


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def estimate(suite: dict) -> dict:
    """What pressing Run will cost, in the units a person can check.

    Shown before the button does anything. A suite is the most expensive action
    in the product and the only one whose price is knowable in advance — N runs
    of a known workflow plus one judge call each — so not showing it would be a
    choice rather than a limitation.
    """
    cases = len(suite.get("cases") or [])
    return {"cases": cases, "runs": cases, "judge_calls": cases}
