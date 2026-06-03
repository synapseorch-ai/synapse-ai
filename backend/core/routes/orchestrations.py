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
from core.config import DATA_DIR
from core.json_store import JsonStore

router = APIRouter()

_orch_store = JsonStore(os.path.join(DATA_DIR, "orchestrations.json"), cache_ttl=2.0)

# In-memory registry of active run tasks (for mid-step cancellation)
_active_tasks: dict[str, asyncio.Task] = {}


def load_orchestrations() -> list[dict]:
    return _orch_store.load()


def save_orchestrations(data: list[dict]):
    _orch_store.save(data)


# ── CRUD ──────────────────────────────────────────────────────────

@router.get("/api/orchestrations")
async def list_orchestrations():
    return load_orchestrations()


@router.get("/api/orchestrations/runs")
async def list_runs():
    """List recent orchestration runs."""
    from core.orchestration.state import SharedState
    return SharedState.list_runs()


@router.get("/api/orchestrations/{orch_id}")
async def get_orchestration(orch_id: str):
    orchs = load_orchestrations()
    orch = next((o for o in orchs if o["id"] == orch_id), None)
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
    raw = get_usage_logs(limit=500, source="orchestration")
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
    orchs = load_orchestrations()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for i, o in enumerate(orchs):
        if o["id"] == orch.id:
            data = orch.model_dump()
            data["updated_at"] = now
            data["created_at"] = o.get("created_at", now)
            orchs[i] = data
            save_orchestrations(orchs)
            return data

    data = orch.model_dump()
    data["created_at"] = now
    data["updated_at"] = now
    orchs.append(data)
    save_orchestrations(orchs)
    return data


@router.delete("/api/orchestrations/{orch_id}")
async def delete_orchestration(orch_id: str):
    orchs = load_orchestrations()
    orchs = [o for o in orchs if o["id"] != orch_id]
    save_orchestrations(orchs)
    return {"status": "success"}


# ── Execution ─────────────────────────────────────────────────────

@router.post("/api/orchestrations/{orch_id}/run")
async def run_orchestration(orch_id: str, request: Request):
    """Start an orchestration run. Returns SSE stream.

    The engine runs in a background task so that it continues executing
    (and logging) even if the SSE client disconnects or is slow to read.
    """
    orchs = load_orchestrations()
    orch_data = next((o for o in orchs if o["id"] == orch_id), None)
    if not orch_data:
        raise HTTPException(status_code=404, detail="Orchestration not found")

    body = await request.json()
    user_input = body.get("message", "")
    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"

    orch = Orchestration.model_validate(orch_data)
    server_module = request.app.state.server_module

    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(orch, server_module)

    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    async def _run_engine():
        try:
            async for event in engine.run(user_input, run_id):
                etype = event.get("type", "")
                if etype not in ("chunk", "thinking", "token_usage"):
                    print(f"DEBUG SSE QUEUE: → {etype} step={event.get('orch_step_id', '')}", flush=True)
                await queue.put(event)
                # Yield so event_stream() can dequeue and flush this event
                # to the HTTP response before the next event is enqueued.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            await queue.put({"type": "orchestration_error", "error": "Cancelled"})
        except Exception as e:
            await queue.put({"type": "orchestration_error", "error": str(e)})
        finally:
            _active_tasks.pop(run_id, None)
            print(f"DEBUG SSE QUEUE: sentinel sent, stream closing", flush=True)
            await queue.put(_SENTINEL)

    # Engine runs independently of SSE consumer; store task for cancellation
    task = asyncio.create_task(_run_engine())
    _active_tasks[run_id] = task

    async def event_stream():
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"
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


@router.get("/api/orchestrations/runs/{run_id}")
async def get_run_status(run_id: str):
    """Get the current state of a run from its checkpoint."""
    from core.orchestration.state import SharedState
    try:
        restored = SharedState.restore(run_id)
        return restored.run.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.post("/api/orchestrations/runs/{run_id}/resume")
async def resume_failed_run(run_id: str, request: Request):
    """Resume a failed or cancelled orchestration from where it stopped. Returns SSE stream."""
    server_module = request.app.state.server_module

    from core.orchestration.engine import OrchestrationEngine

    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    async def _run_engine():
        try:
            async for event in OrchestrationEngine.resume_failed(run_id, server_module):
                await queue.put(event)
                await asyncio.sleep(0)
        except FileNotFoundError:
            await queue.put({"type": "orchestration_error", "error": "Run not found"})
        except Exception as e:
            await queue.put({"type": "orchestration_error", "error": str(e)})
        finally:
            _active_tasks.pop(run_id, None)
            await queue.put(_SENTINEL)

    task = asyncio.create_task(_run_engine())
    _active_tasks[run_id] = task

    async def event_stream():
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"
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
            from core.scale.models_db import OrchestrationRunDB
            async with pg_session_factory() as session:
                result = await session.execute(
                    select(OrchestrationRunDB.worker_id).where(
                        OrchestrationRunDB.run_id == run_id
                    )
                )
                row = result.one_or_none()

            if row and row.worker_id:
                # V2 run: publish to Redis and enqueue resume job
                from core.scale.pubsub import publish_human_input
                resp = human_response if isinstance(human_response, dict) else {"response": human_response}
                await publish_human_input(redis, run_id, resp)

                await arq_redis.enqueue_job(
                    "resume_orchestration_job",
                    run_id=run_id,
                    human_response=resp,
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
    from core.orchestration.engine import OrchestrationEngine

    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    async def _run_engine():
        try:
            async for event in OrchestrationEngine.resume(run_id, human_response, server_module):
                await queue.put(event)
                await asyncio.sleep(0)
        except FileNotFoundError:
            await queue.put({"type": "orchestration_error", "error": "Run not found"})
        except Exception as e:
            await queue.put({"type": "orchestration_error", "error": str(e)})
        finally:
            await queue.put(_SENTINEL)

    asyncio.create_task(_run_engine())

    async def event_stream():
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                break
            yield f"data: {json.dumps(event, default=str)}\n\n"
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
        restored = SharedState.restore(run_id)
        restored.run.status = "cancelled"
        restored.run.ended_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        restored.checkpoint()
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
    """Delete a specific orchestration log."""
    from core.orchestration.logger import OrchestrationLogger
    if OrchestrationLogger.delete_log(run_id):
        return {"status": "deleted", "run_id": run_id}
    raise HTTPException(status_code=404, detail="Log not found")


# ── Deploy ─────────────────────────────────────────────────────

@router.post("/api/orchestrations/{orch_id}/deploy")
async def deploy_as_agent(orch_id: str):
    """Create an orchestrator-type agent from this orchestration."""
    orchs = load_orchestrations()
    orch_data = next((o for o in orchs if o["id"] == orch_id), None)
    if not orch_data:
        raise HTTPException(status_code=404, detail="Orchestration not found")

    from core.routes.agents import load_user_agents, save_user_agents

    agents = load_user_agents()

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

    agents.append(agent)
    save_user_agents(agents)

    return {"status": "deployed", "agent_id": agent_id, "agent": agent}
