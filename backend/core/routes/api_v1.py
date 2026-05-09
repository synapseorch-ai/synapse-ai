"""
V1 External API Endpoints
--------------------------
Programmatic API for external apps to interact with Synapse agents
and orchestrations. All routes are protected by API key auth (Bearer token).

Endpoints:
  POST /chat              — Sync chat (returns only final response)
  POST /chat/stream       — SSE chat (all events)
  POST /orchestrations/{orch_id}/run         — Start orchestration (sync)
  POST /orchestrations/{orch_id}/run/stream  — Start orchestration (SSE)
  POST /orchestrations/runs/{run_id}/resume         — Resume after human input (sync)
  POST /orchestrations/runs/{run_id}/resume/stream  — Resume after human input (SSE)
"""
import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.api_key_middleware import require_api_key

router = APIRouter()
log = logging.getLogger("api_v1")


# ── Request / Response Models ────────────────────────────────────────────────

class V1ChatRequest(BaseModel):
    message: str
    agent: str | None = None          # agent name or ID (optional — first agent if omitted)
    session_id: str | None = None     # for conversation continuity (auto-generated if omitted)
    images: list[str] = []            # optional base64 images


class V1OrchestrationRunRequest(BaseModel):
    message: str = ""


class V1ResumeRequest(BaseModel):
    response: dict | str = {}         # human input fields


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_agent_for_api(agent_identifier: str | None) -> dict | None:
    """Find agent by name (case-insensitive) or ID. Falls back to first agent."""
    from core.routes.agents import load_user_agents
    agents = load_user_agents()
    if not agents:
        return None
    if not agent_identifier:
        return agents[0]
    # Exact ID match
    by_id = next((a for a in agents if a["id"] == agent_identifier), None)
    if by_id:
        return by_id
    # Case-insensitive name match
    lower = agent_identifier.lower()
    by_name = next((a for a in agents if a["name"].lower() == lower), None)
    if by_name:
        return by_name
    # Partial name match
    partial = next((a for a in agents if lower in a["name"].lower()), None)
    if partial:
        return partial
    # Fallback to first agent
    return agents[0]


def _build_chat_request(body: V1ChatRequest, agent: dict) -> "ChatRequest":
    """Convert V1ChatRequest into the internal ChatRequest model."""
    from core.models import ChatRequest
    session_id = body.session_id or f"api_{uuid.uuid4().hex[:12]}"
    return ChatRequest(
        message=body.message,
        session_id=session_id,
        agent_id=agent["id"],
        images=body.images,
    )


def _format_sse_event(event: dict) -> str:
    """Format an event dict as an SSE data line."""
    return f"data: {json.dumps(event, default=str)}\n\n"


# ── Chat Endpoints ──────────────────────────────────────────────────────────

@router.post("/chat")
async def v1_chat(body: V1ChatRequest, key_record: dict = Depends(require_api_key)):
    """Synchronous chat — returns only the final response."""
    agent = _resolve_agent_for_api(body.agent)
    if not agent:
        raise HTTPException(status_code=400, detail="No agents configured")

    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=503, detail="No agent sessions available. Server may still be starting.")

    chat_request = _build_chat_request(body, agent)
    from core.react_engine import run_react_loop

    final_event = None
    error_msg = None

    try:
        async for event in run_react_loop(chat_request, _server):
            etype = event.get("type", "")
            if etype == "final":
                final_event = event
            elif etype == "error":
                error_msg = event.get("message", "Unknown error")
    except Exception as exc:
        log.exception("[v1/chat] Unhandled error for session=%s", chat_request.session_id)
        raise HTTPException(status_code=500, detail="An internal error occurred. Check server logs for details.")

    if error_msg:
        log.error("[v1/chat] Agent error for session=%s: %s", chat_request.session_id, error_msg)
        raise HTTPException(status_code=500, detail="The agent encountered an error processing your request.")

    response_text = "I completed the requested actions."
    if final_event:
        response_text = final_event.get("response", response_text)

    return {
        "response": response_text,
        "agent_id": agent["id"],
        "agent_name": agent["name"],
        "session_id": chat_request.session_id,
    }


@router.post("/chat/stream")
async def v1_chat_stream(body: V1ChatRequest, key_record: dict = Depends(require_api_key)):
    """SSE streaming chat — returns all events."""
    agent = _resolve_agent_for_api(body.agent)
    if not agent:
        raise HTTPException(status_code=400, detail="No agents configured")

    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=503, detail="No agent sessions available. Server may still be starting.")

    chat_request = _build_chat_request(body, agent)

    async def event_generator():
        from core.react_engine import run_react_loop
        try:
            # Emit session info first so the consumer can capture the session_id
            yield _format_sse_event({
                "type": "session",
                "session_id": chat_request.session_id,
                "agent_id": agent["id"],
                "agent_name": agent["name"],
            })
            async for event in run_react_loop(chat_request, _server):
                etype = event.get("type", "")

                if etype == "status":
                    yield _format_sse_event({"type": "status", "message": event["message"]})

                elif etype == "thinking":
                    yield _format_sse_event({
                        "type": "thinking",
                        "message": event.get("message", ""),
                    })

                elif etype == "tool_execution":
                    yield _format_sse_event({
                        "type": "tool_execution",
                        "tool_name": event["tool_name"],
                        "args": event["args"],
                    })

                elif etype == "tool_result":
                    yield _format_sse_event({
                        "type": "tool_result",
                        "tool_name": event["tool_name"],
                        "preview": event["preview"],
                    })

                elif etype == "llm_thought":
                    yield _format_sse_event({
                        "type": "llm_thought",
                        "thought": event["thought"],
                        "turn": event.get("turn", 1),
                    })

                elif etype == "final":
                    yield _format_sse_event({
                        "type": "response",
                        "content": event.get("response", ""),
                        "intent": event.get("intent", "chat"),
                        "session_id": chat_request.session_id,
                    })
                    yield _format_sse_event({"type": "done"})

                elif etype == "error":
                    log.error("[v1/chat/stream] Agent error session=%s: %s", chat_request.session_id, event.get("message"))
                    yield _format_sse_event({"type": "error", "message": "The agent encountered an error processing your request."})

                await asyncio.sleep(0)

        except Exception as e:
            log.exception("[v1/chat/stream] Unhandled error session=%s", chat_request.session_id)
            yield _format_sse_event({"type": "error", "message": "An internal error occurred. Check server logs for details."})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Orchestration Endpoints ─────────────────────────────────────────────────

@router.post("/orchestrations/{orch_id}/run")
async def v1_orchestration_run(
    orch_id: str,
    body: V1OrchestrationRunRequest,
    key_record: dict = Depends(require_api_key),
):
    """Start an orchestration (sync). Returns final result or human_input_required."""
    import core.server as _server
    from core.routes.orchestrations import load_orchestrations
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orchs = load_orchestrations()
    orch_data = next((o for o in orchs if o["id"] == orch_id), None)
    if not orch_data:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orch_id}' not found")

    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, _server)

    final_response = None
    human_input_event = None
    step_history = []
    shared_state = {}
    status = "running"

    async for event in engine.run(body.message, run_id):
        etype = event.get("type", "")

        if etype == "human_input_required":
            human_input_event = event
            status = "paused"
            break

        if etype == "orchestration_complete":
            status = event.get("status", "completed")
            shared_state = event.get("final_state", {})

        if etype == "final":
            final_response = event.get("response", "")
            step_history = event.get("data", {}).get("step_history", []) if event.get("data") else []

    if human_input_event:
        return {
            "status": "paused",
            "run_id": run_id,
            "human_input_required": {
                "step_id": human_input_event.get("orch_step_id"),
                "prompt": human_input_event.get("prompt"),
                "fields": human_input_event.get("fields", []),
                "agent_context": human_input_event.get("agent_context"),
            },
        }

    return {
        "status": status,
        "run_id": run_id,
        "response": final_response or f"Orchestration {status}.",
        "shared_state": shared_state,
        "step_history": step_history,
    }


@router.post("/orchestrations/{orch_id}/run/stream")
async def v1_orchestration_run_stream(
    orch_id: str,
    body: V1OrchestrationRunRequest,
    key_record: dict = Depends(require_api_key),
):
    """Start an orchestration (SSE stream)."""
    import core.server as _server
    from core.routes.orchestrations import load_orchestrations
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orchs = load_orchestrations()
    orch_data = next((o for o in orchs if o["id"] == orch_id), None)
    if not orch_data:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orch_id}' not found")

    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, _server)

    async def event_stream():
        try:
            async for event in engine.run(body.message, run_id):
                etype = event.get("type", "")

                # Skip internal log events
                if etype.startswith("_log_"):
                    continue

                yield _format_sse_event(event)

                if etype == "human_input_required":
                    yield _format_sse_event({"type": "done"})
                    return

                await asyncio.sleep(0)
        except Exception as e:
            log.exception("[v1/orch/stream] Unhandled error run_id=%s", run_id)
            yield _format_sse_event({"type": "orchestration_error", "error": "An internal error occurred. Check server logs for details."})

        yield _format_sse_event({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Orchestration Resume Endpoints ──────────────────────────────────────────

@router.post("/orchestrations/runs/{run_id}/resume")
async def v1_orchestration_resume(
    run_id: str,
    body: V1ResumeRequest,
    key_record: dict = Depends(require_api_key),
):
    """Submit human input and resume orchestration (sync)."""
    import core.server as _server
    from core.orchestration.engine import OrchestrationEngine

    # Normalize response to dict
    human_response = body.response
    if isinstance(human_response, str):
        human_response = {"response": human_response}

    final_response = None
    human_input_event = None
    step_history = []
    shared_state = {}
    status = "running"
    _allowed_statuses = {"running", "paused", "completed", "failed"}

    try:
        async for event in OrchestrationEngine.resume(run_id, human_response, _server):
            etype = event.get("type", "")

            if etype == "human_input_required":
                human_input_event = event
                status = "paused"
                break

            if etype == "orchestration_complete":
                _raw_status = event.get("status", "completed")
                status = _raw_status if _raw_status in _allowed_statuses else "completed"
                shared_state = event.get("final_state", {})

            if etype == "final":
                final_response = event.get("response", "")
                step_history = event.get("data", {}).get("step_history", []) if event.get("data") else []

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    except Exception as exc:
        log.exception("[v1/resume] Unhandled error run_id=%s", run_id)
        raise HTTPException(status_code=500, detail="An internal error occurred. Check server logs for details.")

    if human_input_event:
        return {
            "status": "paused",
            "run_id": run_id,
            "human_input_required": {
                "step_id": human_input_event.get("orch_step_id"),
                "prompt": human_input_event.get("prompt"),
                "fields": human_input_event.get("fields", []),
                "agent_context": human_input_event.get("agent_context"),
            },
        }

    return {
        "status": status,
        "run_id": run_id,
        "response": final_response or f"Orchestration {status}.",
        "shared_state": shared_state,
        "step_history": step_history,
    }


@router.post("/orchestrations/runs/{run_id}/resume/stream")
async def v1_orchestration_resume_stream(
    run_id: str,
    body: V1ResumeRequest,
    key_record: dict = Depends(require_api_key),
):
    """Submit human input and resume orchestration (SSE stream)."""
    import core.server as _server
    from core.orchestration.engine import OrchestrationEngine

    # Normalize response to dict
    human_response = body.response
    if isinstance(human_response, str):
        human_response = {"response": human_response}

    async def event_stream():
        try:
            async for event in OrchestrationEngine.resume(run_id, human_response, _server):
                etype = event.get("type", "")

                if etype.startswith("_log_"):
                    continue

                yield _format_sse_event(event)

                if etype == "human_input_required":
                    yield _format_sse_event({"type": "done"})
                    return

                await asyncio.sleep(0)
        except FileNotFoundError:
            yield _format_sse_event({"type": "orchestration_error", "error": f"Run '{run_id}' not found"})
        except Exception as e:
            log.exception("[v1/resume/stream] Unhandled error run_id=%s", run_id)
            yield _format_sse_event({"type": "orchestration_error", "error": "An internal error occurred. Check server logs for details."})

        yield _format_sse_event({"type": "done"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Read-Only Discovery Endpoints ──────────────────────────────────────────

@router.get("/agents")
async def v1_list_agents(key_record: dict = Depends(require_api_key)):
    """List all configured agents (id, name, type, capabilities)."""
    from core.routes.agents import load_user_agents
    agents = load_user_agents()
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "type": a.get("type", "conversational"),
            "model": a.get("model", ""),
            "capabilities": a.get("capabilities", []),
        }
        for a in agents
    ]


@router.get("/agents/{agent_id}")
async def v1_get_agent(agent_id: str, key_record: dict = Depends(require_api_key)):
    """Get details for a specific agent."""
    from core.routes.agents import load_user_agents
    agents = load_user_agents()
    agent = next((a for a in agents if a["id"] == agent_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return {
        "id": agent["id"],
        "name": agent["name"],
        "type": agent.get("type", "conversational"),
        "model": agent.get("model", ""),
        "capabilities": agent.get("capabilities", []),
        "description": agent.get("description", ""),
    }


@router.get("/orchestrations")
async def v1_list_orchestrations(key_record: dict = Depends(require_api_key)):
    """List all configured orchestrations (id, name, steps summary)."""
    from core.routes.orchestrations import load_orchestrations
    orchs = load_orchestrations()
    return [
        {
            "id": o["id"],
            "name": o.get("name", ""),
            "description": o.get("description", ""),
            "steps": len(o.get("steps", [])),
        }
        for o in orchs
    ]


@router.get("/orchestrations/{orch_id}")
async def v1_get_orchestration(orch_id: str, key_record: dict = Depends(require_api_key)):
    """Get details for a specific orchestration including step definitions."""
    from core.routes.orchestrations import load_orchestrations
    orchs = load_orchestrations()
    orch = next((o for o in orchs if o["id"] == orch_id), None)
    if not orch:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orch_id}' not found")
    return {
        "id": orch["id"],
        "name": orch.get("name", ""),
        "description": orch.get("description", ""),
        "steps": [
            {
                "id": s.get("id", ""),
                "label": s.get("label", ""),
                "type": s.get("type", ""),
                "agent_id": s.get("agent_id", ""),
                "depends_on": s.get("depends_on", []),
            }
            for s in orch.get("steps", [])
        ],
        "edges": orch.get("edges", []),
    }

