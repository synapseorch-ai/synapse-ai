"""Where search traces are kept: `knowledge_queries` (B1a, D158).

Every search — an agent's tool call, a Retrieve step, an API or MCP call, an
eval, the test-search panel — leaves one row holding its `trace.Trace`, so a run
log can show why the agent saw what it saw, and a search can be replayed.

**A trace is never allowed to fail a search.** `record` logs and returns False
when the row cannot be written; the agent still gets its results.

**Kept 30 days** (`knowledge_trace_retention_days`, D153 #16), except rows an
eval set holds (`retained_by`). OSS arms nothing on a schedule, so `record`
purges the current tenant's expired rows itself, at most once a day per
process; `purge_expired` is also callable directly (cloud's cleanup job).

The query's own embedding is stored beside the trace (`query_embedding`, for
exact replay) and never inside it: `Trace` refuses one, and nothing here
returns the column.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from core.knowledge.trace import Trace
from core.log import get_logger
from core.store.models import KnowledgeQueryDB

_log = get_logger(__name__)

PURGE_EVERY_SECONDS = 24 * 3600
_last_purge: dict[str, float] = {}


def _tenant() -> str:
    from core.tenancy import get_tenant
    return get_tenant()


async def record(trace: Trace, *, query_embedding: bytes | None = None) -> bool:
    """Store `trace` as its `knowledge_queries` row. False (logged) if it could not be written."""
    from core.store import session

    origin = trace.origin
    try:
        async with session() as s:
            s.add(KnowledgeQueryDB(
                id=trace.query_id, tenant_id=_tenant(), surface=origin.surface, run_id=origin.run_id,
                session_id=origin.session_id, agent_id=origin.agent_id, step_id=origin.step_id,
                api_key_id=origin.api_key_id, kb_ids=list(trace.request.kb_ids), trace_version=trace.trace_version,
                trace=trace.model_dump(mode="json"), query_embedding=query_embedding, hit_count=len(trace.hits),
                latency_ms=trace.timings_ms.total, est_usd=trace.costs.total_est_usd,
                degraded=bool(trace.degraded), created_at=datetime.fromisoformat(trace.created_at),
            ))
    except Exception as exc:  # noqa: BLE001 — a trace never fails the search it describes
        _log.warning(f"knowledge trace {trace.query_id} not recorded: {exc}")
        return False
    await _maybe_purge()
    return True


async def get(query_id: str) -> Trace | None:
    """A stored trace, in the current tenant, or None."""
    from core.store import session

    async with session() as s:
        row = (await s.execute(select(KnowledgeQueryDB.trace).where(
            KnowledgeQueryDB.tenant_id == _tenant(), KnowledgeQueryDB.id == query_id))).scalar()
    return Trace.model_validate(row) if row else None


async def purge_expired(*, now: datetime | None = None, retention_days: int | None = None) -> int:
    """Delete the current tenant's traces older than the retention, except those an eval set keeps."""
    from core.knowledge.config import knowledge_settings
    from core.store import session

    days = retention_days if retention_days is not None else knowledge_settings().trace_retention_days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    async with session() as s:
        result = await s.execute(delete(KnowledgeQueryDB).where(
            KnowledgeQueryDB.tenant_id == _tenant(), KnowledgeQueryDB.created_at < cutoff,
            KnowledgeQueryDB.retained_by.is_(None)))
        return int(result.rowcount or 0)


async def _maybe_purge() -> None:
    tenant = _tenant()
    now = time.monotonic()
    last = _last_purge.get(tenant)
    if last is not None and now - last < PURGE_EVERY_SECONDS:
        return
    _last_purge[tenant] = now
    try:
        removed = await purge_expired()
        if removed:
            _log.info(f"knowledge: purged {removed} expired search traces")
    except Exception as exc:  # noqa: BLE001 — housekeeping never fails a search
        _log.warning(f"knowledge trace purge failed: {exc}")
