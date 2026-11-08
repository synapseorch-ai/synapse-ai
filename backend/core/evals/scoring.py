"""
Scoring a finished run: fetch the rubric, judge, measure, record, announce.

This is the only module that knows the whole sequence, and it is deliberately
the only one that does I/O *and* decides anything. `judge.py` makes a model
call, `regression.py` is pure arithmetic, `store.py` reads and writes
documents; this puts them in order and owns the failure policy.

Two rules it exists to enforce
-----------------------------
**The baseline is taken before this run's score is written.** Otherwise a run
is measured partly against itself — which sounds harmless and is not, because
it drags the mean toward whatever just happened and makes a sustained decline
look like a series of small ones.

**Nothing here may fail a run.** The run is already finished and its result
already belongs to the customer; a judge that raises would turn a successful
run into a failed one over an opinion about it. Every entry point is wrapped,
and the caller in the engine wraps it again.
"""
from __future__ import annotations

from core.evals import judge as _judge
from core.evals import regression as _regression
from core.evals import store as _store
from core.log import get_logger

_log = get_logger(__name__)

#: Runs in these states are worth judging. A failed run **is** scored — "it
#: failed" is a quality signal, and skipping it would hide exactly the
#: regressions this feature exists to surface. A paused run is not: it has not
#: finished, and judging it would score half of someone's work.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


async def rubric_for_orchestration(orch) -> dict | None:
    """The rubric bound to an orchestration, if it has one and it still exists.

    A deleted rubric leaves a dangling id on the orchestration rather than
    rewriting every definition that referenced it, so a miss here is ordinary
    and means "not evaluated", not "broken".
    """
    rubric_id = getattr(orch, "rubric_id", None) or (
        orch.get("rubric_id") if isinstance(orch, dict) else None
    )
    if not rubric_id:
        return None
    return await _store.get_rubric(rubric_id)


async def score_run(run, orch, settings: dict, suite_run_id: str | None = None) -> dict | None:
    """Judge one finished run and record the verdict. Returns it, or None.

    None means "not scored" for any reason — no rubric bound, not terminal, the
    judge did not answer — and is never an error. The caller has nothing to do
    about it either way.
    """
    if run.status not in TERMINAL_STATUSES:
        return None

    rubric = await rubric_for_orchestration(orch)
    if not rubric:
        return None

    result_key = getattr(orch, "result_key", None) or (
        orch.get("result_key") if isinstance(orch, dict) else None
    ) or ""
    verdict = await _judge.judge_run(run, rubric, settings, result_key=result_key)
    if verdict is None:
        return None

    version = int(rubric.get("version") or 1)
    window = int(rubric.get("baseline_runs") or _store.DEFAULT_BASELINE_RUNS)

    # Read before write: the baseline is what the orchestration looked like
    # *before* this run, on this same rubric version.
    try:
        from core.store import evals as _evals_store
        prior = await _evals_store.scores_for_orchestration(
            run.orchestration_id,
            limit=window,
            rubric_id=rubric["id"],
            rubric_version=version,
        )
    except Exception as exc:
        _log.warning(f"baseline lookup failed for run '{run.run_id}': {exc}")
        prior = []

    base = _regression.baseline([p["score"] for p in prior], window)
    regressed, rule = _regression.verdict(
        verdict["score"], base, floor=rubric.get("floor"), drop=rubric.get("drop")
    )

    values = {
        "run_id": run.run_id,
        "orchestration_id": run.orchestration_id,
        "rubric_id": rubric["id"],
        "rubric_version": version,
        "score": verdict["score"],
        "per_criterion": verdict.get("per_criterion") or {},
        "per_step": verdict.get("per_step") or {},
        "reasoning": verdict.get("reasoning") or "",
        "judge_model": verdict.get("judge_model") or "",
        "suite_run_id": suite_run_id,
        "baseline": base,
        "regression": regressed,
        "rule": rule,
        "latency_seconds": verdict.get("latency_seconds") or 0.0,
    }

    from core.store import evals as _evals_store
    record = await _evals_store.append_score(values)

    if regressed:
        await _announce(run, orch, rubric, record)
    return record


async def _announce(run, orch, rubric: dict, record: dict) -> None:
    """Tell someone. Best-effort: a notification failure is not a scoring one.

    The wording comes from `regression.describe` rather than being composed
    here, so the bell, the dashboard and the stored `rule` cannot drift into
    describing the same verdict three different ways.
    """
    try:
        from core.notifications import hub

        name = getattr(orch, "name", None) or (
            orch.get("name") if isinstance(orch, dict) else None
        ) or run.orchestration_id
        body = _regression.describe(
            record.get("rule"),
            record["score"],
            record.get("baseline"),
            rubric.get("floor"),
            rubric.get("drop"),
        )
        hub.publish(
            "eval_regression",
            f"{name} scored below expectation",
            body=body,
            run_id=run.run_id,
            data={
                "score": record["score"],
                "baseline": record.get("baseline"),
                "rule": record.get("rule"),
                "rubric_id": rubric.get("id"),
                "orchestration_id": run.orchestration_id,
            },
        )
    except Exception as exc:
        _log.warning(f"regression notification failed for run '{run.run_id}': {exc}")
