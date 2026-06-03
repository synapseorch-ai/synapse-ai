"""
Postgres-backed SharedState — drop-in replacement for core/orchestration/state.py.
Implements the same interface (get, set, update, checkpoint, restore, list_runs)
so OrchestrationEngine can be used unmodified inside worker jobs.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models_orchestration import OrchestrationRun
from core.scale.models_db import OrchestrationRunDB


class SharedStatePG:
    """Persists orchestration run state to Postgres instead of JSON files."""

    def __init__(self, run: OrchestrationRun, session: AsyncSession):
        self.run = run
        self._session = session

    # --- dict-like accessors (same as SharedState) ---

    def get(self, key: str, default=None) -> Any:
        return self.run.shared_state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.run.shared_state[key] = value

    def update(self, data: dict) -> None:
        self.run.shared_state.update(data)

    # --- persistence ---

    async def checkpoint(self) -> None:
        """UPSERT the full run state into orchestration_runs."""
        now = datetime.now(timezone.utc)
        run = self.run

        values = dict(
            run_id=run.run_id,
            orchestration_id=run.orchestration_id,
            session_id=run.session_id,
            status=run.status,
            shared_state=run.shared_state,
            step_history=run.step_history,
            current_step_id=run.current_step_id,
            waiting_for_human=run.waiting_for_human,
            human_prompt=run.human_prompt,
            human_fields=run.human_fields or [],
            nested_run_id=run.nested_run_id,
            nested_orch_id=run.nested_orch_id,
            total_tokens_used=run.total_tokens_used,
            total_cost_usd=run.total_cost_usd,
            cache_read_tokens=run.cache_read_tokens_total,
            cache_write_tokens=run.cache_write_tokens_total,
            cache_hit_count=run.cache_hit_count,
            estimated_savings_usd=run.estimated_savings_usd,
            started_at=_parse_dt(run.started_at),
            ended_at=_parse_dt(run.ended_at),
        )

        stmt = (
            pg_insert(OrchestrationRunDB)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["run_id"],
                set_={k: v for k, v in values.items() if k != "run_id"},
            )
        )
        await self._session.execute(stmt)
        await self._session.commit()

    @classmethod
    async def restore(cls, run_id: str, session: AsyncSession) -> "SharedStatePG":
        """Load run state from Postgres and return a SharedStatePG instance."""
        result = await session.execute(
            select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise FileNotFoundError(f"No DB checkpoint found for run {run_id}")

        run = OrchestrationRun(
            run_id=row.run_id,
            orchestration_id=row.orchestration_id,
            session_id=row.session_id,
            status=row.status,
            shared_state=row.shared_state or {},
            step_history=row.step_history or [],
            current_step_id=row.current_step_id,
            waiting_for_human=row.waiting_for_human or False,
            human_prompt=row.human_prompt,
            human_fields=row.human_fields or [],
            nested_run_id=row.nested_run_id,
            nested_orch_id=row.nested_orch_id,
            total_tokens_used=row.total_tokens_used or 0,
            total_cost_usd=row.total_cost_usd or 0.0,
            cache_read_tokens_total=row.cache_read_tokens or 0,
            cache_write_tokens_total=row.cache_write_tokens or 0,
            cache_hit_count=row.cache_hit_count or 0,
            estimated_savings_usd=row.estimated_savings_usd or 0.0,
            started_at=_dt_to_str(row.started_at),
            ended_at=_dt_to_str(row.ended_at),
        )
        return cls(run, session)

    @classmethod
    async def list_runs(cls, session: AsyncSession, limit: int = 20) -> list[dict]:
        """Return a summary list of recent runs from Postgres."""
        from sqlalchemy import desc

        result = await session.execute(
            select(
                OrchestrationRunDB.run_id,
                OrchestrationRunDB.orchestration_id,
                OrchestrationRunDB.status,
                OrchestrationRunDB.started_at,
                OrchestrationRunDB.ended_at,
            )
            .order_by(desc(OrchestrationRunDB.created_at))
            .limit(limit)
        )
        rows = result.all()
        return [
            {
                "run_id": r.run_id,
                "orchestration_id": r.orchestration_id,
                "status": r.status,
                "started_at": _dt_to_str(r.started_at),
                "ended_at": _dt_to_str(r.ended_at),
            }
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
