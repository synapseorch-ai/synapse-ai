"""
Scale settings management routes (UI-facing, internal auth).
Provides connection testing, sync, worker management, and DLQ access.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class ScaleConfigUpdate(BaseModel):
    redis_url: str = ""
    scale_postgres_url: str = ""
    scale_mode_enabled: bool = False
    scale_auto_sync: bool = False
    worker_concurrency: int = 10
    otlp_endpoint: str = ""
    metrics_token: str = ""
    max_global_queue_depth: int = 1_000_000
    rate_limit_per_tenant_rps: int = 1000
    pgbouncer_mode: bool = False
    redis_cluster_mode: bool = False
    num_queue_shards: int = 1


@router.get("/scale/config")
async def get_scale_config_route():
    from core.config import load_settings
    settings = load_settings()
    return {
        "redis_url": settings.get("redis_url", ""),
        "scale_postgres_url": settings.get("scale_postgres_url", ""),
        "scale_mode_enabled": settings.get("scale_mode_enabled", False),
        "scale_auto_sync": settings.get("scale_auto_sync", False),
        "worker_concurrency": settings.get("worker_concurrency", 10),
        "otlp_endpoint": settings.get("otlp_endpoint", ""),
        "metrics_token": settings.get("metrics_token", ""),
        "max_global_queue_depth": settings.get("max_global_queue_depth", 1_000_000),
        "rate_limit_per_tenant_rps": settings.get("rate_limit_per_tenant_rps", 1000),
        "pgbouncer_mode": settings.get("pgbouncer_mode", False),
        "num_queue_shards": settings.get("num_queue_shards", 1),
    }


@router.post("/scale/config")
async def update_scale_config_route(body: ScaleConfigUpdate):
    from core.config import load_settings, SETTINGS_FILE
    from core.json_store import JsonStore

    store = JsonStore(SETTINGS_FILE, default_factory=dict)
    settings = store.load()
    settings.update({
        "redis_url": body.redis_url,
        "scale_postgres_url": body.scale_postgres_url,
        "scale_mode_enabled": body.scale_mode_enabled,
        "scale_auto_sync": body.scale_auto_sync,
        "worker_concurrency": body.worker_concurrency,
        "otlp_endpoint": body.otlp_endpoint,
        "metrics_token": body.metrics_token,
        "max_global_queue_depth": body.max_global_queue_depth,
        "rate_limit_per_tenant_rps": body.rate_limit_per_tenant_rps,
        "pgbouncer_mode": body.pgbouncer_mode,
        "num_queue_shards": body.num_queue_shards,
    })
    store.save(settings)
    return {"status": "saved"}


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------

@router.post("/scale/test-redis")
async def test_redis_connection(request: Request):
    body = await request.json()
    redis_url = body.get("redis_url", "")
    if not redis_url:
        raise HTTPException(400, detail="redis_url is required")
    try:
        from core.scale.pubsub import get_redis_client
        client = get_redis_client(redis_url)
        await client.ping()
        await client.aclose()
        return {"status": "ok", "message": "Redis connection successful"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/scale/test-postgres")
async def test_postgres_connection(request: Request):
    body = await request.json()
    pg_url = body.get("postgres_url", "")
    if not pg_url:
        raise HTTPException(400, detail="postgres_url is required")
    try:
        from core.scale.db import build_engine
        # Normalise to asyncpg
        if "postgresql+asyncpg" not in pg_url:
            pg_url = pg_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            pg_url = pg_url.replace("postgres://", "postgresql+asyncpg://", 1)
        engine = build_engine(pg_url)
        async with engine.connect() as conn:
            from sqlalchemy import text
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return {"status": "ok", "message": "Postgres connection successful"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

@router.post("/scale/sync")
async def trigger_sync(request: Request):
    """Sync all local JSON data to Postgres."""
    pg_engine = getattr(request.app.state, "pg_engine", None)
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured. Enable scale mode first.")

    from core.scale.sync import full_sync
    from core.scale.config import get_scale_config
    cfg = get_scale_config()

    async with session_factory() as session:
        result = await full_sync(session, tenant_id=cfg.default_tenant_id)

    return result


@router.get("/scale/sync/status")
async def sync_status(request: Request):
    """Return row counts per table (last sync indicator)."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return {"available": False, "message": "Postgres not configured"}

    from core.scale.sync import get_sync_status
    async with session_factory() as session:
        counts = await get_sync_status(session)
    return {"available": True, "counts": counts}


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------

@router.get("/scale/workers")
async def list_workers(request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return []

    from sqlalchemy import select
    from core.scale.models_db import WorkerDB

    async with session_factory() as session:
        result = await session.execute(select(WorkerDB))
        rows = result.scalars().all()

    return [
        {
            "worker_id": r.worker_id,
            "hostname": r.hostname,
            "address": r.address,
            "status": r.status,
            "active_jobs": r.active_jobs,
            "max_jobs": r.max_jobs,
            "last_heartbeat": str(r.last_heartbeat) if r.last_heartbeat else None,
            "mcp_disabled": r.mcp_disabled or [],
        }
        for r in rows
    ]


@router.delete("/scale/workers/{worker_id}")
async def remove_worker(worker_id: str, request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured")

    from sqlalchemy import delete
    from core.scale.models_db import WorkerDB

    async with session_factory() as session:
        await session.execute(delete(WorkerDB).where(WorkerDB.worker_id == worker_id))
        await session.commit()

    return {"status": "removed", "worker_id": worker_id}


@router.get("/scale/workers/{worker_id}/health")
async def worker_health(worker_id: str, request: Request):
    """Proxy health check request to the worker's /health endpoint."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured")

    from sqlalchemy import select
    from core.scale.models_db import WorkerDB

    async with session_factory() as session:
        result = await session.execute(
            select(WorkerDB).where(WorkerDB.worker_id == worker_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(404, detail=f"Worker '{worker_id}' not found")
    if not row.address:
        raise HTTPException(400, detail="Worker has no address configured")

    import time
    import httpx
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{row.address}/health")
            latency_ms = round((time.monotonic() - start) * 1000)
            return {"status": "ok", "latency_ms": latency_ms, "data": resp.json()}
    except Exception as e:
        latency_ms = round((time.monotonic() - start) * 1000)
        return {"status": "error", "latency_ms": latency_ms, "message": str(e)}


# ---------------------------------------------------------------------------
# Queue stats
# ---------------------------------------------------------------------------

@router.get("/scale/queue")
async def queue_stats(request: Request):
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        return {"available": False}

    import os
    queue_name = f"synapse:orchestrations:{os.getenv('WORKER_QUEUE_SHARD', 'default')}"

    try:
        queued = await redis.llen(queue_name) or 0
    except Exception:
        queued = 0

    session_factory = getattr(request.app.state, "pg_session_factory", None)
    active = 0
    failed = 0
    if session_factory:
        try:
            from sqlalchemy import select, func
            from core.scale.models_db import OrchestrationRunDB
            async with session_factory() as session:
                r = await session.execute(
                    select(
                        OrchestrationRunDB.status,
                        func.count().label("cnt"),
                    ).group_by(OrchestrationRunDB.status)
                )
                for row in r.all():
                    if row.status == "running":
                        active = row.cnt
                    elif row.status == "failed":
                        failed = row.cnt
        except Exception:
            pass

    dlq_count = 0
    if session_factory:
        try:
            from sqlalchemy import select, func
            from core.scale.models_db import DeadLetterQueueDB
            async with session_factory() as session:
                r = await session.execute(
                    select(func.count()).select_from(DeadLetterQueueDB).where(
                        DeadLetterQueueDB.resolved == False  # noqa: E712
                    )
                )
                dlq_count = r.scalar() or 0
        except Exception:
            pass

    return {
        "available": True,
        "queue_name": queue_name,
        "queued": queued,
        "active": active,
        "failed": failed,
        "dlq_count": dlq_count,
    }


# ---------------------------------------------------------------------------
# Dead Letter Queue
# ---------------------------------------------------------------------------

@router.get("/scale/dlq")
async def list_dlq(request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return []

    from sqlalchemy import select
    from core.scale.models_db import DeadLetterQueueDB

    async with session_factory() as session:
        result = await session.execute(
            select(DeadLetterQueueDB)
            .where(DeadLetterQueueDB.resolved == False)  # noqa: E712
            .order_by(DeadLetterQueueDB.last_failed_at.desc())
            .limit(50)
        )
        rows = result.scalars().all()

    return [
        {
            "id": str(r.id),
            "run_id": r.run_id,
            "orchestration_id": r.orchestration_id,
            "job_function": r.job_function,
            "error_message": r.error_message,
            "attempt_count": r.attempt_count,
            "last_failed_at": str(r.last_failed_at) if r.last_failed_at else None,
        }
        for r in rows
    ]


@router.post("/scale/dlq/{dlq_id}/retry")
async def retry_dlq_job(dlq_id: str, request: Request):
    """Re-enqueue a DLQ job and mark it as resolved."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    arq_redis = getattr(request.app.state, "arq_redis", None)
    if not session_factory or not arq_redis:
        raise HTTPException(503, detail="Scale mode not available")

    from sqlalchemy import select, update
    from core.scale.models_db import DeadLetterQueueDB
    import uuid as _uuid

    async with session_factory() as session:
        result = await session.execute(
            select(DeadLetterQueueDB).where(
                DeadLetterQueueDB.id == _uuid.UUID(dlq_id)
            )
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(404, detail="DLQ entry not found")

    # Re-enqueue the original job to the correct worker queue
    import os as _os
    queue_name = f"synapse:orchestrations:{_os.getenv('WORKER_QUEUE_SHARD', 'default')}"
    payload = row.job_payload or {}
    await arq_redis.enqueue_job(row.job_function, **payload, _queue_name=queue_name)

    # Mark as resolved
    async with session_factory() as session:
        await session.execute(
            update(DeadLetterQueueDB)
            .where(DeadLetterQueueDB.id == _uuid.UUID(dlq_id))
            .values(resolved=True)
        )
        await session.commit()

    return {"status": "re-enqueued", "dlq_id": dlq_id}


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    tenant_id: str
    name: str = ""
    max_concurrent_runs: int = 100
    max_queued_runs: int = 1000
    priority: int = 0


@router.get("/scale/tenants")
async def list_tenants(request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return []

    from sqlalchemy import select
    from core.scale.models_db import TenantDB

    async with session_factory() as session:
        result = await session.execute(select(TenantDB))
        rows = result.scalars().all()

    return [
        {
            "tenant_id": r.tenant_id,
            "name": r.name,
            "max_concurrent_runs": r.max_concurrent_runs,
            "max_queued_runs": r.max_queued_runs,
            "priority": r.priority,
        }
        for r in rows
    ]


@router.post("/scale/tenants", status_code=201)
async def create_tenant(body: TenantCreate, request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured")

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from core.scale.models_db import TenantDB

    async with session_factory() as session:
        stmt = pg_insert(TenantDB).values(
            tenant_id=body.tenant_id,
            name=body.name,
            max_concurrent_runs=body.max_concurrent_runs,
            max_queued_runs=body.max_queued_runs,
            priority=body.priority,
        ).on_conflict_do_update(
            index_elements=["tenant_id"],
            set_={
                "name": body.name,
                "max_concurrent_runs": body.max_concurrent_runs,
                "max_queued_runs": body.max_queued_runs,
                "priority": body.priority,
            },
        )
        await session.execute(stmt)
        await session.commit()

    return {"status": "created", "tenant_id": body.tenant_id}


@router.delete("/scale/tenants/{tenant_id}")
async def delete_tenant(tenant_id: str, request: Request):
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured")

    from sqlalchemy import delete
    from core.scale.models_db import TenantDB

    async with session_factory() as session:
        await session.execute(delete(TenantDB).where(TenantDB.tenant_id == tenant_id))
        await session.commit()

    return {"status": "deleted", "tenant_id": tenant_id}
