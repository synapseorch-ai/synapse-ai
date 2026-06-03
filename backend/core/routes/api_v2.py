"""
V2 External API Endpoints
--------------------------
Scalable async API that enqueues jobs to Redis ARQ workers instead of
running orchestrations in-process. All routes are protected by API key auth.

Differences from V1:
  - Orchestration runs return 202 immediately with run_id
  - Events delivered via Redis Streams (SSE with Last-Event-ID reconnect)
  - Webhooks supported for fire-and-forget patterns
  - Agent chat sessions backed by Postgres (persistent across server restarts)
  - Cancellation is distributed (works across multiple API servers + workers)

V1 API is completely unchanged.
"""
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.api_key_middleware import require_api_key

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response Models
# ---------------------------------------------------------------------------

class V2OrchestrationRunRequest(BaseModel):
    message: str = ""
    session_id: str | None = None
    priority: int = 0
    webhook_url: str | None = None
    webhook_secret: str | None = None
    tenant_id: str | None = None


class V2ResumeRequest(BaseModel):
    response: dict | str = {}
    webhook_url: str | None = None
    webhook_secret: str | None = None


class V2ChatRequest(BaseModel):
    message: str
    agent: str | None = None
    session_id: str | None = None
    images: list[str] = []
    priority: int = 0
    webhook_url: str | None = None
    webhook_secret: str | None = None
    tenant_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_scale_mode(request: Request):
    redis = getattr(request.app.state, "redis", None)
    if not redis:
        raise HTTPException(
            status_code=503,
            detail="Scale mode is not enabled. Set redis_url in Settings > Scale to use V2 API.",
        )
    return redis


def _get_arq_redis(request: Request):
    arq = getattr(request.app.state, "arq_redis", None)
    if not arq:
        raise HTTPException(503, detail="ARQ Redis connection not available.")
    return arq


def _get_pg_session_factory(request: Request):
    sf = getattr(request.app.state, "pg_session_factory", None)
    if not sf:
        raise HTTPException(503, detail="Postgres connection not available.")
    return sf


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:12]}_{int(time.time() * 1000)}"


def _new_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"


async def _check_tenant_quota(session_factory, tenant_id: str, redis) -> None:
    """Raise 429 if the tenant has exceeded their queued run limit."""
    try:
        from sqlalchemy import select, func
        from core.scale.models_db import OrchestrationRunDB, TenantDB

        async with session_factory() as session:
            # Get tenant limits (use defaults if tenant not found)
            t_result = await session.execute(
                select(TenantDB).where(TenantDB.tenant_id == tenant_id)
            )
            tenant = t_result.scalar_one_or_none()
            max_queued = tenant.max_queued_runs if tenant else 1000

            # Count active + queued runs for this tenant
            count_result = await session.execute(
                select(func.count()).select_from(OrchestrationRunDB).where(
                    OrchestrationRunDB.tenant_id == tenant_id,
                    OrchestrationRunDB.status.in_(["running", "queued", "paused"]),
                )
            )
            active_count = count_result.scalar() or 0

        if active_count >= max_queued:
            raise HTTPException(
                status_code=429,
                detail=f"Tenant '{tenant_id}' queue limit ({max_queued}) exceeded.",
                headers={"Retry-After": "30"},
            )
    except HTTPException:
        raise
    except Exception:
        pass  # Don't block runs due to quota check failures


async def _check_global_queue_depth(redis, queue_name: str, max_depth: int) -> None:
    """Raise 503 if the global queue depth exceeds the configured maximum."""
    try:
        depth = await redis.llen(queue_name)
        if depth >= max_depth:
            raise HTTPException(
                status_code=503,
                detail="System at capacity. Please retry later.",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception:
        pass


async def _check_rate_limit(redis, tenant_id: str, max_rps: int) -> None:
    """Sliding-window rate limit per tenant using Redis INCR + EXPIRE.
    Raises 429 when tenant exceeds max_rps requests per second."""
    if max_rps <= 0:
        return
    try:
        import time as _time
        window_key = f"synapse:ratelimit:{tenant_id}:{int(_time.time())}"
        count = await redis.incr(window_key)
        if count == 1:
            await redis.expire(window_key, 2)  # 2s TTL covers the 1s window + lag
        if count > max_rps:
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded.",
                headers={
                    "X-RateLimit-Limit": str(max_rps),
                    "X-RateLimit-Remaining": "0",
                    "Retry-After": "1",
                },
            )
    except HTTPException:
        raise
    except Exception:
        pass


async def _create_run_row(session_factory, run_id: str, orch_id: str, tenant_id: str) -> None:
    """Pre-create the orchestration_runs row so status polling works immediately."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from core.scale.models_db import OrchestrationRunDB

    async with session_factory() as session:
        stmt = pg_insert(OrchestrationRunDB).values(
            run_id=run_id,
            orchestration_id=orch_id,
            tenant_id=tenant_id,
            status="queued",
            shared_state={},
            step_history=[],
            started_at=datetime.now(timezone.utc),
        ).on_conflict_do_nothing()
        await session.execute(stmt)
        await session.commit()


# ---------------------------------------------------------------------------
# Orchestration endpoints
# ---------------------------------------------------------------------------

@router.get("/orchestrations")
async def v2_list_orchestrations(
    request: Request,
    _: dict = Depends(require_api_key),
):
    """List all orchestration definitions from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import OrchestrationDB

    async with sf() as session:
        result = await session.execute(
            select(OrchestrationDB.id, OrchestrationDB.name, OrchestrationDB.description, OrchestrationDB.updated_at)
        )
        rows = result.all()

    return [
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "updated_at": str(r.updated_at) if r.updated_at else None,
        }
        for r in rows
    ]


@router.get("/orchestrations/{orch_id}")
async def v2_get_orchestration(
    orch_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Get a single orchestration definition from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import OrchestrationDB

    async with sf() as session:
        result = await session.execute(
            select(OrchestrationDB).where(OrchestrationDB.id == orch_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(404, detail=f"Orchestration '{orch_id}' not found.")
    return row.definition


@router.post("/orchestrations/{orch_id}/run", status_code=202)
async def v2_run_orchestration(
    orch_id: str,
    body: V2OrchestrationRunRequest,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """
    Enqueue an orchestration run. Returns immediately with run_id.
    Worker picks up the job from Redis ARQ queue.
    """
    redis = _require_scale_mode(request)
    arq_redis = _get_arq_redis(request)
    sf = _get_pg_session_factory(request)

    from core.scale.config import get_scale_config
    cfg = get_scale_config()

    tenant_id = body.tenant_id or cfg.default_tenant_id
    queue_name = f"synapse:orchestrations:{tenant_id}" if cfg.enable_tenant_isolation else f"synapse:orchestrations:{os.getenv('WORKER_QUEUE_SHARD', 'default')}"

    # Rate limit + quota + backpressure checks
    await _check_rate_limit(redis, tenant_id, cfg.rate_limit_per_tenant_rps)
    if cfg.enable_tenant_isolation:
        await _check_tenant_quota(sf, tenant_id, redis)
    await _check_global_queue_depth(redis, queue_name, cfg.max_global_queue_depth)

    run_id = _new_run_id()

    # Pre-create run row so status polling works immediately
    await _create_run_row(sf, run_id, orch_id, tenant_id)

    # Enqueue to ARQ
    await arq_redis.enqueue_job(
        "run_orchestration_job",
        run_id=run_id,
        orch_id=orch_id,
        initial_input=body.message,
        session_id=body.session_id,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        tenant_id=tenant_id,
        _queue_name=queue_name,
        _job_id=run_id,
    )

    # Record metric
    from core.scale.metrics import record_run_enqueued
    record_run_enqueued(tenant_id=tenant_id, orch_id=orch_id)

    return {
        "run_id": run_id,
        "status": "queued",
        "stream_url": f"/api/v2/orchestrations/runs/{run_id}/stream",
        "status_url": f"/api/v2/orchestrations/runs/{run_id}/status",
    }


@router.get("/orchestrations/runs/{run_id}/status")
async def v2_run_status(
    run_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Poll run status from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import OrchestrationRunDB

    async with sf() as session:
        result = await session.execute(
            select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(404, detail=f"Run '{run_id}' not found.")

    return {
        "run_id": row.run_id,
        "orchestration_id": row.orchestration_id,
        "status": row.status,
        "current_step_id": row.current_step_id,
        "waiting_for_human": row.waiting_for_human,
        "worker_id": row.worker_id,
        "total_cost_usd": float(row.total_cost_usd or 0),
        "total_tokens_used": row.total_tokens_used or 0,
        "started_at": str(row.started_at) if row.started_at else None,
        "ended_at": str(row.ended_at) if row.ended_at else None,
    }


@router.get("/orchestrations/runs/{run_id}/stream")
async def v2_stream_run(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    _: dict = Depends(require_api_key),
):
    """
    SSE stream for a run. Subscribes to Redis Stream and forwards events.
    Supports reconnect via Last-Event-ID header — missed events are replayed.
    """
    redis = _require_scale_mode(request)

    from core.scale.event_bridge import stream_run_events

    return StreamingResponse(
        stream_run_events(redis, run_id, last_event_id or "0"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/orchestrations/runs/{run_id}/cancel")
async def v2_cancel_run(
    run_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Publish a distributed cancellation signal for the run."""
    redis = _require_scale_mode(request)

    from core.scale.pubsub import publish_cancellation
    await publish_cancellation(redis, run_id)

    return {"status": "cancellation_requested", "run_id": run_id}


@router.post("/orchestrations/runs/{run_id}/resume", status_code=202)
async def v2_resume_run(
    run_id: str,
    body: V2ResumeRequest,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Enqueue a resume job for a paused run (after human input)."""
    redis = _require_scale_mode(request)
    arq_redis = _get_arq_redis(request)
    sf = _get_pg_session_factory(request)

    # Publish human input to Redis so workers polling for it can pick it up
    from core.scale.pubsub import publish_human_input
    resp = body.response
    if isinstance(resp, str):
        resp = {"response": resp}
    await publish_human_input(redis, run_id, resp)

    # Also enqueue a resume job in case the worker already returned
    await arq_redis.enqueue_job(
        "resume_orchestration_job",
        run_id=run_id,
        human_response=resp,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        _job_id=f"resume_{run_id}_{int(time.time())}",
    )

    return {
        "run_id": run_id,
        "status": "resuming",
        "stream_url": f"/api/v2/orchestrations/runs/{run_id}/stream",
    }


# ---------------------------------------------------------------------------
# Agent / Chat endpoints
# ---------------------------------------------------------------------------

@router.get("/agents")
async def v2_list_agents(
    request: Request,
    _: dict = Depends(require_api_key),
):
    """List all agent definitions from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import AgentDB

    async with sf() as session:
        result = await session.execute(
            select(AgentDB.id, AgentDB.name, AgentDB.updated_at)
        )
        rows = result.all()

    return [{"id": r.id, "name": r.name, "updated_at": str(r.updated_at) if r.updated_at else None} for r in rows]


@router.get("/agents/{agent_id}")
async def v2_get_agent(
    agent_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Get a single agent definition from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import AgentDB

    async with sf() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.id == agent_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        raise HTTPException(404, detail=f"Agent '{agent_id}' not found.")
    return row.definition


@router.post("/chat", status_code=202)
async def v2_chat(
    body: V2ChatRequest,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """
    Enqueue an agent chat turn. Returns immediately with session_id.
    Worker runs the ReAct loop and publishes events to a Redis Stream.
    """
    redis = _require_scale_mode(request)
    arq_redis = _get_arq_redis(request)

    from core.scale.config import get_scale_config
    cfg = get_scale_config()

    session_id = body.session_id or _new_session_id()
    tenant_id = body.tenant_id or cfg.default_tenant_id
    queue_name = f"synapse:orchestrations:{os.getenv('WORKER_QUEUE_SHARD', 'default')}"

    await arq_redis.enqueue_job(
        "run_agent_chat_job",
        session_id=session_id,
        agent_id=body.agent,
        message=body.message,
        images=body.images,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        tenant_id=tenant_id,
        _queue_name=queue_name,
        _job_id=f"chat_{session_id}_{int(time.time())}",
    )

    return {
        "session_id": session_id,
        "status": "queued",
        "stream_url": f"/api/v2/chat/{session_id}/stream",
        "status_url": f"/api/v2/chat/{session_id}/status",
    }


@router.get("/chat/{session_id}/stream")
async def v2_chat_stream(
    session_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    _: dict = Depends(require_api_key),
):
    """SSE stream for a chat session. Supports reconnect via Last-Event-ID."""
    redis = _require_scale_mode(request)

    from core.scale.event_bridge import stream_chat_events

    return StreamingResponse(
        stream_chat_events(redis, session_id, last_event_id or "0"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/chat/{session_id}/status")
async def v2_chat_status(
    session_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Poll chat session status and message history from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import ChatSessionDB

    async with sf() as session:
        result = await session.execute(
            select(ChatSessionDB).where(ChatSessionDB.session_id == session_id)
        )
        row = result.scalar_one_or_none()

    if not row:
        return {"session_id": session_id, "status": "not_found", "messages": []}

    return {
        "session_id": row.session_id,
        "agent_id": row.agent_id,
        "status": row.status,
        "messages": row.messages or [],
        "last_message_at": str(row.last_message_at) if row.last_message_at else None,
        "worker_id": row.worker_id,
    }


@router.post("/chat/{session_id}/cancel")
async def v2_cancel_chat(
    session_id: str,
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Publish a cancellation signal for a chat session."""
    redis = _require_scale_mode(request)

    await redis.set(f"synapse:cancel:chat:{session_id}", "1", ex=3600)
    return {"status": "cancellation_requested", "session_id": session_id}


# ---------------------------------------------------------------------------
# Workers + Queue stats
# ---------------------------------------------------------------------------

@router.get("/workers")
async def v2_list_workers(
    request: Request,
    _: dict = Depends(require_api_key),
):
    """List all registered workers from Postgres."""
    _require_scale_mode(request)
    sf = _get_pg_session_factory(request)

    from sqlalchemy import select
    from core.scale.models_db import WorkerDB

    async with sf() as session:
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


@router.get("/queue/stats")
async def v2_queue_stats(
    request: Request,
    _: dict = Depends(require_api_key),
):
    """Return ARQ queue depth and active job counts from Redis."""
    redis = _require_scale_mode(request)

    from core.scale.config import get_scale_config
    cfg = get_scale_config()
    queue_name = f"synapse:orchestrations:{os.getenv('WORKER_QUEUE_SHARD', 'default')}"

    try:
        queued = await redis.llen(queue_name) or 0
    except Exception:
        queued = 0

    # Count active runs in Postgres
    try:
        sf = _get_pg_session_factory(request)
        from sqlalchemy import select, func
        from core.scale.models_db import OrchestrationRunDB
        async with sf() as session:
            r = await session.execute(
                select(func.count()).select_from(OrchestrationRunDB).where(
                    OrchestrationRunDB.status == "running"
                )
            )
            active = r.scalar() or 0
    except Exception:
        active = 0

    return {
        "queue_name": queue_name,
        "queued": queued,
        "active": active,
    }


import os


# ---------------------------------------------------------------------------
# Prometheus /metrics endpoint (enterprise observability)
# ---------------------------------------------------------------------------

@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    """Prometheus scrape endpoint. Protected by METRICS_TOKEN env var if set."""
    from fastapi.responses import Response as FastAPIResponse

    token = os.getenv("METRICS_TOKEN", "")
    if token:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {token}":
            raise HTTPException(401, detail="Unauthorized")

    from core.scale.metrics import get_metrics_response
    content, content_type = get_metrics_response()
    if content is None:
        raise HTTPException(503, detail="prometheus_client not installed")
    return FastAPIResponse(content=content, media_type=content_type)
