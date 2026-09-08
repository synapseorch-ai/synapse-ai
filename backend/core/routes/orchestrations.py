"""
Orchestration management endpoints: CRUD, run, human-input, cancel.
"""
import asyncio
import os
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.models_orchestration import Orchestration
from core.react_engine import drain_queue_with_heartbeat
# Registry of active run tasks (for mid-step cancellation), owned by the runner
# so non-route callers can register runs too.
from core.orchestration.runner import SENTINEL, _active_tasks, spawn_engine_run

router = APIRouter()

async def load_orchestrations() -> list[dict]:
    from core.store.resources import load_orchestrations as _load
    return await _load()


async def save_orchestrations(data: list[dict]):
    """Replace this tenant's orchestrations with `data`.

    Bulk replace, for import and seeding. CRUD routes use save_orchestration /
    delete_orchestration instead: read-list, mutate, write-list loses a
    concurrent edit, and passing an empty list here deletes everything.
    """
    from core.store.resources import replace_orchestrations
    await replace_orchestrations(data)


# ── CRUD ──────────────────────────────────────────────────────────

@router.get("/api/orchestrations")
async def list_orchestrations():
    return await load_orchestrations()


@router.get("/api/orchestrations/runs")
async def list_runs(limit: int = 20):
    """List recent orchestration runs (newest first).

    Runs whose orchestration has since been deleted are omitted — the UI
    cannot open them (there is no definition to render), so listing them
    would only produce rows that do nothing when clicked.
    """
    from core.orchestration.state import SharedState
    known_ids = {o["id"] for o in await load_orchestrations() if o.get("id")}
    return await SharedState.list_runs(limit=max(1, min(limit, 200)), orchestration_ids=known_ids)


async def _run_summary(run_id: str) -> dict | None:
    """Small status snapshot from the checkpoint (None if no checkpoint yet)."""
    from core.orchestration.state import SharedState
    try:
        run = (await SharedState.restore(run_id)).run
        return {
            "run_id": run.run_id,
            "orchestration_id": run.orchestration_id,
            "status": run.status,
            "current_step_id": run.current_step_id,
            "waiting_for_human": run.waiting_for_human,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
        }
    except FileNotFoundError:
        return None


async def _run_is_terminal(run_id: str) -> bool:
    summary = await _run_summary(run_id)
    return summary is not None and summary["status"] in ("completed", "failed", "cancelled")


# Seconds between keepalive comments while tailing an idle run (patchable in tests).
_TAIL_KEEPALIVE_SECONDS = 15


@router.get("/api/orchestrations/runs/{run_id}/events")
async def get_run_events(run_id: str, request: Request, after: int = 0, limit: int = 5000):
    """Replay a run's journaled events (id > `after`), oldest first.

    Standalone runs read the JSONL journal; V2/worker runs (no journal file)
    fall back to the Redis Stream the scale workers publish to.
    """
    from core.orchestration.journal import FileRunJournal, open_journal

    # A run still being pumped may have lines queued for the writer thread;
    # wait for them so the replay is complete up to the moment of the request.
    live = open_journal(run_id)
    if live is not None:
        await live.drain()

    if FileRunJournal.exists(run_id) or run_id in _active_tasks:
        # Throwaway reader — only the pump registers journals (get_journal),
        # so finished runs don't accumulate in the registry.
        events = FileRunJournal(run_id).read(after_id=after, limit=limit)
        return {
            "events": events,
            "last_id": events[-1]["id"] if events else after,
            "run": await _run_summary(run_id),
        }

    redis = getattr(request.app.state, "redis", None)
    if redis is not None:
        from core.scale.event_bridge import get_run_events as redis_run_events
        events = await redis_run_events(redis, run_id)
        if events:
            return {
                "events": events,
                "last_id": events[-1]["id"],
                "run": await _run_summary(run_id),
            }

    if await _run_summary(run_id) is not None:
        # Pre-journal run: checkpoint exists but no events were recorded.
        return {"events": [], "last_id": after, "run": await _run_summary(run_id)}
    raise HTTPException(status_code=404, detail="Run not found")


async def _stream_journal_sse(run_id: str, after_id: int):
    """SSE generator: journal replay from `after_id`, then live tail.

    Subscribes to the broker BEFORE reading the file so no event can fall
    between replay and tail; duplicates delivered during the replay window are
    dropped by the monotonic-id check. A `run_stream_end` from the pump closes
    the stream when terminal and keeps it open when paused (the resume pump
    appends to the same journal/broker, so the tail just continues).
    """
    from core.orchestration.journal import FileRunJournal, broker, open_journal

    journal = FileRunJournal(run_id)  # throwaway reader; pump owns the registry
    queue = broker.subscribe(run_id)
    try:
        live = open_journal(run_id)
        if live is not None:
            await live.drain()  # subscribed already, so nothing falls between replay and tail
        last = after_id
        saw_terminal_end = False
        for entry in journal.read(after_id=last):
            last = entry["id"]
            event = entry["event"]
            if event.get("type") == "run_stream_end":
                # Internal pump marker — handled, never forwarded. A non-paused
                # one means the run's last pump finished for good, which also
                # covers runs that died before writing any checkpoint.
                saw_terminal_end = not event.get("paused")
                continue
            saw_terminal_end = False
            yield f"id: {entry['id']}\ndata: {json.dumps(event, default=str)}\n\n"

        # Replay finished. If nothing is running and the run ended (journal
        # terminator or terminal checkpoint), there is nothing to tail.
        if run_id not in _active_tasks and (saw_terminal_end or await _run_is_terminal(run_id)):
            yield f"data: {json.dumps({'type': 'stream_complete'})}\n\n"
            return

        while True:
            try:
                entry = await asyncio.wait_for(queue.get(), timeout=_TAIL_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # Pump died without a run_stream_end (crash) — close once the
                # checkpoint says the run is over instead of idling forever.
                if run_id not in _active_tasks and await _run_is_terminal(run_id):
                    yield f"data: {json.dumps({'type': 'stream_complete'})}\n\n"
                    return
                yield ": keepalive\n\n"
                continue
            if entry["id"] != 0 and entry["id"] <= last:
                continue  # duplicate from the replay window
            event = entry["event"]
            if event.get("type") == "run_stream_end":
                if not event.get("paused"):
                    yield f"data: {json.dumps({'type': 'stream_complete'})}\n\n"
                    return
                continue  # paused: stay subscribed for the resume pump
            if entry["id"] != 0:
                last = entry["id"]
                yield f"id: {entry['id']}\ndata: {json.dumps(event, default=str)}\n\n"
            else:
                # Cap-skipped event (not journaled): deliver live but without an
                # id line, so it can't regress the client's Last-Event-ID.
                yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        broker.unsubscribe(run_id, queue)


@router.get("/api/orchestrations/runs/{run_id}/events/stream")
async def stream_run_events_sse(run_id: str, request: Request, after: int = 0):
    """SSE: replay journaled events after `after` (or Last-Event-ID), then tail live.

    This is the reattach stream — safe to open, close, and reopen at any point
    in a run's life; pass the last seen event id to resume where you left off.
    """
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            after = int(last_event_id)
        except ValueError:
            pass

    from core.orchestration.journal import FileRunJournal

    if not FileRunJournal.exists(run_id) and run_id not in _active_tasks:
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            # V2/worker run: bridge the Redis Stream (its ids are Redis msg ids).
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
        if await _run_summary(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")

    return StreamingResponse(
        _stream_journal_sse(run_id, after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/api/orchestrations/{orch_id}")
async def get_orchestration(orch_id: str):
    from core.store.resources import get_orchestration as _get
    orch = await _get(orch_id)
    if not orch:
        raise HTTPException(status_code=404, detail="Orchestration not found")
    return orch


@router.get("/api/orchestrations/{orch_id}/estimate")
async def estimate_orchestration_cost(orch_id: str, sample_size: int = 5):
    """Estimate next-run cost based on the last N runs' usage logs.

    Returns the average cost and average cache hit-rate across recent runs.
    When no history exists, returns zeros — the UI should show this as
    "no prior runs" rather than $0.00.
    """
    from core.usage_tracker import get_usage_logs
    # Pull recent orchestration logs and filter to this orch_id's runs.
    raw = await get_usage_logs(limit=500, source="orchestration")
    by_run: dict[str, dict] = {}
    for r in raw:
        rid = r.get("run_id") or ""
        if not rid.startswith(f"run_{orch_id}_"):
            continue
        b = by_run.setdefault(rid, {
            "cost": 0.0, "requests": 0, "hits": 0,
            "cache_read": 0, "cache_write": 0,
        })
        b["cost"] += float(r.get("estimated_cost") or 0.0)
        b["requests"] += 1
        if r.get("response_cache_hit"):
            b["hits"] += 1
        b["cache_read"] += int(r.get("cache_read_tokens") or 0)
        b["cache_write"] += int(r.get("cache_write_tokens") or 0)

    runs_sorted = sorted(by_run.items(), key=lambda kv: kv[0], reverse=True)[:sample_size]
    if not runs_sorted:
        return {
            "sample_size": 0,
            "average_cost_usd": 0.0,
            "average_cache_hit_rate": 0.0,
            "average_cache_read_tokens": 0,
        }
    avg_cost = sum(b[1]["cost"] for b in runs_sorted) / len(runs_sorted)
    avg_hit = sum((b[1]["hits"] / b[1]["requests"]) for b in runs_sorted) / len(runs_sorted)
    avg_cache_read = sum(b[1]["cache_read"] for b in runs_sorted) / len(runs_sorted)
    return {
        "sample_size": len(runs_sorted),
        "average_cost_usd": round(avg_cost, 6),
        "average_cache_hit_rate": round(avg_hit, 4),
        "average_cache_read_tokens": int(avg_cache_read),
    }


@router.post("/api/orchestrations")
async def create_or_update_orchestration(orch: Orchestration):
    # A single upsert rather than read-list / mutate / write-list, which loses
    # whichever of two concurrent edits finishes first.
    from core.store.resources import get_orchestration as _get
    from core.store.resources import save_orchestration

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    existing = await _get(orch.id)

    data = orch.model_dump()
    data["updated_at"] = now
    data["created_at"] = (existing or {}).get("created_at", now)
    await save_orchestration(data)
    return data


@router.delete("/api/orchestrations/{orch_id}")
async def delete_orchestration(orch_id: str):
    from core.store.resources import delete_orchestration as _delete
    await _delete(orch_id)
    return {"status": "success"}


# ── Execution ─────────────────────────────────────────────────────

@router.post("/api/orchestrations/{orch_id}/run")
async def run_orchestration(orch_id: str, request: Request):
    """Start an orchestration run. Returns SSE stream.

    The engine runs in a background task so that it continues executing
    (and logging) even if the SSE client disconnects or is slow to read.
    """
    from core.scale.context import resolve_orchestration
    orch_data = await resolve_orchestration(orch_id)
    if not orch_data:
        raise HTTPException(status_code=404, detail="Orchestration not found")

    body = await request.json()
    user_input = body.get("message", "")
    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"

    orch = Orchestration.model_validate(orch_data)
    server_module = request.app.state.server_module

    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(orch, server_module)

    # Engine runs in its own task, independently of the SSE consumer, and is
    # registered for cancellation.
    _task, queue = spawn_engine_run(engine.run(user_input, run_id), run_id)

    async def event_stream():
        # Heartbeats keep the stream warm during long LLM/compaction steps so a
        # busy-but-alive run is not mistaken for a dropped connection.
        async for chunk in drain_queue_with_heartbeat(queue, SENTINEL):
            yield chunk
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


#: State keys the engine keeps for itself — progress records, routing
#: decisions, loop accumulators, execution memory. Named explicitly rather
#: than "anything starting with _": a user's own state_schema key may start
#: with an underscore, and that one is theirs to read back.
_PRIVATE_STATE_PREFIXES = (
    "_step_progress_", "_loop_progress_", "_parallel_progress_", "_exec_memory_",
    "_routing_", "_loop_", "_if_decision_", "_switch_decision_",
)


def _public_run_view(run) -> dict:
    """The run as the API shows it: engine bookkeeping stripped.

    `step_progress` is up to 200 KB of agent transcript per step and
    `_exec_memory_*` grows with every rerun; nothing outside the engine reads
    either, and the frontend never renders shared_state at all.
    """
    doc = run.model_dump()
    doc.pop("step_progress", None)
    doc["shared_state"] = {
        k: v for k, v in (doc.get("shared_state") or {}).items()
        if not k.startswith(_PRIVATE_STATE_PREFIXES)
    }
    return doc


@router.get("/api/orchestrations/runs/{run_id}")
async def get_run_status(run_id: str):
    """Get the current state of a run from its checkpoint."""
    from core.orchestration.state import SharedState
    try:
        restored = await SharedState.restore(run_id)
        return _public_run_view(restored.run)
    except FileNotFoundError:
        # Checkpoints are only written at step boundaries, so a run still inside
        # its first long-running step has no checkpoint yet. If it's a live
        # in-process background task, report it as running so a reconnecting
        # client re-attaches instead of seeing "Run not found".
        task = _active_tasks.get(run_id)
        if task is not None and not task.done():
            return {"run_id": run_id, "status": "running", "step_history": [], "waiting_for_human": False}
        raise HTTPException(status_code=404, detail="Run not found")


@router.post("/api/orchestrations/runs/{run_id}/resume")
async def resume_failed_run(run_id: str, request: Request):
    """Resume a failed or cancelled orchestration from where it stopped. Returns SSE stream."""
    server_module = request.app.state.server_module

    # Two pumps for one run would interleave its journal and duplicate work.
    active = _active_tasks.get(run_id)
    if active is not None and not active.done():
        raise HTTPException(status_code=409, detail="Run is already active")

    from core.orchestration.engine import OrchestrationEngine

    _task, queue = spawn_engine_run(
        OrchestrationEngine.resume_failed(run_id, server_module), run_id
    )

    async def event_stream():
        # Heartbeats keep the stream warm during long LLM/compaction steps so a
        # busy-but-alive run is not mistaken for a dropped connection.
        async for chunk in drain_queue_with_heartbeat(queue, SENTINEL):
            yield chunk
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/orchestrations/runs/{run_id}/human-input")
async def submit_human_input(run_id: str, request: Request):
    """Submit human input and resume the orchestration. Returns SSE stream.

    For V2 (distributed) runs the response is published to Redis and a
    resume job is enqueued to ARQ — no in-process engine is started.
    For V1 (in-process) runs the existing behaviour is unchanged.
    """
    body = await request.json()
    human_response = body.get("response", {})
    step_id = body.get("step_id", "")  # optional, sent by frontend

    server_module = request.app.state.server_module

    # Try to resolve messaging Future (first-wins)
    messaging_manager = getattr(request.app.state, "messaging_manager", None)
    if messaging_manager and step_id:
        key = f"{run_id}:{step_id}"
        response_text = ""
        if isinstance(human_response, dict):
            response_text = " ".join(str(v) for v in human_response.values())
        else:
            response_text = str(human_response)
        messaging_manager.resolve_human_input_by_key(key, response_text)

    # --- V2 distributed path: detect via Postgres run row ---
    redis = getattr(request.app.state, "redis", None)
    arq_redis = getattr(request.app.state, "arq_redis", None)
    pg_session_factory = getattr(request.app.state, "pg_session_factory", None)

    if redis and arq_redis and pg_session_factory:
        # Check if this is a V2 run (has worker_id set in Postgres)
        try:
            from sqlalchemy import select
            from core.store.models import OrchestrationRunDB
            async with pg_session_factory() as session:
                result = await session.execute(
                    select(OrchestrationRunDB.worker_id).where(
                        OrchestrationRunDB.run_id == run_id
                    )
                )
                row = result.one_or_none()

            if row and row.worker_id:
                # V2 run: publish to Redis and enqueue the resume job. One
                # queue — the run's state is in the store, so any worker can
                # resume it, not only the one that paused it.
                from core.scale.pubsub import publish_human_input
                from core.scale.config import QUEUE_NAME as _queue_name
                resp = human_response if isinstance(human_response, dict) else {"response": human_response}
                await publish_human_input(redis, run_id, resp)

                await arq_redis.enqueue_job(
                    "resume_orchestration_job",
                    run_id=run_id,
                    human_response=resp,
                    _queue_name=_queue_name,
                    _job_id=f"resume_{run_id}_{int(time.time())}",
                )

                # Return SSE stream bridged from Redis
                from core.scale.event_bridge import stream_run_events
                return StreamingResponse(
                    stream_run_events(redis, run_id, "0"),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    },
                )
        except Exception:
            pass  # Fall through to in-process V1 path

    # --- V1 in-process path (unchanged) ---
    # Two pumps for one run would interleave its journal and duplicate work.
    active = _active_tasks.get(run_id)
    if active is not None and not active.done():
        raise HTTPException(status_code=409, detail="Run is already active")

    from core.orchestration.engine import OrchestrationEngine

    _task, queue = spawn_engine_run(
        OrchestrationEngine.resume(run_id, human_response, server_module), run_id
    )

    async def event_stream():
        # Heartbeats keep the stream warm during long LLM/compaction steps so a
        # busy-but-alive run is not mistaken for a dropped connection.
        async for chunk in drain_queue_with_heartbeat(queue, SENTINEL):
            yield chunk
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/orchestrations/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    """Cancel a running orchestration (works for both V1 in-process and V2 distributed runs)."""
    from core.orchestration.state import SharedState, _cancelled_run_ids

    # Signal the engine loop to exit on its next iteration (V1 in-process path)
    _cancelled_run_ids.add(run_id)

    # Cancel the asyncio task to interrupt any in-progress await (e.g. LLM call)
    task = _active_tasks.pop(run_id, None)
    if task and not task.done():
        task.cancel()

    # Distributed cancel: publish Redis key so workers on other machines stop too
    redis = getattr(request.app.state, "redis", None)
    if redis:
        try:
            from core.scale.pubsub import publish_cancellation
            await publish_cancellation(redis, run_id)
        except Exception:
            pass

    # Persist cancelled status to disk (V1 checkpoint)
    try:
        restored = await SharedState.restore(run_id)
        restored.run.status = "cancelled"
        restored.run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await restored.checkpoint()
    except FileNotFoundError:
        pass  # Run may not have checkpointed yet; that's fine

    return {"status": "cancelled", "run_id": run_id}


# ── Logs ───────────────────────────────────────────────────────

@router.get("/api/orchestrations/logs")
async def list_orchestration_logs(limit: int = 20):
    """List recent orchestration run logs (summary only)."""
    from core.orchestration.logger import OrchestrationLogger
    return OrchestrationLogger.list_logs(limit=limit)


@router.get("/api/orchestrations/logs/{run_id}")
async def get_orchestration_log(run_id: str):
    """Get full detailed log for a specific orchestration run (plain text)."""
    from core.orchestration.logger import OrchestrationLogger
    from fastapi.responses import PlainTextResponse
    log = OrchestrationLogger.get_log(run_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    return PlainTextResponse(log)


@router.delete("/api/orchestrations/logs/{run_id}")
async def delete_orchestration_log(run_id: str):
    """Delete a specific orchestration log (and its event journal)."""
    from core.orchestration.logger import OrchestrationLogger
    from core.orchestration.journal import FileRunJournal
    deleted_journal = FileRunJournal.delete(run_id)
    if OrchestrationLogger.delete_log(run_id) or deleted_journal:
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(status_code=404, detail="Log not found")


# ── Deploy ─────────────────────────────────────────────────────

@router.post("/api/orchestrations/{orch_id}/deploy")
async def deploy_as_agent(orch_id: str):
    """Create an orchestrator-type agent from this orchestration."""
    from core.store.resources import get_orchestration as _get
    orch_data = await _get(orch_id)
    if not orch_data:
        raise HTTPException(status_code=404, detail="Orchestration not found")

    from core.routes.agents import load_user_agents

    agents = await load_user_agents()

    # Check if already deployed
    existing = next((a for a in agents if a.get("orchestration_id") == orch_id), None)
    if existing:
        return {"status": "already_deployed", "agent_id": existing["id"]}

    agent_id = f"orch_agent_{orch_id}"
    agent = {
        "id": agent_id,
        "name": orch_data["name"],
        "description": orch_data.get("description", ""),
        "avatar": orch_data.get("avatar", "default"),
        "type": "orchestrator",
        "tools": [],
        "repos": [],
        "system_prompt": f"This is an orchestrator agent for '{orch_data['name']}'. It runs automatically.",
        "orchestration_id": orch_id,
    }

    from core.store.resources import save_agent
    await save_agent(agent)

    return {"status": "deployed", "agent_id": agent_id, "agent": agent}
