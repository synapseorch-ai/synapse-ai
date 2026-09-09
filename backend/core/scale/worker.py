"""
ARQ worker definition.
Defines the job functions and WorkerSettings class consumed by `arq run_worker`.
Entry point: backend/worker_main.py
"""
import asyncio
import contextlib
import functools
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

from core import settings_runtime
from core.scale import mcp_pool
from core.scale.pubsub import clear_chat_cancellation, is_chat_cancelled
from core.scale.config import QUEUE_NAME, get_scale_config
from core.tenancy import get_tenant, tenant_scope

from core.log import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared worker context (populated in on_startup, used in job functions)
# ---------------------------------------------------------------------------

_worker_id: str = ""
_worker_address: str = ""
_active_jobs: int = 0        # approximate counter updated by jobs


def _get_active_jobs() -> int:
    return _active_jobs


def _for_the_jobs_tenant(fn):
    """Establish the job's tenant around the handler, if an embedder said how.

    The tenant has to be in the context before the body runs: `_load_job_settings`
    reads it, `mcp_pool.acquire()` keys on it, and the run row is written with
    it. A decorator rather than a line inside each handler so there is one place
    it can be got wrong, and so `functools.wraps` keeps the signature the four
    handlers advertise — `tests/unit/test_tenancy.py` inspects it to assert none
    of them takes a `tenant_id`, and that has to keep meaning something.

    In the shipped single-tenant product no resolver is registered, this returns
    None, and the handler runs exactly as before.
    """
    @functools.wraps(fn)
    async def wrapper(ctx: dict, *args, **kwargs):
        from core.scale.context import resolve_job_tenant

        tenant = await resolve_job_tenant(ctx, fn.__name__, args, kwargs)
        if tenant is None:
            return await fn(ctx, *args, **kwargs)
        with tenant_scope(tenant):
            return await fn(ctx, *args, **kwargs)

    return wrapper


async def _load_job_settings() -> None:
    """Read the current tenant's settings into the job's context.

    Called at the top of every job function, after the tenant is established.
    Once per job rather than per step: a long run would otherwise re-read the
    database at every `load_settings()` call, of which there are ~16 per step —
    hence `force=True`, which skips the staleness check the HTTP path wants and
    always reads.

    The machinery lives in `core.settings_runtime`; this is just where a job
    happens to be the boundary. `load_settings()` stays synchronous either way.
    """
    from core import settings_runtime

    await settings_runtime.bind(force=True)


# ---------------------------------------------------------------------------
# Job: run an orchestration
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def run_orchestration_job(
    ctx: dict,
    run_id: str,
    orch_id: str,
    initial_input: str,
    session_id: str | None = None,
    initial_state: dict | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    api_key_id: str | None = None,
) -> dict:
    """Pull an orchestration definition from the store, run it, publish events.

    There is deliberately no `tenant_id` parameter. It used to be taken from
    the job payload and written straight onto the run row, which meant any
    caller that could enqueue a job could label its run with any tenant it
    liked. The tenant now comes from the execution context — see
    core/tenancy.py — which an embedder establishes around the call and which
    nothing in the shipped single-tenant product can change.
    """
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False

    try:
        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True
        # Mark run as picked up in Postgres (UPSERT so DLQ retries work even
        # without a pre-existing row created by the V2 API enqueue path).
        async with session_factory() as session:
            from core.store import upsert
            from core.store.models import OrchestrationRunDB
            now = datetime.now(timezone.utc)
            await upsert(
                session,
                OrchestrationRunDB,
                values={
                    "run_id": run_id,
                    "orchestration_id": orch_id,
                    "status": "running",
                    "worker_id": _worker_id,
                    "job_id": ctx.get("job_id", ""),
                    "tenant_id": get_tenant(),
                    "started_at": now,
                },
                index_elements=["run_id"],
                # A retry must not rewrite the tenant or the start time.
                update=["worker_id", "job_id", "status"],
            )
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
        _log.debug(f"[worker] run_orchestration_job ERROR {run_id}: {exc}\n{tb}")

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
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume after human input
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
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
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False

    try:
        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True
        _log.debug(f"[resume] ▶ job picked up run_id={run_id} human_response_keys={list(human_response.keys()) if isinstance(human_response, dict) else type(human_response).__name__}")

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
            _log.debug(f"[resume] 📋 loaded run from DB: orch_id={orch_id} status={row.status} current_step_id={row.current_step_id} waiting_for_human={row.waiting_for_human}")

        orch = await _load_orchestration(session_factory, orch_id)
        _log.debug(f"[resume] 📦 orchestration loaded from Postgres: id={orch.id} name={orch.name} steps={len(orch.steps)}")

        from core.scale.pubsub import RunEventPublisher
        publisher = RunEventPublisher(redis, run_id, ttl=cfg.pubsub_event_ttl)

        # Mark run as picked up: transition paused → running so status polling reflects activity
        async with session_factory() as session:
            from sqlalchemy import update
            from core.scale.models_db import OrchestrationRunDB
            await session.execute(
                update(OrchestrationRunDB)
                .where(OrchestrationRunDB.run_id == run_id)
                .values(
                    status="running",
                    waiting_for_human=False,
                    worker_id=_worker_id,
                    job_id=ctx.get("job_id", ""),
                )
            )
            await session.commit()
        _log.debug(f"[resume] ✅ DB status set to 'running', waiting_for_human=False")

        await publisher.publish({"type": "worker_picked_up", "worker_id": _worker_id, "run_id": run_id})

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
        _log.debug(f"[resume] 🔄 calling adapter.resume() ...")
        final_status = await adapter.resume(human_response=human_response)
        _log.debug(f"[resume] 🏁 adapter.resume() returned final_status={final_status}")

        if webhook_url and final_status != "paused":
            await _deliver_webhook_for_run(
                run_id=run_id,
                session_factory=session_factory,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret,
            )

        return {"status": final_status, "run_id": run_id}

    except Exception as exc:
        _log.debug(f"[worker] resume_orchestration_job ERROR {run_id}: {exc}")

        # Mirror run_orchestration_job: mark failed in Postgres so it doesn't stay "paused"
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

        try:
            from core.scale.pubsub import RunEventPublisher
            _pub = RunEventPublisher(redis, run_id)
            await _pub.publish({"type": "orchestration_error", "error": str(exc)})
            await _pub.publish_done()
        except Exception:
            pass

        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


# ---------------------------------------------------------------------------
# Job: resume a failed run
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def resume_failed_job(ctx: dict, run_id: str) -> dict:
    """Retry a failed or cancelled orchestration run from its last checkpoint."""
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False

    try:
        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True
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
        _log.debug(f"[worker] resume_failed_job ERROR {run_id}: {exc}")
        raise

    finally:
        if leased:
            await mcp_pool.release()
        _active_jobs = max(0, _active_jobs - 1)


#: How often a running chat turn looks for its stop signal. Matches the run
#: watcher's interval — the person is waiting either way.
CHAT_CANCEL_POLL_SECONDS = 2.0


async def _run_chat_turn_cancellably(redis, session_id: str, consume) -> bool:
    """Run `consume()` under a cancel watcher. True if it was stopped.

    Lifted from `WorkerEngineAdapter._cancel_guarded`, which does this for
    orchestration runs, and kept deliberately close to it: the polling
    interval, the `uncancel()` and the "was this cancel ours?" question are the
    parts that are easy to get subtly wrong.

    `task.uncancel()` is the subtle one. Without it the outstanding
    cancellation count stays raised, and every `await` in the caller's cleanup
    — persisting the partial turn, publishing the terminal event — is cancelled
    again the moment it starts, so a stopped turn would leave exactly the silent
    dead stream that stopping it is meant to avoid.

    A cancel that is not ours (an ARQ timeout, a worker shutting down) is not a
    request: it propagates, and the job is retried or abandoned as it always was.
    """
    driver = asyncio.create_task(consume())
    stop = asyncio.Event()

    async def watch():
        while not stop.is_set():
            try:
                if await is_chat_cancelled(redis, session_id):
                    driver.cancel()
                    return
            except Exception:  # noqa: BLE001 — a Redis hiccup is not a cancel
                pass
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CHAT_CANCEL_POLL_SECONDS)

    watcher = asyncio.create_task(watch())
    try:
        await driver
        return False
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and hasattr(task, "uncancel"):
            task.uncancel()
        if not await is_chat_cancelled(redis, session_id):
            raise
        return True
    finally:
        stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


# ---------------------------------------------------------------------------
# Job: run an agent chat turn
# ---------------------------------------------------------------------------

@_for_the_jobs_tenant
async def run_agent_chat_job(
    ctx: dict,
    session_id: str,
    agent_id: str | None,
    message: str,
    images: list | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> dict:
    """Run a single agent chat turn, publish events, persist session.

    No `tenant_id` parameter — see `run_orchestration_job`.
    """
    global _active_jobs
    _active_jobs += 1
    cfg = get_scale_config()
    await _load_job_settings()

    redis = ctx["redis"]
    session_factory = ctx["session_factory"]
    leased = False
    images = images or []

    # Set before the `try`, so the `except` can tell "we never got far enough to
    # have a publisher" from "the turn failed with one". See below.
    publisher = None

    try:
        # Everything that makes this turn *visible* happens before the pool is
        # acquired, and the ordering is the point.
        #
        # `mcp_pool.acquire()` can block for a long time — it builds this
        # tenant's MCP sessions, and a remote server that accepts a connection
        # and then goes quiet holds it. With the acquire first, a turn wedged
        # there wrote no session row and published no event: no trace anywhere
        # except `j_ongoing` in a health line, for up to `job_timeout` (an
        # hour). Observed for real on 2026-08-25 — acme's job `25698d0f` sat
        # in-progress for forty minutes with zero `chat_%` rows for the org.
        #
        # These three reads and the publisher need only Postgres and Redis, so
        # there is nothing to trade away by doing them first. A wedged turn is
        # now a `running` row a staff console or a support reply can see.
        history = await _load_chat_history(session_factory, session_id, agent_id)
        await _upsert_chat_session(session_factory, session_id, agent_id, "running", history)

        from core.scale.pubsub import ChatEventPublisher
        publisher = ChatEventPublisher(redis, session_id, ttl=cfg.pubsub_event_ttl)

        # This tenant's MCP session set, from the bounded pool. It merges the
        # process-wide native servers with the tenant's own, so what the engine
        # sees is the shape `ctx["server_module"]` used to be — except that it
        # is this tenant's, not whoever's was current at worker startup.
        server_module = await mcp_pool.acquire()
        leased = True

        # No local materialisation: core/session.py reads the same rows this
        # job just loaded. The shim that used to live here wrote a session file
        # onto this worker's own disk, so a conversation's history followed
        # whichever worker won the job — and it wrote an empty cli_session_ids,
        # discarding any CLI session this worker was resuming.

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
        async def _consume():
            """Drive the loop. Separated so the cancel watcher has a task to
            cancel that is not the whole job."""
            nonlocal response_text, total_cost
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

        # A chat turn can now be stopped, the same way a run can.
        #
        # The engine checks a stop flag between turns, and between turns is not
        # where a turn spends its time: it is inside one LLM call, or a
        # twenty-minute tool, or one of the continuation retries the loop makes
        # when a response comes back truncated. So the watcher cancels the task
        # driving the engine rather than asking it to notice — the same
        # `_cancel_guarded` shape `WorkerEngineAdapter` uses for runs, keyed on
        # the session because a chat turn has no run id.
        cancelled = await _run_chat_turn_cancellably(redis, session_id, _consume)

        if cancelled:
            # Say so on the stream before closing it. A client that hangs when
            # the server stops talking is a client bug; a server that stops
            # talking without saying why is a server bug, and this is that half
            # — the same rule the `except` below already follows.
            await publisher.publish({"type": "cancelled", "message": "Stopped."})
        await publisher.publish_done()

        # Append to message history and persist.
        #
        # A cancelled turn is persisted too, with whatever the agent had
        # already said. Dropping it would mean the Stop button destroyed the
        # exchange rather than ending it — and Retry would have nothing to
        # retry from.
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response_text})
        await _upsert_chat_session(session_factory, session_id, agent_id,
                                   "cancelled" if cancelled else "completed", history)
        if cancelled:
            await clear_chat_cancellation(redis, session_id)
            return {"status": "cancelled", "session_id": session_id}

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
        _log.debug(f"[worker] run_agent_chat_job ERROR {session_id}: {exc}\n{tb}")

        # Say so on the stream before failing.
        #
        # This used to mark the row `failed` and re-raise without publishing
        # anything, so a turn that died outside the ReAct loop — a recycled
        # worker, an MCP pool failure, anything before the loop starts — left
        # the Redis stream open with nothing further to say. The API went on
        # sending keepalives because it was still waiting for `done`, and the
        # browser spun forever. A client that hangs when the server stops
        # talking is a client bug; a server that stops talking without saying
        # why is a server bug, and this is that half.
        #
        # Best-effort: a Redis that is already unreachable must not replace the
        # real error with a publishing one, and the `failed` row below is the
        # durable record either way.
        if publisher is not None:
            try:
                await publisher.publish({"type": "error", "message": str(exc)})
                await publisher.publish_done()
            except Exception:
                pass

        try:
            # `history` — never `[]`. This wrote an empty list into the
            # `messages` column, and `_upsert_chat_session` lists `messages`
            # among the columns it updates, so **every failed turn erased the
            # whole conversation**. The row was described here as "the durable
            # record"; it was the opposite. `history` holds the turns loaded at
            # the top of this job, so the exchange survives its own failure and
            # there is something left to retry from.
            await _upsert_chat_session(session_factory, session_id, agent_id, "failed", history)
        except Exception:
            pass
        raise

    finally:
        if leased:
            await mcp_pool.release()
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

    _log.debug(f"[worker] starting up as {_worker_id} @ {_worker_address}")

    # Build the store's engine and session factory
    from core.store import build_engine, build_session_factory, init_db, set_store
    pg_engine = build_engine(cfg.postgres_url, pgbouncer_mode=cfg.pgbouncer_mode)
    await init_db(pg_engine)
    session_factory = build_session_factory(pg_engine)
    ctx["session_factory"] = session_factory
    ctx["pg_engine"] = pg_engine

    # Point the store at this engine, so the resolvers (agents, tools, MCP
    # servers) and the state backend reach the same Postgres the worker uses
    # rather than opening a second, SQLite one.
    set_store(session_factory)

    # Redis client (already provided by ARQ as ctx["redis"])
    redis = ctx["redis"]

    # Settings come from the store, per job, for the tenant in context.
    # Nothing is written to the process environment: a provider key there is
    # readable in /proc/<pid>/environ and inherited by every stdio MCP server
    # and sandbox the worker spawns.
    settings_runtime.install_provider()

    # The native tool servers, which are the same for every tenant. A tenant's
    # own MCP — and the Filesystem server, which is rooted at its vault — is
    # built per job by core.scale.mcp_pool and cached there.
    from core.scale.worker_server_module import WorkerServerModule
    server_module = await WorkerServerModule.build_shared()
    mcp_pool.set_shared(server_module)
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

    _log.debug(f"[worker] startup complete. MCP disabled: {server_module.mcp_disabled}")


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

    # Every tenant's sessions first, then the shared ones they borrow from.
    # `drain()` waits, because each entry closes on its own task and the process
    # exiting underneath one leaves an orphaned MCP subprocess behind.
    await mcp_pool.drain()

    server_module = ctx.get("server_module")
    if server_module:
        await server_module.close()

    pg_engine = ctx.get("pg_engine")
    if pg_engine:
        await pg_engine.dispose()

    _log.debug(f"[worker] shutdown complete")


# ---------------------------------------------------------------------------
# ARQ WorkerSettings
# ---------------------------------------------------------------------------

_cfg = get_scale_config()


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


async def _load_chat_history(session_factory, session_id: str, agent_id: str | None) -> list:
    """Load message history for this conversation.

    Scoped by agent as well as session: the same session id under a different
    agent is a different conversation, and this is the read that decides what
    the model is shown.
    """
    from sqlalchemy import select
    from core.store.models import ChatSessionDB

    async with session_factory() as session:
        result = await session.execute(
            select(ChatSessionDB).where(
                ChatSessionDB.tenant_id == get_tenant(),
                ChatSessionDB.session_id == session_id,
                ChatSessionDB.agent_id == (agent_id or "default"),
            )
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
    """Create or update a chat session row."""
    from core.store import upsert
    from core.store.models import ChatSessionDB

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        await upsert(
            session,
            ChatSessionDB,
            values={
                "session_id": session_id,
                "agent_id": agent_id or "default",
                "tenant_id": get_tenant(),
                "status": status,
                "messages": messages,
                "last_message_at": now,
                "worker_id": _worker_id,
            },
            index_elements=["tenant_id", "session_id", "agent_id"],
            update=["status", "messages", "last_message_at", "worker_id"],
        )
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
        _log.debug(f"[worker] webhook delivery failed for {run_id}: {e}")


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
        _log.debug(f"[worker] failed to write DLQ: {e}")
