"""
The default `StateBackend`: run state in the engine's store.

This is what makes a run portable between processes. A run paused on one worker
is resumed by reading these rows on any other, which is the whole reason the
queue no longer needs per-tenant affinity.

Structural runs are hidden from `list_runs`, matching the previous behaviour:
builder sessions (``builder_*``) and nested sub-orchestration runs
(``{parent}__{step}_dN``) are implementation details of a parent run and can
never be opened on their own.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from core.models_orchestration import OrchestrationRun
from core.tenancy import get_tenant

#: Columns that mirror fields of the run document. Kept explicit rather than
#: derived: `orchestration_runs` also carries worker attribution, webhooks and
#: metering that the engine must not overwrite from an in-memory run object.
_MIRRORED = (
    "orchestration_id",
    "session_id",
    "status",
    "shared_state",
    "step_history",
    "current_step_id",
    "tasks",
    "step_progress",
    "waiting_for_human",
    "human_prompt",
    "human_fields",
    "nested_run_id",
    "nested_orch_id",
    "total_tokens_used",
    "total_cost_usd",
)

#: `OrchestrationRun` carries these as ISO strings; the columns are DateTime.
#: Postgres would coerce a string, SQLite refuses it outright — so the
#: conversion is explicit rather than left to the driver.
_TIMESTAMPS = ("started_at", "ended_at")


def _iso_utc(value):
    """A stored timestamp as an unambiguous UTC string.

    SQLite has no timezone type: an aware datetime goes in and a *naive* one
    comes back, so `isoformat()` would drop the zone and a browser would read
    the result as local time — a run a minute old rendering as hours old east
    of UTC. Everything here is stamped in UTC (`engine.run` uses `time.gmtime`),
    so a naive value means "UTC, marker lost in storage" and is labelled as
    such. An aware value (Postgres keeps the zone) is converted rather than
    assumed.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _to_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    try:
        # `fromisoformat` handles the trailing "Z" only from 3.11 onwards; the
        # replace keeps this working if the run was written by an older build.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_structural(run_id: str) -> bool:
    return run_id.startswith("builder_") or "__" in run_id


async def claim_answer(
    session_factory,
    run_id: str,
    *,
    pause_ordinal: int | None,
    job_id: str,
    worker_id: str,
    redelivery: bool = False,
) -> bool:
    """Take a paused run for one answer, or say another answer has it (F136).

    Reading the run and then writing "running" let two answer jobs picked up in
    one poll both see it asking, both pass F111's check, and both drive the
    engine from the same checkpoint — every step after the question ran twice.
    So the write is the check: one UPDATE that matches only a run still paused
    on a question, and whichever job's UPDATE lands first is the one that
    answers. The other matches nothing.

    `redelivery` is a job handed out again (`job_try > 1`) whose first try may
    have claimed the run and stopped before the answer reached a checkpoint. It
    matches its own claim by the job id ARQ keeps across tries — the run is
    `running` then, or `failed` if the API's startup sweep got to it first.

    The ordinal is compared *after* the claim, in the same transaction, because
    that is the only place it cannot go stale: a winner that answers, carries on
    and pauses at the next question before a loser's UPDATE lands leaves a run
    that is paused again — on a question the loser's answer is not for.
    Compared as F111 does: an answer that names no question, or a run whose
    pause has no entry to compare, is applied.

    The UPDATE is the transaction's first statement on purpose: on SQLite a
    transaction that has read first cannot then wait for the write lock.
    """
    from sqlalchemy import and_, or_, update

    from core.orchestration.pauses import pause_ordinal as ordinal_of
    from core.store.models import OrchestrationRunDB as Run

    asking = and_(Run.status == "paused", Run.waiting_for_human.is_(True))
    if redelivery and job_id:
        asking = or_(asking, and_(Run.job_id == job_id, Run.status.in_(("running", "failed"))))

    async with session_factory() as s:
        claimed = (await s.execute(
            update(Run)
            .where(Run.run_id == run_id, Run.tenant_id == get_tenant(), asking)
            .values(status="running", waiting_for_human=False, worker_id=worker_id, job_id=job_id)
            .returning(Run.step_history)
        )).one_or_none()
        if claimed is None:
            await s.rollback()
            return False
        now_asking = ordinal_of(claimed.step_history)
        if pause_ordinal is not None and now_asking is not None and now_asking != pause_ordinal:
            await s.rollback()
            return False
        await s.commit()
        return True


class StoreStateBackend:
    """Reads and writes `orchestration_runs` in the engine's store."""

    #: `save` accepts `columns` — see `SharedState.checkpoint`. A backend
    #: without this attribute gets full saves only.
    supports_partial_save = True

    async def save(self, run: OrchestrationRun, columns: Sequence[str] | None = None) -> None:
        """Persist the run. With `columns`, update only those.

        A full save serialises the whole run and upserts every mirrored
        column. A per-turn progress checkpoint used to do exactly that —
        rewriting shared_state, step_history and every execution-memory blob
        to store one step's transcript — so `columns` exists for the callers
        that change one thing. It is an UPDATE, never an insert: the first
        write of a run is always full (the engine's initial checkpoint, the
        worker's row pre-create), and a partial write finding no row falls
        back to a full save rather than inserting a run with no
        orchestration_id.
        """
        from core.store import session, upsert
        from core.store.models import OrchestrationRunDB

        if columns:
            from sqlalchemy import update

            subset = run.model_dump(mode="json", include=set(columns))
            values = {c: subset.get(c) for c in columns if c in _MIRRORED}
            for column in _TIMESTAMPS:
                if column in values:
                    values[column] = _to_datetime(values[column])
            if values:
                async with session() as s:
                    result = await s.execute(
                        update(OrchestrationRunDB)
                        .where(
                            OrchestrationRunDB.run_id == run.run_id,
                            OrchestrationRunDB.tenant_id == get_tenant(),
                        )
                        .values(**values)
                    )
                    if result.rowcount:
                        return
            # No row yet (or nothing mirrored was named): the full path below.

        # mode="json" so arbitrary objects inside shared_state / step_history
        # are already primitives by the time the JSON column serialises them.
        document = run.model_dump(mode="json")
        values = {"run_id": run.run_id, "tenant_id": get_tenant()}
        for column in _MIRRORED:
            if column in document:
                values[column] = document[column]
        for column in _TIMESTAMPS:
            values[column] = _to_datetime(document.get(column))

        async with session() as s:
            await upsert(
                s,
                OrchestrationRunDB,
                values=values,
                index_elements=["run_id"],
                # Never rewrite the tenant on an update: a run belongs to whoever
                # started it, and letting a checkpoint move it would turn a
                # mis-scoped resume into silent cross-tenant data movement.
                update=[c for c in values if c not in ("run_id", "tenant_id")],
            )

    async def load(self, run_id: str) -> OrchestrationRun | None:
        from core.store import session
        from core.store.models import OrchestrationRunDB

        async with session() as s:
            row = (
                await s.execute(
                    select(OrchestrationRunDB).where(
                        OrchestrationRunDB.run_id == run_id,
                        OrchestrationRunDB.tenant_id == get_tenant(),
                    )
                )
            ).scalar_one_or_none()

        if row is None:
            return None
        return OrchestrationRun.model_validate(_to_document(row))

    async def list_runs(
        self, limit: int = 20, orchestration_ids: set[str] | None = None
    ) -> list[dict]:
        from core.orchestration.run_error import failure_reason
        from core.store import session
        from core.store.models import OrchestrationRunDB

        # Structural runs are left out by the query, and the page is cut there
        # too (F251): a run's children each have a row of their own now, one per
        # run of a nested step, and reading every row of the tenant to keep a
        # page of twenty grew with every loop iteration.
        stmt = (
            select(OrchestrationRunDB)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(~OrchestrationRunDB.run_id.contains("__", autoescape=True))
            .where(~OrchestrationRunDB.run_id.startswith("builder_", autoescape=True))
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(limit)
        )
        if orchestration_ids is not None:
            stmt = stmt.where(OrchestrationRunDB.orchestration_id.in_(orchestration_ids))

        async with session() as s:
            rows = (await s.execute(stmt)).scalars().all()

        summaries: list[dict] = []
        for row in rows:
            if _is_structural(row.run_id or ""):
                continue
            history = row.step_history or []
            summaries.append({
                "run_id": row.run_id,
                "orchestration_id": row.orchestration_id,
                "status": row.status,
                "started_at": _iso_utc(row.started_at),
                "ended_at": _iso_utc(row.ended_at),
                # Trigger source: "schedule_<id>" (scheduler), a chat session id
                # (orchestrator agent), or None (manual/API).
                "session_id": row.session_id,
                # Live-progress fields for the runs table
                "current_step_id": row.current_step_id,
                "steps_completed": len(history),
                "last_step_name": (history[-1].get("step_name") if history else None),
                "waiting_for_human": bool(row.waiting_for_human),
                "total_cost_usd": row.total_cost_usd,
                # Why it stopped, for a failed or cancelled run (F257).
                "error": failure_reason(row.status, history, row.step_progress),
            })
        return summaries


def _to_document(row) -> dict:
    """Rebuild the shape `OrchestrationRun` validates from a stored row."""
    return {
        "run_id": row.run_id,
        "orchestration_id": row.orchestration_id,
        "session_id": row.session_id,
        "status": row.status,
        "shared_state": row.shared_state or {},
        "step_history": row.step_history or [],
        "current_step_id": row.current_step_id,
        "tasks": row.tasks or [],
        "step_progress": row.step_progress or {},
        "waiting_for_human": bool(row.waiting_for_human),
        "human_prompt": row.human_prompt,
        "human_fields": row.human_fields or [],
        "nested_run_id": row.nested_run_id,
        "nested_orch_id": row.nested_orch_id,
        "total_tokens_used": row.total_tokens_used or 0,
        "total_cost_usd": row.total_cost_usd or 0.0,
        "started_at": _iso_utc(row.started_at),
        "ended_at": _iso_utc(row.ended_at),
    }


async def unfinished_descendants(run_id: str) -> list[str]:
    """Ids of the runs nested under `run_id`, at any depth, that have not finished (F257).

    A nested run's id is its parent's plus `__…`. `autoescape` is not optional:
    run ids are full of `_`, which LIKE reads as "any one character", so an
    unescaped `run_x__%` would also match another run whose id merely starts
    with `run_x` and two more characters.
    """
    from core.store import session
    from core.store.models import OrchestrationRunDB

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(OrchestrationRunDB.run_id.startswith(f"{run_id}__", autoescape=True))
            .where(OrchestrationRunDB.status.in_(("running", "paused", "queued")))
        )).scalars().all()
    return sorted(rows)


async def list_children(run_id: str) -> list[dict]:
    """The runs nested directly under `run_id`, oldest first (F251).

    Direct only — a grandchild is listed under its own parent. Each with what a
    list needs: its orchestration, status, position, times, cost and, for one
    that did not complete, why.
    """
    from core.orchestration.run_error import failure_reason
    from core.store import session
    from core.store.models import OrchestrationRunDB

    prefix = f"{run_id}__"
    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .where(OrchestrationRunDB.run_id.startswith(prefix, autoescape=True))
            .order_by(OrchestrationRunDB.created_at)
        )).scalars().all()
    children = []
    for row in rows:
        if "__" in row.run_id[len(prefix):]:
            continue
        history = row.step_history or []
        children.append({
            "run_id": row.run_id,
            "orchestration_id": row.orchestration_id,
            "status": row.status,
            "started_at": _iso_utc(row.started_at),
            "ended_at": _iso_utc(row.ended_at),
            "current_step_id": row.current_step_id,
            "steps_completed": len(history),
            "waiting_for_human": bool(row.waiting_for_human),
            "total_cost_usd": row.total_cost_usd,
            "error": failure_reason(row.status, history, row.step_progress),
        })
    return children
