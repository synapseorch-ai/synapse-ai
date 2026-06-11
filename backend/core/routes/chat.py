"""
Chat endpoints: /chat and /chat/stream
Thin wrappers around the shared ReAct engine (core.react_engine).
"""
import json
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.models import ChatRequest, ChatResponse
from core.react_engine import run_react_loop, parse_tool_call  # noqa: F401 — re-export for backwards compat

router = APIRouter()


def _resolve_agent(request: ChatRequest):
    """Return the agent dict for the request's agent_id (or the active agent)."""
    from core.routes.agents import load_user_agents, get_active_agent_data
    agent_id = getattr(request, "agent_id", None)
    if agent_id:
        agents = load_user_agents()
        return next((a for a in agents if a["id"] == agent_id), None)
    try:
        return get_active_agent_data()
    except RuntimeError:
        return None


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=500, detail="No agents connected")

    final_event = None
    async for event in run_react_loop(request, _server):
        if event["type"] == "final":
            final_event = event
        elif event["type"] == "error":
            return ChatResponse(response=event["message"], intent="chat", data=None, tool_name=None)

    if not final_event:
        return ChatResponse(response="I completed the requested actions.", intent="chat", data=None, tool_name=None)

    return ChatResponse(
        response=final_event["response"],
        intent=final_event["intent"],
        data=final_event.get("data"),
        tool_name=final_event.get("tool_name"),
    )


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Real-time streaming endpoint with SSE"""

    async def event_generator():
        import core.server as _server

        # Route builder agents to the dedicated builder stream
        agent = _resolve_agent(request)
        if agent and agent.get("type") == "builder":
            from core.routes.builder import run_builder_stream_compat
            async for chunk in run_builder_stream_compat(request, _server):
                yield chunk
            return

        try:
            async for event in run_react_loop(request, _server):
                etype = event["type"]

                if etype == "status":
                    yield f"data: {json.dumps({'type': 'status', 'message': event['message']})}\n\n"

                elif etype == "thinking":
                    yield f"data: {json.dumps({'type': 'thinking', 'message': event.get('message', ''), 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')})}\n\n"

                elif etype == "tool_execution":
                    yield f"data: {json.dumps({'type': 'tool_execution', 'tool_name': event['tool_name'], 'args': event['args'], 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')})}\n\n"

                elif etype == "tool_result":
                    yield f"data: {json.dumps({'type': 'tool_result', 'tool_name': event['tool_name'], 'preview': event['preview'], 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')})}\n\n"

                elif etype == "llm_reasoning":
                    # Private chain-of-thought extracted from [REASONING] blocks.
                    # Rendered as a dedicated "thinking" panel above the tool call
                    # in the chat UI / orchestration step view.
                    yield f"data: {json.dumps({'type': 'llm_reasoning', 'reasoning': event['reasoning'], 'turn': event.get('turn', 1), 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}, default=str)}\n\n"

                elif etype == "llm_thought":
                    # Action text (tool-call JSON or final-response prose) with
                    # reasoning already stripped — carries orch_step_id & step_name
                    # when inside orchestration.
                    yield f"data: {json.dumps({'type': 'llm_thought', 'thought': event['thought'], 'turn': event.get('turn', 1), 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}, default=str)}\n\n"

                elif etype == "final":
                    # Sub-agent step final (inside orchestration) → agent_step_result
                    # Distinguished by the presence of orch_step_id (added by AgentStepExecutor)
                    if event.get("orch_step_id"):
                        yield f"data: {json.dumps({'type': 'agent_step_result', 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name', ''), 'content': event.get('response', ''), 'intent': event.get('intent', 'chat'), 'data': event.get('data'), 'tool_name': event.get('tool_name')}, default=str)}\n\n"
                    else:
                        # Top-level final (single agent or orchestration summary) → response + done
                        yield f"data: {json.dumps({'type': 'response', 'content': event.get('response', ''), 'intent': event.get('intent', 'chat'), 'data': event.get('data'), 'tool_name': event.get('tool_name')}, default=str)}\n\n"
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"

                elif etype == "error":
                    yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"

                # ── Orchestration lifecycle events ────────────────────────────────
                elif etype == "orchestration_start":
                    yield f"data: {json.dumps({'type': 'orchestration_start', 'run_id': event.get('run_id'), 'orchestration_name': event.get('orchestration_name'), 'orchestration_id': event.get('orchestration_id')}, default=str)}\n\n"

                elif etype == "step_start":
                    yield f"data: {json.dumps({'type': 'step_start', 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name'), 'step_type': event.get('step_type')}, default=str)}\n\n"

                elif etype == "step_complete":
                    yield f"data: {json.dumps({'type': 'step_complete', 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name'), 'duration_seconds': event.get('duration_seconds')}, default=str)}\n\n"

                elif etype == "step_error":
                    yield f"data: {json.dumps({'type': 'step_error', 'orch_step_id': event.get('orch_step_id'), 'error': event.get('error')}, default=str)}\n\n"

                elif etype == "orchestration_complete":
                    yield f"data: {json.dumps({'type': 'orchestration_complete', 'run_id': event.get('run_id'), 'status': event.get('status')}, default=str)}\n\n"

                elif etype == "orchestration_error":
                    yield f"data: {json.dumps({'type': 'orchestration_error', 'error': event.get('error')}, default=str)}\n\n"

                elif etype == "human_input_required":
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                    # Send done so the loading spinner stops — user must submit the form
                    yield f"data: {json.dumps({'type': 'done'})}\n\n"
                    return

                elif etype in (
                    "parallel_start", "parallel_complete", "branch_start",
                    "loop_iteration", "loop_complete", "merge_complete",
                    "transform_result", "routing_decision",
                    "loop_limit_reached", "step_warning",
                ):
                    yield f"data: {json.dumps(event, default=str)}\n\n"

                await asyncio.sleep(0)

        except Exception as e:
            print(f"ERROR in SSE stream: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
