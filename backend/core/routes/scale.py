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
    # S3 storage
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "synapse"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_endpoint_url: str = ""


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
        "s3_bucket": settings.get("s3_bucket", ""),
        "s3_region": settings.get("s3_region", "us-east-1"),
        "s3_prefix": settings.get("s3_prefix", "synapse"),
        "s3_access_key_id": settings.get("s3_access_key_id", ""),
        "s3_secret_access_key": settings.get("s3_secret_access_key", ""),
        "s3_endpoint_url": settings.get("s3_endpoint_url", ""),
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
        "s3_bucket": body.s3_bucket,
        "s3_region": body.s3_region,
        "s3_prefix": body.s3_prefix,
        "s3_access_key_id": body.s3_access_key_id,
        "s3_secret_access_key": body.s3_secret_access_key,
        "s3_endpoint_url": body.s3_endpoint_url,
    })
    store.save(settings)
    # Invalidate the S3 singleton so the new config is picked up immediately
    from core.s3_storage import invalidate_s3_singleton
    invalidate_s3_singleton()
    return {"status": "saved"}


# ---------------------------------------------------------------------------
# Connection testing
# ---------------------------------------------------------------------------

class S3TestRequest(BaseModel):
    s3_bucket: str
    s3_region: str = "us-east-1"
    s3_prefix: str = "synapse"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_endpoint_url: str = ""


@router.post("/scale/test-s3")
async def test_s3_connection(body: S3TestRequest):
    from core.s3_storage import SynapseS3
    if not body.s3_bucket:
        raise HTTPException(400, detail="s3_bucket is required")
    client = SynapseS3(
        bucket=body.s3_bucket,
        region=body.s3_region,
        prefix=body.s3_prefix,
        access_key_id=body.s3_access_key_id,
        secret_access_key=body.s3_secret_access_key,
        endpoint_url=body.s3_endpoint_url,
    )
    return client.test_connection()


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

    from core.scale.config import get_scale_config
    cfg = get_scale_config()

    # ARQ stores pending jobs in a Redis sorted set (ZSET), not a list.
    # Sum across all shards when num_queue_shards > 1.
    queued = 0
    try:
        if cfg.num_queue_shards > 1:
            import asyncio
            shard_names = [f"synapse:orchestrations:{i}" for i in range(cfg.num_queue_shards)]
            counts = await asyncio.gather(*[redis.zcard(q) for q in shard_names], return_exceptions=True)
            queued = sum(c for c in counts if isinstance(c, int))
        else:
            queue_name = f"synapse:orchestrations:{cfg.default_tenant_id if cfg.enable_tenant_isolation else 'default'}"
            queued = await redis.zcard(queue_name) or 0
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


# ---------------------------------------------------------------------------
# Analytics & Run Dashboard
# ---------------------------------------------------------------------------

@router.get("/scale/analytics")
async def scale_analytics(request: Request):
    """Aggregated run analytics for the scale dashboard."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return {"available": False}

    try:
        from sqlalchemy import select, func
        from core.scale.models_db import OrchestrationRunDB, WorkerDB
        from datetime import datetime

        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        async with session_factory() as session:
            # Status breakdown
            r = await session.execute(
                select(OrchestrationRunDB.status, func.count().label("cnt"))
                .group_by(OrchestrationRunDB.status)
            )
            status_counts: dict = {row.status: row.cnt for row in r.all()}
            total_runs = sum(status_counts.values())
            completed = status_counts.get("completed", 0)
            success_rate = round(completed / total_runs * 100, 1) if total_runs else 0.0

            # Runs today
            r = await session.execute(
                select(func.count()).select_from(OrchestrationRunDB).where(
                    OrchestrationRunDB.created_at >= today_start
                )
            )
            runs_today = r.scalar() or 0

            # Avg cost (all time, non-null)
            r = await session.execute(
                select(func.avg(OrchestrationRunDB.total_cost_usd)).where(
                    OrchestrationRunDB.total_cost_usd.isnot(None)
                )
            )
            avg_cost_usd = round(float(r.scalar() or 0), 6)

            # Cost today
            r = await session.execute(
                select(func.sum(OrchestrationRunDB.total_cost_usd)).where(
                    OrchestrationRunDB.created_at >= today_start,
                    OrchestrationRunDB.total_cost_usd.isnot(None),
                )
            )
            total_cost_usd_today = round(float(r.scalar() or 0), 6)

            # Avg duration for completed runs (seconds)
            r = await session.execute(
                select(
                    func.avg(
                        func.extract("epoch", OrchestrationRunDB.ended_at)
                        - func.extract("epoch", OrchestrationRunDB.started_at)
                    )
                ).where(
                    OrchestrationRunDB.status == "completed",
                    OrchestrationRunDB.ended_at.isnot(None),
                    OrchestrationRunDB.started_at.isnot(None),
                )
            )
            avg_duration_seconds = round(float(r.scalar() or 0), 1)

            # Cache hit rate: cache_read_tokens / total_tokens_used
            r = await session.execute(
                select(
                    func.sum(OrchestrationRunDB.cache_read_tokens).label("cache_reads"),
                    func.sum(OrchestrationRunDB.total_tokens_used).label("total_tokens"),
                ).where(OrchestrationRunDB.total_tokens_used.isnot(None))
            )
            row = r.one()
            cache_reads = int(row.cache_reads or 0)
            total_tokens = int(row.total_tokens or 0)
            cache_hit_rate = round(cache_reads / total_tokens * 100, 1) if total_tokens else 0.0

            # Workers online
            r = await session.execute(
                select(func.count()).select_from(WorkerDB).where(WorkerDB.status == "online")
            )
            workers_online = r.scalar() or 0

        return {
            "available": True,
            "total_runs": total_runs,
            "runs_today": runs_today,
            "status_counts": status_counts,
            "success_rate": success_rate,
            "avg_cost_usd": avg_cost_usd,
            "total_cost_usd_today": total_cost_usd_today,
            "avg_duration_seconds": avg_duration_seconds,
            "cache_hit_rate": cache_hit_rate,
            "workers_online": workers_online,
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


@router.get("/scale/runs")
async def list_scale_runs(request: Request, limit: int = 20, session_id: str = None):
    """List recent orchestration runs from Postgres for the scale dashboard."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        return []

    try:
        from sqlalchemy import select
        from core.scale.models_db import OrchestrationRunDB

        async with session_factory() as session:
            q = (
                select(OrchestrationRunDB)
                .order_by(OrchestrationRunDB.created_at.desc())
                .limit(min(limit, 100))
            )
            if session_id:
                q = q.where(OrchestrationRunDB.session_id == session_id)
            result = await session.execute(q)
            rows = result.scalars().all()

        return [
            {
                "run_id": r.run_id,
                "orchestration_id": r.orchestration_id,
                "session_id": r.session_id,
                "tenant_id": r.tenant_id,
                "status": r.status,
                "started_at": str(r.started_at) if r.started_at else None,
                "ended_at": str(r.ended_at) if r.ended_at else None,
                "total_cost_usd": r.total_cost_usd,
                "total_tokens_used": r.total_tokens_used,
                "worker_id": r.worker_id,
            }
            for r in rows
        ]
    except Exception:
        return []


@router.get("/scale/runs/{run_id}")
async def get_scale_run(run_id: str, request: Request):
    """Fetch a single orchestration run by run_id (for the search feature)."""
    session_factory = getattr(request.app.state, "pg_session_factory", None)
    if not session_factory:
        raise HTTPException(503, detail="Postgres not configured")

    try:
        from sqlalchemy import select
        from core.scale.models_db import OrchestrationRunDB

        async with session_factory() as session:
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()

        if not row:
            raise HTTPException(404, detail=f"Run '{run_id}' not found")

        return {
            "run_id": row.run_id,
            "orchestration_id": row.orchestration_id,
            "session_id": row.session_id,
            "tenant_id": row.tenant_id,
            "status": row.status,
            "started_at": str(row.started_at) if row.started_at else None,
            "ended_at": str(row.ended_at) if row.ended_at else None,
            "total_cost_usd": row.total_cost_usd,
            "total_tokens_used": row.total_tokens_used,
            "worker_id": row.worker_id,
            "current_step_id": row.current_step_id,
            "waiting_for_human": row.waiting_for_human,
            "human_prompt": row.human_prompt,
            "cache_hit_count": row.cache_hit_count,
            "estimated_savings_usd": row.estimated_savings_usd,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))
