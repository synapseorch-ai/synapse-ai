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
import types
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

#: Why a set stopped when its run was cut short (F471) — its host's time limit,
#: or a worker stopping under it — after "Stopped before case k of n: ".
SET_CUT = ("the set was cut off before it finished: the worker running it stopped, or it ran "
           "past its time limit.")

#: Why a set nothing was running any more stopped (F471): its worker was killed,
#: or the server running it restarted.
SET_GONE = "the worker running the set stopped before it finished."

#: Why a set handed out again after its worker stopped did not run again (F471):
#: as a chat turn is not (F89), since the cases it ran may already have acted.
SET_HANDED_OUT_AGAIN = ("the worker running the set stopped, and a set is not run again on its "
                        "own: its earlier cases may already have acted. Start it again to run "
                        "every case.")

#: How long a cut set's ending may take on its way out (F471): the cut is the
#: host's, and an ending that hangs must not hold it.
SET_END_SECONDS = 3

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

    A row no live set holds is over, where sets hold leases (scale mode, a
    host's fleet): its worker was killed under it, and nothing will ever end it
    or let Start go again. It is ended here, with why (F471). Standalone, a set
    runs in this process, and a restart's sweep ends the ones it left.
    """
    from core.scale import run_lease
    from core.store import evals as store

    for row in await store.suite_runs_for(suite_id, limit=1):
        if row["status"] in ("running", CANCELLING):
            if run_lease.installed() and not await run_lease.held(row["id"]):
                await end_set(row["id"], SET_GONE)
                return None
            return row["id"]
    return None


async def end_set(suite_run_id: str, why: str, *, before_case: int | None = None) -> bool:
    """End a set its run left unended (F471). False if it had already ended.

    `stopped` with "Stopped before case k of n: <why>", or `cancelled` when
    somebody had asked it to stop — that is what it did. `k` is the case after
    the last one scored when the caller does not know it. Conditional on the
    row still being in flight, so it never rewrites an ending.
    """
    from core.store import evals as store

    row = await store.get_suite_run(suite_run_id)
    if row is None or row["status"] not in ("running", CANCELLING):
        return False
    # The tally as the loop last wrote it, unless a score landed after.
    scores = await store.scores_for_suite_run(suite_run_id)
    scored = max(len(scores), int(row["scored_count"] or 0))
    mean = (sum(r["score"] for r in scores) / len(scores)) if scores else row["mean_score"]
    values: dict = {"scored_count": scored, "mean_score": mean, "ended_at": _utcnow()}
    if row["status"] == CANCELLING:
        values["status"] = "cancelled"
    else:
        total = int(row["case_count"] or 0)
        k = before_case if before_case is not None else min(scored + 1, max(total, 1))
        values["status"] = STOPPED
        values["stop_reason"] = stop_line(k, total, why)
    return await store.end_suite_run(suite_run_id, ("running", CANCELLING), **values)


def stop_line(case: int, total: int, why: str) -> str:
    """A stopped set's reason: "Stopped before case k of n: <why>" — a host's ending too (F441, F471)."""
    return f"Stopped before case {case} of {total}: {why}"


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
    from core.scale import run_lease
    from core.store import evals as store
    from core.store.resources import get_orchestration

    suite_id = suite["id"]
    orch_data = await get_orchestration(suite.get("orchestration_id") or "")
    if not orch_data:
        raise ValueError("The orchestration this set points at no longer exists.")

    rubric = await evals_store.get_rubric(orch_data.get("rubric_id") or "")
    cases = suite.get("cases") or []
    suite_run_id = suite_run_id or new_suite_run_id()

    # Held while the set runs, where the host shares Redis: what tells Start,
    # Stop and the sweeps that it is alive (F471).
    async with run_lease.hold(suite_run_id, kind="set"):
        if await store.get_suite_run(suite_run_id) is not None:
            # Handed out again: its worker stopped under it and its job came
            # back. It is not run again, as a chat turn is not (F89) — the cases
            # it ran may already have acted. One that had ended stays as it was.
            await end_set(suite_run_id, SET_HANDED_OUT_AGAIN)
            return await store.get_suite_run(suite_run_id) or {}
        await store.create_suite_run({
            "id": suite_run_id,
            "suite_id": suite_id,
            "orchestration_id": orch_data["id"],
            "status": "running",
            "case_count": len(cases),
            "rubric_id": (rubric or {}).get("id", ""),
            "rubric_version": int((rubric or {}).get("version") or 1),
        })
        at = types.SimpleNamespace(case=1)
        try:
            return await _run_cases(suite_run_id, orch_data, cases, server_module, run_case, at)
        except asyncio.CancelledError:
            # Cut: the host's time limit, a stopping worker. Its ending used to
            # come only after the loop, so a cut set read `running` for good and
            # refused every Start (F471).
            await _end_cut(suite_run_id, SET_CUT, at.case)
            raise
        except Exception as exc:
            await _end_cut(suite_run_id, f"the set could not go on: {exc}", at.case)
            raise


async def _end_cut(suite_run_id: str, why: str, before_case: int) -> None:
    """A cut set's ending, bounded: the cut is its host's, and must not wait on it."""
    try:
        await asyncio.wait_for(end_set(suite_run_id, why, before_case=before_case), SET_END_SECONDS)
    except Exception:  # noqa: BLE001 — TimeoutError included; a restart's sweep ends it then
        _log.warning(f"suite run '{suite_run_id}': its ending was not written after its cut",
                     exc_info=True)


async def _run_cases(suite_run_id, orch_data, cases, server_module, run_case, at) -> dict:
    """Run the cases in order, then write the set's ending (`run_suite`). `at.case` follows the loop."""
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import SENTINEL, spawn_engine_run
    from core.store import evals as store

    scored = 0
    stopped = False
    refused: str | None = None
    session_id = f"suite_{suite_run_id}"
    for index, case in enumerate(cases):
        at.case = index + 1
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
            refused = stop_line(index + 1, len(cases), str(exc))
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
