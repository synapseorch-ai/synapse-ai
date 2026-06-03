"""
ARQ worker definition.
Defines the job functions and WorkerSettings class consumed by `arq run_worker`.
Entry point: backend/worker_main.py
"""
import asyncio
import json
import os
import socket
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from arq import ArqRedis
from arq.connections import RedisSettings

from core.scale.config import get_scale_config


# ---------------------------------------------------------------------------
# Shared worker context (populated in on_startup, used in job functions)
# ---------------------------------------------------------------------------

_worker_id: str = ""
_worker_address: str = ""
_active_jobs: int = 0        # approximate counter updated by jobs


def _get_active_jobs() -> int:
    return _active_jobs


# ---------------------------------------------------------------------------
# Job: run an orchestration
# ---------------------------------------------------------------------------

async def run_orchestration_job(
    ctx: dict,
    run_id: str,
    orch_id: str,
    initial_input: str,
    session_id: str | None = None,
    initial_state: dict | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    tenant_id: str | None = None,
    api_key_id: str | None = None,
) -> dict:
    """Pull an orchestration definition from Postgres, run it, publish events."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    server_module = ctx["server_module"]

    try:
        # Mark run as picked up in Postgres (UPSERT so DLQ retries work even
        # without a pre-existing row created by the V2 API enqueue path).
        async with session_factory() as session:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from core.scale.models_db import OrchestrationRunDB
            now = datetime.now(timezone.utc)
            stmt = pg_insert(OrchestrationRunDB).values(
                run_id=run_id,
                orchestration_id=orch_id,
                status="running",
                worker_id=_worker_id,
                job_id=ctx.get("job_id", ""),
                tenant_id=tenant_id or cfg.default_tenant_id,
                started_at=now,
            ).on_conflict_do_update(
                index_elements=["run_id"],
                set_={
                    "worker_id": _worker_id,
                    "job_id": ctx.get("job_id", ""),
                    "status": "running",
                },
            )
            await session.execute(stmt)
            await session.commit()

        # Load orchestration definition from Postgres
        orch = await _load_orchestration(session_factory, orch_id)

        # Publish worker_picked_up event so SSE clients know execution started
        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)
        await publisher.publish({
            "type": "worker_picked_up",
            "worker_id": _worker_id,
            "run_id": run_id,
        })

        # Run the orchestration
        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
            job_id=ctx.get("job_id", ""),
        )
        final_status = await adapter.run(
            initial_input=initial_input,
            session_id=session_id,
            initial_state=initial_state,
        )

        # Deliver webhook if requested
        if webhook_url and final_status != "paused":
            await _deliver_webhook_for_run(
                run_id=run_id,
                session_factory=session_factory,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )

        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[worker] run_orchestration_job ERROR {run_id}: {exc}\n{tb}", flush=True)

        # Update run status in Postgres to failed
        try:
            async with session_factory() as session:
                from sqlalchemy import update
                from core.scale.models_db import OrchestrationRunDB
                await session.execute(
                    update(OrchestrationRunDB)
                    .where(OrchestrationRunDB.run_id == run_id)
                    .values(
                        status="failed",
                        ended_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
        except Exception:
            pass

        # Publish error event so SSE clients get notified
        try:
            from core.scale.pubsub import RunEventPublisher
            publisher = RunEventPublisher(redis, run_id)
            await publisher.publish({"type": "orchestration_error", "error": str(exc)})
            await publisher.publish_done()
        except Exception:
            pass

        # On final retry, write to DLQ
        attempt = ctx.get("job_try", 1)
        max_tries = int(os.getenv("WORKER_MAX_RETRIES", "3"))
        if attempt >= max_tries:
            await _write_dlq(
                session_factory=session_factory,
                run_id=run_id,
                orch_id=orch_id,
                job_function="run_orchestration_job",
                job_payload={
                    "run_id": run_id,
                    "orch_id": orch_id,
                    "initial_input": initial_input,
                },
                error_message=str(exc),
                error_traceback=tb,
                attempt_count=attempt,
            )

        raise  # ARQ will retry

    finally:
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume after human input
# ---------------------------------------------------------------------------

async def resume_orchestration_job(
    ctx: dict,
    run_id: str,
    human_response: dict | str,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> dict:
    """Resume a paused orchestration run after human input is submitted."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    server_module = ctx["server_module"]

    try:
        # Load run to find orchestration_id
        async with session_factory() as session:
            from core.scale.models_db import OrchestrationRunDB
            from sqlalchemy import select
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                raise ValueError(f"Run {run_id} not found in Postgres")
            orch_id = row.orchestration_id

        orch = await _load_orchestration(session_factory, orch_id)

        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)

        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
            job_id=ctx.get("job_id", ""),
        )
        final_status = await adapter.resume(human_response=human_response)

        if webhook_url and final_status != "paused":
            await _deliver_webhook_for_run(
                run_id=run_id,
                session_factory=session_factory,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )

        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        print(f"[worker] resume_orchestration_job ERROR {run_id}: {exc}", flush=True)
        raise

    finally:
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume a failed run
# ---------------------------------------------------------------------------

async def resume_failed_job(ctx: dict, run_id: str) -> dict:
    """Retry a failed or cancelled orchestration run from its last checkpoint."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    server_module = ctx["server_module"]

    try:
        async with session_factory() as session:
            from core.scale.models_db import OrchestrationRunDB
            from sqlalchemy import select
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()
            if not row:
                raise ValueError(f"Run {run_id} not found in Postgres")
            orch_id = row.orchestration_id

        orch = await _load_orchestration(session_factory, orch_id)

        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)

        from core.scale.worker_engine_adapter import WorkerEngineAdapter
        adapter = WorkerEngineAdapter(
            orch=orch,
            run_id=run_id,
            worker_server_module=server_module,
            publisher=publisher,
            session_factory=session_factory,
            redis_client=redis,
            worker_id=_worker_id,
        )
        final_status = await adapter.resume_failed()
        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        print(f"[worker] resume_failed_job ERROR {run_id}: {exc}", flush=True)
        raise

    finally:
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: run an agent chat turn
# ---------------------------------------------------------------------------

async def run_agent_chat_job(
    ctx: dict,
    session_id: str,
    agent_id: str | None,
    message: str,
    images: list | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Run a single agent chat turn, publish events, persist session."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    server_module = ctx["server_module"]
    images = images or []

    try:
        # Load agent definition from Postgres
        agent_data = await _load_agent(session_factory, agent_id) if agent_id else None
        # Load prior message history from Postgres chat_sessions
        history = await _load_chat_history(session_factory, session_id)

        # Mark session as running
        await _upsert_chat_session(session_factory, session_id, agent_id, "running", history)

        from core.scale.pubsub import ChatEventPublisher
        publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl)

        # Pre-populate local session file from Postgres history so run_react_loop
        # finds it via get_recent_history_messages(session_id, agent_id).
        # history is [{role, content}]; session file uses [{user, assistant}] turns.
        if history:
            from core.session import _write_session_file
            turns = []
            msgs = list(history)
            while len(msgs) >= 2 and msgs[0].get("role") == "user" and msgs[1].get("role") == "assistant":
                turns.append({
                    "user": msgs.pop(0).get("content", ""),
                    "assistant": msgs.pop(0).get("content", ""),
                    "tools": [],
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                })
            _write_session_file({
                "session_id": session_id,
                "agent_id": agent_id or "default",
                "turns": turns,
                "last_response": turns[-1]["assistant"] if turns else None,
                "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
                "cli_session_ids": {},
            }, session_id, agent_id)

        # Run the ReAct loop for this chat turn
        response_text = ""
        tool_calls_summary = []
        total_cost = 0.0

        from core.models import ChatRequest
        from core.react_engine import run_react_loop
        chat_request = ChatRequest(
            message=message,
            session_id=session_id,
            agent_id=agent_id,
            images=images or [],
        )
        async for event in run_react_loop(chat_request, server_module):
            await publisher.publish(event)
            # Capture the final response text and cost
            if event.get("type") in ("response", "final"):
                response_text = event.get("response", response_text)
            elif event.get("type") == "tool_execution":
                tool_calls_summary.append({
                    "tool": event.get("tool_name"),
                    "args": event.get("tool_args"),
                })
            elif event.get("type") == "done":
                total_cost = event.get("total_cost_usd", 0.0)

        await publisher.publish_done()

        # Append to message history and persist
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response_text})
        await _upsert_chat_session(session_factory, session_id, agent_id, "completed", history)

        # Deliver webhook if requested
        if webhook_url:
            from core.scale.webhook import deliver_webhook
            await deliver_webhook(
                webhook_url=webhook_url,
                payload={
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "status": "completed",
                    "message": {"role": "assistant", "content": response_text},
                    "tool_calls": tool_calls_summary,
                    "total_cost_usd": total_cost,
                },
                secret=webhook_secret,
            )

        return {"status": "completed", "session_id": session_id}

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[worker] run_agent_chat_job ERROR {session_id}: {exc}\n{tb}", flush=True)
        try:
            await _upsert_chat_session(session_factory, session_id, agent_id, "failed", [])
        except Exception:
            pass
        raise

    finally:
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Worker lifecycle hooks
# ---------------------------------------------------------------------------

async def worker_startup(ctx: dict) -> None:
    """Initialize Postgres, Redis, register worker, load LLM keys, start heartbeat."""
    global _worker_id, _worker_address

    cfg = get_scale_config()

    # Unique worker ID
    _worker_id = os.getenv("WORKER_ID", f"worker-{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
    _worker_address = f"http://{socket.gethostname()}:{os.getenv('WORKER_HEALTH_PORT', '9000')}"

    print(f"[worker] starting up as {_worker_id} @ {_worker_address}", flush=True)

    # Build Postgres engine and session factory
    from core.scale.db import build_engine, build_session_factory, init_db
    pg_engine = build_engine(cfg.postgres_url, pgbouncer_mode=cfg.pgbouncer_mode)
    await init_db(pg_engine)
    session_factory = build_session_factory(pg_engine)
    ctx["session_factory"] = session_factory
    ctx["pg_engine"] = pg_engine

    # Redis client (already provided by ARQ as ctx["redis"])
    redis = ctx["redis"]

    # Load LLM keys from Postgres
    async with session_factory() as session:
        from core.scale.llm_keys import load_llm_settings_from_pg, inject_llm_env
        settings = await load_llm_settings_from_pg(session)
        inject_llm_env(settings)

    # Build WorkerServerModule (connects to available tools/MCP servers)
    from core.scale.worker_server_module import WorkerServerModule
    server_module = await WorkerServerModule.build()
    ctx["server_module"] = server_module

    # Register worker in Postgres
    from core.scale.models_db import WorkerDB
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    async with session_factory() as session:
        stmt = pg_insert(WorkerDB).values(
            worker_id=_worker_id,
            hostname=socket.gethostname(),
            address=_worker_address,
            status="online",
            active_jobs=0,
            max_jobs=cfg.worker_concurrency,
            last_heartbeat=datetime.now(timezone.utc),
            mcp_disabled=server_module.mcp_disabled,
        ).on_conflict_do_update(
            index_elements=["worker_id"],
            set_={
                "hostname": socket.gethostname(),
                "address": _worker_address,
                "status": "online",
                "max_jobs": cfg.worker_concurrency,
                "last_heartbeat": datetime.now(timezone.utc),
                "mcp_disabled": server_module.mcp_disabled,
            },
        )
        await session.execute(stmt)
        await session.commit()

    # Start heartbeat background task
    from core.scale.heartbeat import run_heartbeat
    heartbeat_task = asyncio.create_task(
        run_heartbeat(
            worker_id=_worker_id,
            address=_worker_address,
            hostname=socket.gethostname(),
            redis_client=redis,
            session_factory=session_factory,
            get_active_jobs_fn=_get_active_jobs,
            max_jobs=cfg.worker_concurrency,
            mcp_disabled=server_module.mcp_disabled,
        )
    )
    ctx["heartbeat_task"] = heartbeat_task
    ctx["worker_id"] = _worker_id

    print(f"[worker] startup complete. MCP disabled: {server_module.mcp_disabled}", flush=True)


async def worker_shutdown(ctx: dict) -> None:
    """Mark worker offline, cancel heartbeat, close connections."""
    heartbeat_task = ctx.get("heartbeat_task")
    if heartbeat_task and not heartbeat_task.done():
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

    session_factory = ctx.get("session_factory")
    if session_factory and _worker_id:
        from core.scale.heartbeat import mark_worker_offline
        await mark_worker_offline(_worker_id, session_factory)

    server_module = ctx.get("server_module")
    if server_module:
        await server_module.close()

    pg_engine = ctx.get("pg_engine")
    if pg_engine:
        await pg_engine.dispose()

    print(f"[worker] shutdown complete", flush=True)


# ---------------------------------------------------------------------------
# ARQ WorkerSettings
# ---------------------------------------------------------------------------

_cfg = get_scale_config()

QUEUE_NAME = f"synapse:orchestrations:{os.getenv('WORKER_QUEUE_SHARD', 'default')}"


def _build_redis_settings() -> RedisSettings:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    # ARQ doesn't support Redis Cluster natively; use single-node URL for ARQ
    # and RedisCluster separately for event streaming.
    if redis_url.startswith("redis+cluster://"):
        stripped = redis_url.replace("redis+cluster://", "")
        first = stripped.split(",")[0]
        host, port = first.rsplit(":", 1)
        return RedisSettings(host=host, port=int(port))
    return RedisSettings.from_dsn(redis_url)


class WorkerSettings:
    functions = [
        run_orchestration_job,
        resume_orchestration_job,
        resume_failed_job,
        run_agent_chat_job,
    ]
    queue_name = QUEUE_NAME
    max_jobs = int(os.getenv("WORKER_CONCURRENCY", "10"))
    job_timeout = int(os.getenv("WORKER_JOB_TIMEOUT", "3600"))
    max_tries = int(os.getenv("WORKER_MAX_RETRIES", "3"))
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    redis_settings = _build_redis_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_orchestration(session_factory, orch_id: str):
    """Load orchestration definition from Postgres."""
    from sqlalchemy import select
    from core.scale.models_db import OrchestrationDB
    from core.models_orchestration import Orchestration

    async with session_factory() as session:
        result = await session.execute(
            select(OrchestrationDB).where(OrchestrationDB.id == orch_id)
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise ValueError(f"Orchestration '{orch_id}' not found in Postgres. Run Sync first.")

    return Orchestration.model_validate(row.definition)


async def _load_agent(session_factory, agent_id: str) -> dict | None:
    """Load agent definition dict from Postgres."""
    from sqlalchemy import select
    from core.scale.models_db import AgentDB

    async with session_factory() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.id == agent_id)
        )
        row = result.scalar_one_or_none()

    return row.definition if row else None


async def _load_chat_history(session_factory, session_id: str) -> list:
    """Load message history from Postgres chat_sessions."""
    from sqlalchemy import select
    from core.scale.models_db import ChatSessionDB

    async with session_factory() as session:
        result = await session.execute(
            select(ChatSessionDB).where(ChatSessionDB.session_id == session_id)
        )
        row = result.scalar_one_or_none()

    if row and row.messages:
        return list(row.messages)
    return []


async def _upsert_chat_session(
    session_factory,
    session_id: str,
    agent_id: str | None,
    status: str,
    messages: list,
) -> None:
    """Create or update a chat session row in Postgres."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from core.scale.models_db import ChatSessionDB

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        stmt = pg_insert(ChatSessionDB).values(
            session_id=session_id,
            agent_id=agent_id,
            status=status,
            messages=messages,
            last_message_at=now,
            worker_id=_worker_id,
        ).on_conflict_do_update(
            index_elements=["session_id"],
            set_={
                "agent_id": agent_id,
                "status": status,
                "messages": messages,
                "last_message_at": now,
                "worker_id": _worker_id,
            },
        )
        await session.execute(stmt)
        await session.commit()


async def _deliver_webhook_for_run(
    run_id: str,
    session_factory,
    webhook_url: str,
    webhook_secret: str | None,
) -> None:
    """Load run result from Postgres and deliver to webhook."""
    try:
        from sqlalchemy import select
        from core.scale.models_db import OrchestrationRunDB
        from core.scale.webhook import deliver_webhook

        async with session_factory() as session:
            result = await session.execute(
                select(OrchestrationRunDB).where(OrchestrationRunDB.run_id == run_id)
            )
            row = result.scalar_one_or_none()

        if not row:
            return

        await deliver_webhook(
            webhook_url=webhook_url,
            payload={
                "run_id": run_id,
                "orchestration_id": row.orchestration_id,
                "status": row.status,
                "started_at": str(row.started_at) if row.started_at else None,
                "ended_at": str(row.ended_at) if row.ended_at else None,
                "total_cost_usd": float(row.total_cost_usd or 0),
                "shared_state": row.shared_state or {},
                "error": None if row.status == "completed" else "Run did not complete",
            },
            secret=webhook_secret,
        )
    except Exception as e:
        print(f"[worker] webhook delivery failed for {run_id}: {e}", flush=True)


async def _write_dlq(
    session_factory,
    run_id: str,
    orch_id: str,
    job_function: str,
    job_payload: dict,
    error_message: str,
    error_traceback: str,
    attempt_count: int,
) -> None:
    """Write a failed job to the dead letter queue table."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from core.scale.models_db import DeadLetterQueueDB

    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            await session.execute(
                pg_insert(DeadLetterQueueDB).values(
                    run_id=run_id,
                    orchestration_id=orch_id,
                    job_function=job_function,
                    job_payload=job_payload,
                    error_message=error_message,
                    error_traceback=error_traceback,
                    attempt_count=attempt_count,
                    first_failed_at=now,
                    last_failed_at=now,
                )
            )
            await session.commit()
    except Exception as e:
        print(f"[worker] failed to write DLQ: {e}", flush=True)
