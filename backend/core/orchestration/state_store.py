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

from datetime import datetime

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


class StoreStateBackend:
    """Reads and writes `orchestration_runs` in the engine's store."""

    async def save(self, run: OrchestrationRun) -> None:
        from core.store import session, upsert
        from core.store.models import OrchestrationRunDB

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
        from core.store import session
        from core.store.models import OrchestrationRunDB

        stmt = (
            select(OrchestrationRunDB)
            .where(OrchestrationRunDB.tenant_id == get_tenant())
            .order_by(OrchestrationRunDB.created_at.desc())
        )
        if orchestration_ids is not None:
            stmt = stmt.where(OrchestrationRunDB.orchestration_id.in_(orchestration_ids))

        async with session() as s:
            rows = (await s.execute(stmt)).scalars().all()

        summaries: list[dict] = []
        for row in rows:
            if len(summaries) >= limit:
                break
            if _is_structural(row.run_id or ""):
                continue
            history = row.step_history or []
            summaries.append({
                "run_id": row.run_id,
                "orchestration_id": row.orchestration_id,
                "status": row.status,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                # Trigger source: "schedule_<id>" (scheduler), a chat session id
                # (orchestrator agent), or None (manual/API).
                "session_id": row.session_id,
                # Live-progress fields for the runs table
                "current_step_id": row.current_step_id,
                "steps_completed": len(history),
                "last_step_name": (history[-1].get("step_name") if history else None),
                "waiting_for_human": bool(row.waiting_for_human),
                "total_cost_usd": row.total_cost_usd,
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
        "waiting_for_human": bool(row.waiting_for_human),
        "human_prompt": row.human_prompt,
        "human_fields": row.human_fields or [],
        "nested_run_id": row.nested_run_id,
        "nested_orch_id": row.nested_orch_id,
        "total_tokens_used": row.total_tokens_used or 0,
        "total_cost_usd": row.total_cost_usd or 0.0,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
    }
