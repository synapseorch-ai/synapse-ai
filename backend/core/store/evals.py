"""
Judge scores, human feedback, and golden-set suite runs.

Three tables that earn their own module for the reason `CollectionDB`'s
docstring gives: every one of them is *queried*. A collection is fetched by its
own key and nothing else; these are asked for by run, by orchestration in time
order, and by suite — and the orchestration-in-time-order query is doing double
duty as both the trend the dashboard draws and the baseline the next verdict is
measured against.

The rubrics and suites themselves are **not** here. Those are documents fetched
by id, so they live in `collections` alongside skills and widgets. What is here
is the evidence about runs.

Reads are ordered oldest-first when scoped to one run or one orchestration,
matching `core/store/usage.py`: a caller asking for a run's history wants to
see it in the order it happened. `id` breaks ties on the timestamp, which is
accurate to the millisecond and therefore not unique under load.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from core.tenancy import get_tenant


def _ts(dt: datetime | None) -> str:
    """The millisecond ISO stamp the API emits everywhere else."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

def score_to_dict(row) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "orchestration_id": row.orchestration_id,
        "rubric_id": row.rubric_id,
        "rubric_version": row.rubric_version,
        "score": row.score,
        "per_criterion": row.per_criterion or {},
        "per_step": row.per_step or {},
        "reasoning": row.reasoning or "",
        "judged_at": _ts(row.judged_at),
        "judge_model": row.judge_model or "",
        "suite_run_id": row.suite_run_id,
        "baseline": row.baseline,
        "regression": bool(row.regression),
        "rule": row.rule,
        "cost_usd": row.cost_usd or 0.0,
        "latency_seconds": row.latency_seconds or 0.0,
    }


async def append_score(values: dict) -> dict:
    """Insert one verdict. Never an update — see `EvalScoreDB`."""
    from core.store import session
    from core.store.models import EvalScoreDB

    async with session() as s:
        row = EvalScoreDB(tenant_id=get_tenant(), **values)
        s.add(row)
        await s.flush()
        await s.refresh(row)
        return score_to_dict(row)


async def scores_for_run(run_id: str) -> list[dict]:
    """Every verdict on one run, newest first.

    Newest first because the run page shows the current opinion at the top;
    the older rows below it are the record of what earlier rubric versions
    thought, which is the point of keeping them.
    """
    from core.store import session
    from core.store.models import EvalScoreDB

    stmt = (
        select(EvalScoreDB)
        .where(EvalScoreDB.tenant_id == get_tenant(), EvalScoreDB.run_id == run_id)
        .order_by(EvalScoreDB.judged_at.desc(), EvalScoreDB.id.desc())
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [score_to_dict(r) for r in rows]


async def scores_for_runs(run_ids: list[str]) -> dict[str, dict]:
    """The newest verdict for each of several runs, keyed by run id.

    What the runs table needs: one score per row, in one query rather than one
    query per row. Newest wins because that is what the run page shows.
    """
    if not run_ids:
        return {}
    from core.store import session
    from core.store.models import EvalScoreDB

    stmt = (
        select(EvalScoreDB)
        .where(
            EvalScoreDB.tenant_id == get_tenant(),
            EvalScoreDB.run_id.in_(run_ids),
        )
        .order_by(EvalScoreDB.judged_at, EvalScoreDB.id)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    # Oldest first, so the last write for a run wins and the dict ends up
    # holding the newest without a second sort.
    return {r.run_id: score_to_dict(r) for r in rows}


async def scores_for_orchestration(
    orchestration_id: str,
    limit: int = 200,
    rubric_id: str | None = None,
    rubric_version: int | None = None,
) -> list[dict]:
    """An orchestration's verdicts, oldest first — the trend, and the baseline.

    `rubric_version` is what makes a baseline honest: a mean taken across a
    rubric change is a mean of two different questions. The baseline caller
    always passes it; the trend view does not, and renders the version change
    as a break in the line instead.
    """
    from core.store import session
    from core.store.models import EvalScoreDB

    stmt = select(EvalScoreDB).where(
        EvalScoreDB.tenant_id == get_tenant(),
        EvalScoreDB.orchestration_id == orchestration_id,
    )
    if rubric_id is not None:
        stmt = stmt.where(EvalScoreDB.rubric_id == rubric_id)
    if rubric_version is not None:
        stmt = stmt.where(EvalScoreDB.rubric_version == rubric_version)

    # Newest `limit` rows, returned oldest-first: a trend wants the recent
    # window, and a window taken from the wrong end is the whole history.
    stmt = stmt.order_by(EvalScoreDB.judged_at.desc(), EvalScoreDB.id.desc()).limit(limit)
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [score_to_dict(r) for r in reversed(rows)]


async def scores_for_suite_run(suite_run_id: str) -> list[dict]:
    from core.store import session
    from core.store.models import EvalScoreDB

    stmt = (
        select(EvalScoreDB)
        .where(
            EvalScoreDB.tenant_id == get_tenant(),
            EvalScoreDB.suite_run_id == suite_run_id,
        )
        .order_by(EvalScoreDB.judged_at, EvalScoreDB.id)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [score_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def feedback_to_dict(row) -> dict:
    return {
        "id": row.id,
        "created_at": _ts(row.created_at),
        "user_ref": row.user_ref or "",
        "run_id": row.run_id,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "verdict": row.verdict,
        "comment": row.comment or "",
    }


async def set_feedback(values: dict) -> dict:
    """Record a thumb, replacing this person's previous one on the same target.

    Replace rather than append: a thumb is an opinion, not an event, and
    someone who changes their mind has not left two. Scoped to `user_ref` so
    one member's change of heart does not erase a colleague's.
    """
    from core.store import session
    from core.store.models import EvalFeedbackDB

    tenant = get_tenant()
    user_ref = values.get("user_ref") or ""
    stmt = select(EvalFeedbackDB).where(
        EvalFeedbackDB.tenant_id == tenant,
        EvalFeedbackDB.user_ref == user_ref,
    )
    if values.get("run_id"):
        stmt = stmt.where(EvalFeedbackDB.run_id == values["run_id"])
    else:
        stmt = stmt.where(
            EvalFeedbackDB.session_id == values.get("session_id"),
            EvalFeedbackDB.turn_index == values.get("turn_index"),
        )

    async with session() as s:
        existing = (await s.execute(stmt)).scalars().first()
        if existing is not None:
            existing.verdict = values["verdict"]
            existing.comment = values.get("comment")
            await s.flush()
            await s.refresh(existing)
            return feedback_to_dict(existing)
        row = EvalFeedbackDB(tenant_id=tenant, **values)
        s.add(row)
        await s.flush()
        await s.refresh(row)
        return feedback_to_dict(row)


async def clear_feedback(
    user_ref: str,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
) -> bool:
    """Withdraw a thumb. Returns whether there was one to withdraw."""
    from core.store import session
    from core.store.models import EvalFeedbackDB

    stmt = delete(EvalFeedbackDB).where(
        EvalFeedbackDB.tenant_id == get_tenant(),
        EvalFeedbackDB.user_ref == (user_ref or ""),
    )
    if run_id:
        stmt = stmt.where(EvalFeedbackDB.run_id == run_id)
    else:
        stmt = stmt.where(
            EvalFeedbackDB.session_id == session_id,
            EvalFeedbackDB.turn_index == turn_index,
        )
    async with session() as s:
        result = await s.execute(stmt)
    return bool(result.rowcount)


async def feedback_for_run(run_id: str) -> list[dict]:
    from core.store import session
    from core.store.models import EvalFeedbackDB

    stmt = (
        select(EvalFeedbackDB)
        .where(EvalFeedbackDB.tenant_id == get_tenant(), EvalFeedbackDB.run_id == run_id)
        .order_by(EvalFeedbackDB.created_at, EvalFeedbackDB.id)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [feedback_to_dict(r) for r in rows]


async def feedback_for_session(session_id: str) -> list[dict]:
    from core.store import session
    from core.store.models import EvalFeedbackDB

    stmt = (
        select(EvalFeedbackDB)
        .where(
            EvalFeedbackDB.tenant_id == get_tenant(),
            EvalFeedbackDB.session_id == session_id,
        )
        .order_by(EvalFeedbackDB.turn_index, EvalFeedbackDB.id)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [feedback_to_dict(r) for r in rows]


async def feedback_for_runs(run_ids: list[str]) -> dict[str, list[dict]]:
    """Every thumb on each of several runs — what the agreement figure joins."""
    if not run_ids:
        return {}
    from core.store import session
    from core.store.models import EvalFeedbackDB

    stmt = (
        select(EvalFeedbackDB)
        .where(
            EvalFeedbackDB.tenant_id == get_tenant(),
            EvalFeedbackDB.run_id.in_(run_ids),
        )
        .order_by(EvalFeedbackDB.created_at, EvalFeedbackDB.id)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    out: dict[str, list[dict]] = {}
    for row in rows:
        out.setdefault(row.run_id, []).append(feedback_to_dict(row))
    return out


# ---------------------------------------------------------------------------
# Suite runs
# ---------------------------------------------------------------------------

def suite_run_to_dict(row) -> dict:
    return {
        "id": row.id,
        "suite_id": row.suite_id,
        "orchestration_id": row.orchestration_id,
        "status": row.status,
        "case_count": row.case_count,
        "scored_count": row.scored_count,
        "mean_score": row.mean_score,
        "rubric_id": row.rubric_id or "",
        "rubric_version": row.rubric_version,
        "started_at": _ts(row.started_at),
        "ended_at": _ts(row.ended_at),
    }


async def create_suite_run(values: dict) -> dict:
    from core.store import session
    from core.store.models import EvalSuiteRunDB

    async with session() as s:
        row = EvalSuiteRunDB(tenant_id=get_tenant(), **values)
        s.add(row)
        await s.flush()
        await s.refresh(row)
        return suite_run_to_dict(row)


async def update_suite_run(suite_run_id: str, **values) -> None:
    """Patch a suite run in flight, so a half-finished suite reads honestly."""
    from core.store import session
    from core.store.models import EvalSuiteRunDB

    async with session() as s:
        await s.execute(
            update(EvalSuiteRunDB)
            .where(
                EvalSuiteRunDB.tenant_id == get_tenant(),
                EvalSuiteRunDB.id == suite_run_id,
            )
            .values(**values)
        )


async def get_suite_run(suite_run_id: str) -> dict | None:
    from core.store import session
    from core.store.models import EvalSuiteRunDB

    stmt = select(EvalSuiteRunDB).where(
        EvalSuiteRunDB.tenant_id == get_tenant(),
        EvalSuiteRunDB.id == suite_run_id,
    )
    async with session() as s:
        row = (await s.execute(stmt)).scalars().first()
    return suite_run_to_dict(row) if row is not None else None


async def suite_runs_for(suite_id: str, limit: int = 50) -> list[dict]:
    """A suite's history, newest first — what "did this get better" reads."""
    from core.store import session
    from core.store.models import EvalSuiteRunDB

    stmt = (
        select(EvalSuiteRunDB)
        .where(
            EvalSuiteRunDB.tenant_id == get_tenant(),
            EvalSuiteRunDB.suite_id == suite_id,
        )
        .order_by(EvalSuiteRunDB.started_at.desc())
        .limit(limit)
    )
    async with session() as s:
        rows = (await s.execute(stmt)).scalars().all()
    return [suite_run_to_dict(r) for r in rows]
