"""
V1 External API Endpoints
--------------------------
Programmatic API for external apps to interact with Synapse agents
and orchestrations. All routes are protected by API key auth (Bearer token).

Endpoints:
  POST /chat              — Sync chat (returns only final response)
  POST /chat/stream       — SSE chat (all events)
  POST /chat/{session_id}/cancel             — Stop the turn a conversation is running
  POST /orchestrations/{orch_id}/run         — Start orchestration (sync)
  POST /orchestrations/{orch_id}/run/stream  — Start orchestration (SSE)
  GET  /orchestrations/runs/{run_id}         — A run's state, and its result once it has ended
  POST /orchestrations/runs/{run_id}/resume         — Resume after human input (sync)
  POST /orchestrations/runs/{run_id}/resume/stream  — Resume after human input (SSE)
  POST /orchestrations/runs/{run_id}/cancel         — Stop a run
  GET  /orchestrations/runs                         — Recent runs (D126)
  GET  /orchestrations/runs/{run_id}/events         — A run's events from a cursor (D126)
  GET  /orchestrations/runs/{run_id}/events/stream  — The same, then followed live (D126)
  POST /chat/{session_id}/approve                   — Answer a turn's tool approval (D126)
  GET/POST /schedules, GET/PATCH/DELETE /schedules/{id}, POST /schedules/{id}/run  (D126)
  GET  /agents, /agents/{agent_id}                  — Discovery
  GET  /orchestrations, /orchestrations/{orch_id}   — Discovery
"""
import asyncio
import json
import logging
import time
import uuid

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

from core.api_key_middleware import require_api_key
from core.images import checked_images
from core.models_schedule import ScheduleCreate, ScheduleUpdate

router = APIRouter()
log = logging.getLogger("api_v1")


# ── Request / Response Models ────────────────────────────────────────────────
# Every body refuses a field it does not know (422 naming it), as cloud's v1
# does: a misspelt field used to be dropped, so `{"human_response": …}` answered
# a person's question with the default `{}` (F232), and `{"input": …}` started a
# run with no message.

class V1ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    agent: str | None = None          # agent name or ID (optional — first agent if omitted)
    session_id: str | None = None     # for conversation continuity (auto-generated if omitted)
    images: list[str] = []            # optional base64 images

    # A malformed image is the caller's 422, not a 500 raised building the request (F69).
    _check_images = checked_images()


class V1OrchestrationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = ""
    # The run's inputs, merged into its shared state (cloud's `RunRequest` has
    # the same field). A leading underscore is the engine's own bookkeeping
    # (`_model_override`, …), so a caller cannot set one (D126).
    initial_state: dict = {}

    @field_validator("initial_state")
    @classmethod
    def _no_engine_keys(cls, value: dict) -> dict:
        engine_keys = sorted(k for k in value if str(k).startswith("_"))
        if engine_keys:
            raise ValueError(f"these keys are the engine's own and cannot be set: {', '.join(engine_keys)}")
        return value


class V1ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required, and never empty (`refuse_empty`): text, or the question's fields.
    response: dict | str


class V1ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str = ""
    # The call this answers, as `awaiting_approval` named it. When given and it
    # is not the call the conversation is waiting on, the answer is refused.
    approval_id: str | None = None
    agent: str | None = None          # the conversation's agent (first agent if omitted)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_system_agent(agent: dict) -> bool:
    """The AI builder's own agents — seeded, not the workspace's to call."""
    return bool(agent.get("system")) or agent.get("type") == "builder"


async def _api_agents() -> list[dict]:
    from core.routes.agents import load_user_agents
    return [a for a in await load_user_agents() if not _is_system_agent(a)]


async def _resolve_agent_for_api(agent_identifier: str | None) -> dict | None:
    """The agent an API call names: an exact id, else an exact name in any case.

    With no identifier, the workspace's first agent. None when there is no such
    agent. It used to fall back to a partial name match and then to the first
    agent — so a misspelled name ran a different agent, and on a fresh install
    the first agent was one of the AI builder's own (F69).
    """
    agents = await _api_agents()
    if not agent_identifier:
        return agents[0] if agents else None
    by_id = next((a for a in agents if a["id"] == agent_identifier), None)
    if by_id:
        return by_id
    lower = agent_identifier.lower()
    return next((a for a in agents if (a.get("name") or "").lower() == lower), None)


async def _agent_or_error(agent_identifier: str | None) -> dict:
    agent = await _resolve_agent_for_api(agent_identifier)
    if agent is not None:
        return agent
    if agent_identifier:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_identifier}' not found")
    raise HTTPException(status_code=400, detail="No agents configured")


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


def _pause_of(event: dict) -> dict | None:
    """What a chat turn that stopped for a person reports, or None (F68).

    A `human_input_required` ends the turn with nothing after it: either a tool
    call whose policy is `confirm` (the call has not run) or an orchestrator
    agent's orchestration pausing on a human step. The first is answered at
    `/chat/{session_id}/approve`, the second at the run's `resume` (D126).
    """
    if event.get("type") != "human_input_required":
        return None
    if event.get("kind") == "tool_approval":
        return {
            "status": "awaiting_approval",
            "prompt": event.get("prompt") or "",
            "pending_call": {
                "tool_name": event.get("tool_name"),
                "tool_args": event.get("tool_args") or {},
                "category": event.get("category") or "",
                "approval_id": event.get("approval_id") or "",
            },
        }
    return {"status": "paused", "prompt": event.get("prompt") or "", "run_id": event.get("run_id")}


def _format_sse_event(event: dict) -> str:
    """Format an event dict as an SSE data line."""
    return f"data: {json.dumps(event, default=str)}\n\n"


# ── Chat Endpoints ──────────────────────────────────────────────────────────

async def _v1_turn_events(chat_request, agent: dict, _server, resume_state: dict | None = None):
    """One v1 chat turn's events, in v1's vocabulary — what its turn's task drives.

    Started by the handler through `core.chat_turns`, never inside a response:
    an API caller that stops reading has not asked the turn to stop, and
    `POST /api/v1/chat/{session_id}/cancel` is how it does (F49).

    A tool approval parks the call and the turn's last snapshot, as the app's
    chat does (`routes/chat.py::_event_stream`), so `/chat/{id}/approve` picks
    the turn up where it stopped instead of re-running what already ran.
    `resume_state` is that snapshot, on the way back in (D126).
    """
    from core import chat_turns
    from core.llm_providers import LLMError
    from core.react_engine import run_react_loop

    turn = chat_turns.current_turn.get()

    async def _stopped() -> bool:
        return bool(turn is not None and turn.stop_requested)

    # Session info first so the consumer can capture the session_id.
    yield {
        "type": "session",
        "session_id": chat_request.session_id,
        "agent_id": agent["id"],
        "agent_name": agent["name"],
    }
    latest_progress: dict | None = None
    try:
        async for event in run_react_loop(chat_request, _server, should_stop=_stopped,
                                          progress_events=True, resume_state=resume_state,
                                          expand_mentions=True):
            etype = event.get("type", "")

            if etype == "_step_progress":
                # Engine state for a resume, never an event.
                latest_progress = {k: v for k, v in event.items() if k != "type"}

            elif etype == "status":
                yield {"type": "status", "message": event["message"]}

            elif etype == "thinking":
                yield {"type": "thinking", "message": event.get("message", "")}

            elif etype == "tool_execution":
                yield {"type": "tool_execution", "tool_name": event["tool_name"], "args": event["args"]}

            elif etype == "tool_result":
                yield {"type": "tool_result", "tool_name": event["tool_name"], "preview": event["preview"]}

            elif etype == "llm_thought":
                yield {"type": "llm_thought", "thought": event["thought"], "turn": event.get("turn", 1)}

            elif etype == "final" and event.get("orch_step_id"):
                # An orchestrator agent's step finishing, not the turn: it used
                # to be sent as the turn's `response` and `done`, so a caller
                # stopped reading after the first step.
                yield {"type": "agent_step_result", "orch_step_id": event.get("orch_step_id"),
                       "step_name": event.get("step_name", ""), "content": event.get("response", "")}

            elif etype == "final":
                yield {
                    "type": "response",
                    "content": event.get("response", ""),
                    "intent": event.get("intent", "chat"),
                    "session_id": chat_request.session_id,
                }
                yield {"type": "done"}

            elif _pause_of(event):
                if event.get("kind") == "tool_approval":
                    from core.chat_approval import parked_payload
                    from core.store import sessions as _sessions
                    await _sessions.set_pending_approval(
                        chat_request.session_id, chat_request.agent_id,
                        parked_payload(event, message=chat_request.message, progress=latest_progress),
                    )
                pause = _pause_of(event)
                yield {"type": pause.pop("status"), **pause, "session_id": chat_request.session_id}
                yield {"type": "done"}

            elif etype in ("error", "orchestration_error"):
                # orchestration_error is what an orchestrator agent fails with.
                log.error("[v1/chat] Agent error session=%s: %s", chat_request.session_id,
                          event.get("message") or event.get("error"))
                yield {"type": "error", "message": "The agent encountered an error processing your request."}

            await asyncio.sleep(0)

    except LLMError as exc:
        # The model failed (F133): the turn raises rather than answering with the
        # provider's words, and an API caller hears what it hears of any agent
        # error — not those words, which are the workspace's to read.
        log.error("[v1/chat] Model error session=%s: %s", chat_request.session_id, exc)
        yield {"type": "error", "message": "The agent encountered an error processing your request."}
    except Exception:
        log.exception("[v1/chat] Unhandled error session=%s", chat_request.session_id)
        yield {"type": "error", "message": "An internal error occurred. Check server logs for details."}


def _v1_start(body: V1ChatRequest, agent: dict, _server):
    """Start a v1 turn from the handler; 409 while the conversation has one running."""
    from core import chat_turns
    from core.routes.chat import TURN_RUNNING

    chat_request = _build_chat_request(body, agent)
    try:
        with chat_turns.claim(chat_request.session_id, message=chat_request.message,
                              agent_key=agent["id"]) as turn:
            turn.start(_v1_turn_events(chat_request, agent, _server))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return chat_request, turn


@router.post("/chat")
async def v1_chat(body: V1ChatRequest, key_record: dict = Depends(require_api_key)):
    """Synchronous chat — returns only the final response."""
    agent = await _agent_or_error(body.agent)

    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=503, detail="No agent sessions available. Server may still be starting.")

    chat_request, turn = _v1_start(body, agent, _server)
    # Waiting is not owning: this request going away leaves the turn running.
    await turn.wait()

    identity = {
        "agent_id": agent["id"],
        "agent_name": agent["name"],
        "session_id": chat_request.session_id,
    }
    for entry in turn.entries:
        event = entry.event
        kind = event.get("type")
        if kind == "error":
            raise HTTPException(status_code=500, detail=event.get("message") or "The agent encountered an error.")
        if kind == "cancelled":
            return {"response": event.get("message") or "Stopped.", "status": "cancelled", **identity}
        if kind in ("awaiting_approval", "paused"):
            pause = {k: v for k, v in event.items() if k not in ("type", "session_id")}
            prompt = pause.pop("prompt", "")
            return {"response": prompt, "status": kind, **pause, **identity}
        if kind == "response":
            return {"response": event.get("content", ""), "status": "completed", **identity}

    return {"response": "I completed the requested actions.", "status": "completed", **identity}


@router.post("/chat/stream")
async def v1_chat_stream(body: V1ChatRequest, key_record: dict = Depends(require_api_key)):
    """SSE streaming chat — returns all events."""
    from core import chat_turns

    agent = await _agent_or_error(body.agent)

    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=503, detail="No agent sessions available. Server may still be starting.")

    _chat_request, turn = _v1_start(body, agent, _server)

    async def event_generator():
        # The turn's frames exactly as v1 has always sent them — no cursor.
        async for item in chat_turns.read(turn):
            yield item if isinstance(item, str) else item.data

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/{session_id}/cancel")
async def v1_cancel_chat(session_id: str, key_record: dict = Depends(require_api_key)):
    """Stop the turn running in this conversation.

    A v1 turn carries on when its caller stops reading, so stopping it is its own
    call — the shape `/api/v2/chat/{session_id}/cancel` has. `stopped` is false
    when nothing was running: not an error, the turn may simply have finished.
    """
    from core.routes.chat import cancel_chat
    result = await cancel_chat(session_id)
    return {"status": "cancellation_requested", "session_id": session_id, "stopped": result["stopped"]}


@router.post("/chat/{session_id}/approve")
async def v1_approve_chat_tool(session_id: str, body: V1ApprovalRequest,
                               key_record: dict = Depends(require_api_key)):
    """Answer the tool call this conversation stopped on, and carry the turn on (SSE).

    The app's `/chat/{id}/approve`, in v1's frames. The question is claimed
    before it is spent, so an answer while another turn runs here is a 409 with
    the question still open. Naming the call (`approval_id`) makes an answer to
    an older question a 409 rather than an answer to this one.
    """
    from core import chat_turns
    from core.chat_approval import resume_state_for
    from core.models import ChatRequest
    from core.routes.chat import TURN_RUNNING
    from core.store import sessions as _sessions
    import core.server as _server

    agent = await _agent_or_error(body.agent)
    try:
        with chat_turns.claim(session_id, agent_key=agent["id"]) as turn:
            pending = await _sessions.get_pending_approval(session_id, agent["id"])
            if not pending:
                raise HTTPException(status_code=409, detail="This conversation is not waiting on an approval.")
            if body.approval_id is not None and body.approval_id != pending.get("approval_id"):
                raise HTTPException(status_code=409, detail="That approval is not the one this conversation is waiting on.")
            await _sessions.set_pending_approval(session_id, agent["id"], None)

            resumed = ChatRequest(message=pending.get("message") or "", session_id=session_id,
                                  agent_id=agent["id"])
            turn.message = resumed.message
            turn.start(_v1_turn_events(resumed, agent, _server,
                                       resume_state_for(pending, approve=body.approve, reason=body.reason)))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)

    async def event_generator():
        async for item in chat_turns.read(turn):
            yield item if isinstance(item, str) else item.data

    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _v1_drain(queue):
    """A run's queued events as v1 SSE: `_log_*` held back, `done` after a pause or the end.

    The run itself is spawned by the handler (`spawn_engine_run`); this only
    reads its queue, so a caller that stops reading leaves the run running (F49).
    """
    async def body():
        from core.orchestration.runner import SENTINEL
        from core.react_engine import SSE_HEARTBEAT

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                yield SSE_HEARTBEAT
                continue
            if event is SENTINEL:
                break
            etype = event.get("type", "")
            if etype.startswith("_log_"):
                continue
            yield _format_sse_event(event)
            if etype == "human_input_required":
                break
        yield _format_sse_event({"type": "done"})
    return body()


async def _workspace_orchestration(orch_id: str) -> dict:
    """An orchestration v1 may run or read: the workspace's, never the AI builder's own (F96).

    F162 left it out of the list; by id it was still run, streamed and served.
    MCP's point-load (`builder_tools._visible_orchestration`), so the two agree.
    """
    from core.builder_tools import _visible_orchestration

    orch = await _visible_orchestration(orch_id)
    if orch is None:
        raise HTTPException(status_code=404, detail=f"Orchestration '{orch_id}' not found")
    return orch


async def _refuse_builder_run(run_id: str) -> None:
    """404 a run of the AI builder's own orchestration (F96), as MCP's `_served_run` does.

    Narrower than MCP's check on purpose: a run whose orchestration has since
    been deleted is still read back here, as v1 and the CLI always have.
    """
    from core.builder_tools import _is_visible
    from core.orchestration.run_view import run_orchestration_id
    from core.store.resources import get_orchestration

    orch_id = await run_orchestration_id(run_id)
    orch = await get_orchestration(orch_id) if orch_id else None
    if orch is not None and not _is_visible(orch):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")


async def _answer(request: Request, run_id: str, human_response):
    """Resume a paused run once — the helper the app and a channel reply use."""
    from core.orchestration.answers import (
        EmptyAnswer, RunAlreadyActive, RunNotWaiting, answer_paused_run, refuse_empty,
    )
    try:
        refuse_empty(human_response)
    except EmptyAnswer as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await _refuse_builder_run(run_id)
    try:
        return await answer_paused_run(request.app.state, run_id, human_response)
    except RunAlreadyActive:
        raise HTTPException(status_code=409, detail="Run is already active")
    except RunNotWaiting as exc:
        if exc.status is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        raise HTTPException(status_code=409, detail=f"The run is {exc.status} and is not waiting for an answer.")


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# ── Orchestration Endpoints ─────────────────────────────────────────────────

@router.post("/orchestrations/{orch_id}/run")
async def v1_orchestration_run(
    orch_id: str,
    body: V1OrchestrationRunRequest,
    key_record: dict = Depends(require_api_key),
):
    """Start an orchestration (sync). Returns final result or human_input_required."""
    import core.server as _server
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import stream_engine_events

    orch_data = await _workspace_orchestration(orch_id)

    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, _server)

    final_response = None
    human_input_event = None
    step_history = []
    shared_state = {}
    status = "running"

    # Through the runner pump so the run is journaled, registered for
    # cancellation, and survives a dropped client (the engine itself is
    # advanced by the pump's task — issue #356).
    async for event in stream_engine_events(
            engine.run(body.message, run_id, initial_state=body.initial_state or None), run_id):
        etype = event.get("type", "")

        if etype == "human_input_required":
            human_input_event = event
            status = "paused"
            break

        if etype == "orchestration_error":
            # The pump surfaces engine exceptions as events instead of raising.
            status = "failed"
            final_response = final_response or f"Orchestration error: {event.get('error', '')}"

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
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch_data = await _workspace_orchestration(orch_id)

    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, _server)

    # Spawned here, not inside the response: the run belongs to no reader.
    from core.orchestration.runner import spawn_engine_run
    _task, queue = spawn_engine_run(
        engine.run(body.message, run_id, initial_state=body.initial_state or None), run_id)
    return StreamingResponse(_v1_drain(queue), media_type="text/event-stream", headers=_SSE_HEADERS)


# ── Orchestration Resume Endpoints ──────────────────────────────────────────

@router.post("/orchestrations/runs/{run_id}/resume")
async def v1_orchestration_resume(
    run_id: str,
    body: V1ResumeRequest,
    request: Request,
    key_record: dict = Depends(require_api_key),
):
    """Submit human input and resume orchestration (sync)."""
    # Normalize response to dict
    human_response = body.response
    if isinstance(human_response, str):
        human_response = {"response": human_response}

    final_response = None
    human_input_event = None
    step_history = []
    shared_state = {}
    status = "running"
    _allowed_statuses = {"running", "paused", "completed", "failed", "cancelled"}

    # Answered once, through the helper the app and a channel reply use: a run
    # not waiting is a 404/409, a second answer while it runs a 409, and a run
    # a worker ran goes back to the fleet.
    answered = await _answer(request, run_id, human_response)
    if answered.on_fleet:
        return {"status": "resuming", "run_id": run_id}

    async def _events():
        from core.orchestration.runner import SENTINEL
        while (event := await answered.queue.get()) is not SENTINEL:
            yield event

    try:
        async for event in _events():
            etype = event.get("type", "")

            if etype == "human_input_required":
                human_input_event = event
                status = "paused"
                break

            if etype == "orchestration_error":
                # The pump surfaces engine exceptions as events instead of
                # raising; keep the pre-pump HTTP semantics for "not found".
                if str(event.get("error", "")) == "Run not found":
                    raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
                status = "failed"
                final_response = final_response or f"Orchestration error: {event.get('error', '')}"

            if etype == "orchestration_complete":
                _raw_status = event.get("status", "completed")
                status = _raw_status if _raw_status in _allowed_statuses else "completed"
                shared_state = event.get("final_state", {})

            if etype == "final":
                final_response = event.get("response", "")
                step_history = event.get("data", {}).get("step_history", []) if event.get("data") else []

    except HTTPException:
        raise
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
    request: Request,
    key_record: dict = Depends(require_api_key),
):
    """Submit human input and resume orchestration (SSE stream)."""
    # Normalize response to dict
    human_response = body.response
    if isinstance(human_response, str):
        human_response = {"response": human_response}

    answered = await _answer(request, run_id, human_response)
    if answered.on_fleet:
        from core.scale import event_bridge
        return StreamingResponse(
            event_bridge.stream_run_events(request.app.state.redis, run_id, "0"),
            media_type="text/event-stream", headers=_SSE_HEADERS,
        )
    return StreamingResponse(_v1_drain(answered.queue), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# Declared before `/orchestrations/{orch_id}`, which would read `runs` as an id.
@router.get("/orchestrations/runs")
async def v1_list_runs(
    limit: int = Query(20, ge=1, le=200),
    status: Literal["running", "paused", "completed", "failed", "cancelled"] | None = None,
    orchestration_id: str | None = None,
    key_record: dict = Depends(require_api_key),
):
    """Recent runs of the workspace's orchestrations, newest first (D126).

    The list MCP's `list_runs` reads too (`core/orchestration/run_view.py`), so
    the two cannot drift. The filters are cloud's `GET /orchestrations/runs`'.
    """
    from core.orchestration.run_view import list_runs
    return await list_runs(limit=limit, status=status, orchestration_id=orchestration_id)


@router.get("/orchestrations/runs/{run_id}/events")
async def v1_run_events(run_id: str, request: Request, after: int = Query(0, ge=0),
                        limit: int = Query(5000, ge=1, le=5000),
                        key_record: dict = Depends(require_api_key)):
    """A run's events after `after`, oldest first, with `last_id` to read on from (D126).

    The app's run log (`GET /api/orchestrations/runs/{id}/events`), unchanged.
    """
    from core.routes.orchestrations import get_run_events
    await _refuse_builder_run(run_id)
    return await get_run_events(run_id, request, after=after, limit=limit)


@router.get("/orchestrations/runs/{run_id}/events/stream")
async def v1_run_events_stream(run_id: str, request: Request, after: int = Query(0, ge=0),
                               key_record: dict = Depends(require_api_key)):
    """SSE: the run's events after `after` (or `Last-Event-ID`), then followed live (D126).

    Frames carry `id:` lines to reconnect from; `stream_complete` ends the
    stream once the run has ended. The app's reattach stream, unchanged.
    """
    from core.routes.orchestrations import stream_run_events_sse
    await _refuse_builder_run(run_id)
    return await stream_run_events_sse(run_id, request, after=after)


@router.get("/orchestrations/runs/{run_id}")
async def v1_get_run(run_id: str, key_record: dict = Depends(require_api_key)):
    """A run's state and, once it has ended, its result (F117).

    A run carries on when its caller stops reading (F49), so a caller that
    dropped `run/stream` or `resume/stream` needs a way back to what it would
    have been sent — and v2's status route is scale-mode only and carries no
    result. The shapes are the sync routes': a paused run names its question,
    an ended one carries `response`, `shared_state` and `step_history`. Engine
    bookkeeping is left out of `shared_state`, as the app's own read leaves it.
    MCP's `get_run` reads the same view (`core/orchestration/run_view.py`).
    """
    from core.orchestration.run_view import read_run

    await _refuse_builder_run(run_id)
    view = await read_run(run_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return view


@router.post("/orchestrations/runs/{run_id}/cancel")
async def v1_cancel_run(run_id: str, request: Request, key_record: dict = Depends(require_api_key)):
    """Stop a run. A run carries on when its caller stops reading, so this is how it stops.

    v2's shape, plus `stopped`: false for a run that had already finished, which
    keeps the status it finished with.
    """
    from core.orchestration.cancel import cancel_run

    await _refuse_builder_run(run_id)
    result = await cancel_run(run_id, redis=getattr(request.app.state, "redis", None))
    if not result.found:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return {"status": "cancellation_requested", "run_id": run_id, "stopped": result.stopped}


# ── Schedules (D126) ────────────────────────────────────────────────────────
# The app's schedule routes (`core/routes/schedules.py`), behind an API key.


@router.get("/schedules")
async def v1_list_schedules(key_record: dict = Depends(require_api_key)):
    from core.routes.schedules import list_schedules
    return await list_schedules()


@router.post("/schedules")
async def v1_create_schedule(body: ScheduleCreate, request: Request,
                             key_record: dict = Depends(require_api_key)):
    from core.routes.schedules import create_schedule
    return await create_schedule(body, request)


@router.get("/schedules/{schedule_id}")
async def v1_get_schedule(schedule_id: str, key_record: dict = Depends(require_api_key)):
    from core.routes.schedules import get_schedule
    return await get_schedule(schedule_id)


@router.patch("/schedules/{schedule_id}")
async def v1_patch_schedule(schedule_id: str, body: ScheduleUpdate, request: Request,
                            key_record: dict = Depends(require_api_key)):
    """A partial update; `{"enabled": false}` pauses, `{"enabled": true}` resumes."""
    from core.routes.schedules import patch_schedule
    return await patch_schedule(schedule_id, body, request)


@router.delete("/schedules/{schedule_id}")
async def v1_delete_schedule(schedule_id: str, key_record: dict = Depends(require_api_key)):
    from core.routes.schedules import delete_schedule
    return await delete_schedule(schedule_id)


@router.post("/schedules/{schedule_id}/run")
async def v1_run_schedule_now(schedule_id: str, request: Request,
                              key_record: dict = Depends(require_api_key)):
    """Run it now, as a test: a paused one too, and its next run does not move (F72)."""
    from core.routes.schedules import run_schedule_now
    return await run_schedule_now(schedule_id, request)


# ── Read-Only Discovery Endpoints ──────────────────────────────────────────

@router.get("/agents")
async def v1_list_agents(key_record: dict = Depends(require_api_key)):
    """List all configured agents (id, name, type, capabilities)."""
    agents = await _api_agents()
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
    agents = await _api_agents()
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
    """List the workspace's orchestrations (id, name, steps summary).

    The AI builder's own seeded orchestration is left out, as the app's list
    and v1's agent list (F69) leave it out (F162).
    """
    from core.routes.orchestrations import list_orchestrations
    orchs = await list_orchestrations()
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
    """The orchestration's definition — its steps and how they connect.

    It used to answer in a shape the definitions stopped having (`label`,
    `depends_on`, `edges`), so every step came back unlabelled and unconnected
    (F69). Validated, so what comes back is the model's current fields and
    not whatever older keys a stored document still carries.
    """
    from core.models_orchestration import Orchestration
    orch = await _workspace_orchestration(orch_id)
    return Orchestration.model_validate(orch).model_dump(mode="json")

