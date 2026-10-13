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

A host that meters runs starts each case
----------------------------------------
Cloud admits every run it bills — a hold, a meter with its plan's length — and
runs it as that run's job, which is what charges it and bounds it. A case
started here in-process had none of that (F441): no hold, no charge, no length,
no credit gate. So a host passes `run_case`, and each case is handed to it with
its run id and the way to run it; the host admits it, runs it, and may refuse it
(`CaseRefused`), which stops the set with the host's reason on its row.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from core.log import get_logger

_log = get_logger(__name__)

#: The status a suite run is moved to when somebody asks it to stop. Checked
#: between cases — a case already in flight finishes, because killing a run
#: mid-step leaves state nobody asked for.
CANCELLING = "cancelling"

#: The status of a suite its host stopped by refusing a case (F441). Its reason
#: is the row's `stop_reason`.
STOPPED = "stopped"

#: How long a case its host cuts waits for its run to stop before the host goes
#: on to end it.
CUT_WAIT_SECONDS = 5

#: `run_case(run_id, session_id, go)`: a host's way to start one case (F441).
#: `go()` runs the case under `run_id`; the host runs it inside whatever admits
#: and meters a run, or raises `CaseRefused`.
CaseRunner = Callable[[str, str, Callable[[], Awaitable[None]]], Awaitable[None]]


class CaseRefused(Exception):
    """A host would not start this case — its wallet, its plan (F441).

    The suite stops before it, `stopped`, with the message as the end of its
    reason: "Stopped before case 4 of 10: <message>".
    """


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


async def run_suite(
    suite: dict,
    server_module,
    suite_run_id: str | None = None,
    *,
    run_case: CaseRunner | None = None,
) -> dict:
    """Run every case in `suite`, in order, and score each one.

    Returns the finished `eval_suite_runs` row. Never raises for a case that
    fails: a workflow that errors on one input is a result, not an interruption
    — and quite often the result the suite exists to catch. A case its host
    refuses (`CaseRefused`) is not a result: the set stops there and says why.
    """
    from core.evals import store as evals_store
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import SENTINEL, spawn_engine_run
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
    refused: str | None = None
    session_id = f"suite_{suite_run_id}"
    for index, case in enumerate(cases):
        if await _stop_requested(suite_run_id):
            stopped = True
            break
        run_id = f"suite_{suite_run_id}_{index}"

        async def go(run_id: str = run_id, case: dict = case) -> None:
            orch = Orchestration.model_validate(orch_data)
            engine = OrchestrationEngine(orch, server_module)
            # The seam `_score_run` reads, so this case's verdict is
            # findable as part of this suite rather than only as a run.
            engine.eval_suite_run_id = suite_run_id
            task, events = spawn_engine_run(
                engine.run(case.get("input", ""), run_id, session_id=session_id), run_id
            )
            try:
                while await events.get() is not SENTINEL:
                    pass
            except asyncio.CancelledError:
                # Cut by whoever runs the case (a host's run length, a stopping
                # worker): its run stops with it rather than going on detached,
                # so nothing writes after the host has ended it.
                task.cancel()
                await asyncio.wait({task}, timeout=CUT_WAIT_SECONDS)
                raise

        try:
            if run_case is None:
                await go()
            else:
                await run_case(run_id, session_id, go)
        except CaseRefused as exc:
            refused = f"Stopped before case {index + 1} of {len(cases)}: {exc}"
            _log.info(f"suite run '{suite_run_id}' stopped: {refused}")
            break
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
    if refused is not None:
        status = STOPPED
    elif stopped or await _stop_requested(suite_run_id):
        status = "cancelled"
    else:
        status = "completed"
    await store.update_suite_run(
        suite_run_id,
        status=status,
        scored_count=len(rows),
        mean_score=mean,
        ended_at=_utcnow(),
        **({"stop_reason": refused} if refused is not None else {}),
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
