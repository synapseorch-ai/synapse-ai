"""
Chat endpoints: /chat and /chat/stream
Thin wrappers around the shared ReAct engine (core.react_engine).
"""
import json
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.models import ChatRequest, ChatResponse
from pydantic import BaseModel
from core.react_engine import run_react_loop, parse_tool_call, iter_with_heartbeat  # noqa: F401 — re-export for backwards compat

router = APIRouter()


#: Chat turns running in *this* process, by session id.
#:
#: The single-process product has no worker and no Redis, so there is nothing
#: to publish a stop signal to — the task doing the work is right here. Same
#: shape as `core.orchestration.runner._active_tasks`, and the same reason:
#: cancelling the task reaches a turn that is inside an LLM call, which a flag
#: checked between turns cannot.
#:
#: Scale mode keeps its own path (the Redis key the worker watches). A
#: deployment may be running both, so `/chat/{id}/cancel` does both and reports
#: whether anything was actually there to stop.
_ACTIVE_CHAT_TASKS: dict[str, asyncio.Task] = {}

#: Sessions asked to stop. Two mechanisms, because they fail in opposite ways.
#:
#: The flag is the clean one: the loop consults it between turns and raises,
#: which unwinds through the generator and leaves a `cancelled` event and a
#: `done` on the stream — so the client learns why the turn ended. It cannot
#: reach a turn already inside one long LLM call.
#:
#: Cancelling the task can. What it cannot do is speak afterwards: the response
#: task is the one being cancelled, so anything yielded after that point may
#: never reach the wire. Setting the flag first gives the loop a chance to end
#: cleanly, and the cancel is what stops a wedged turn from running on.
_STOP_REQUESTED: set[str] = set()


async def _resolve_agent(request: ChatRequest):
    """Return the agent dict for the request's agent_id (or the active agent)."""
    from core.routes.agents import get_active_agent_data
    from core.scale.context import resolve_agent

    agent_id = getattr(request, "agent_id", None)
    if agent_id:
        return await resolve_agent(agent_id)
    try:
        return await get_active_agent_data()
    except RuntimeError:
        return None


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=500, detail="No agents connected")

    final_event = None
    pause = None
    async for event in run_react_loop(request, _server, expand_mentions=True):
        if event["type"] == "final":
            final_event = event
        elif event["type"] == "human_input_required":
            from core.routes.api_v1 import _pause_of
            pause = _pause_of(event)
        elif event["type"] == "error":
            return ChatResponse(response=event["message"], intent="chat", data=None, tool_name=None)
        elif event["type"] == "orchestration_error":
            # An orchestrator agent failed — same outcome, different event shape.
            return ChatResponse(response=event["error"], intent="chat", data=None, tool_name=None)

    if not final_event and pause:
        # Stopped for a person, not finished (F68): a tool call awaiting approval
        # or an orchestration paused on a human step.
        status = pause.pop("status")
        prompt = pause.pop("prompt")
        return ChatResponse(response=prompt, intent=status, data=pause, tool_name=None)

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
    return _sse(_guarded_stream(request, expand_mentions=True))


def _guarded_stream(request: ChatRequest, resume_state: dict | None = None, *,
                    expand_mentions: bool):
    """The turn's events, with the session claimed and a stop handled.

    Shared by the first turn and by a resume after a tool approval: both are
    the same loop, they only differ in what seeds it. `expand_mentions` has no
    default on purpose: every caller says whose text this is — a member's
    (`@[path]` reads the vault) or a widget visitor's (it never does).
    """
    async def event_generator():
        import core.server as _server

        # Claim this session for the duration of the turn. Registered from
        # inside the generator because that is the task actually streaming —
        # the request handler has already returned by the time this runs.
        _session_key = getattr(request, "session_id", None)
        _task = asyncio.current_task()
        if _session_key and _task is not None:
            _ACTIVE_CHAT_TASKS[_session_key] = _task

        try:
            async for chunk in _event_stream(request, _server, resume_state,
                                             expand_mentions=expand_mentions):
                yield chunk
        except asyncio.CancelledError:
            # Stopped on purpose: say so and close the stream cleanly, rather
            # than letting the client wait out a socket that will never speak
            # again.
            yield f"data: {json.dumps({'type': 'cancelled', 'message': 'Stopped.'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        finally:
            if _session_key and _ACTIVE_CHAT_TASKS.get(_session_key) is _task:
                _ACTIVE_CHAT_TASKS.pop(_session_key, None)
            _STOP_REQUESTED.discard(_session_key or "")

    return event_generator()


def _sse(generator):
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_stream(request, _server, resume_state: dict | None = None, *,
                        expand_mentions: bool = False):
    # Route builder agents to the dedicated builder stream
    agent = await _resolve_agent(request)
    if agent and agent.get("type") == "builder":
        from core.routes.builder import run_builder_stream_compat
        async for chunk in run_builder_stream_compat(request, _server):
            yield chunk
        return

    async def _stopped() -> bool:
        return getattr(request, "session_id", None) in _STOP_REQUESTED

    try:
        # The last turn-boundary snapshot. Kept so a pause can be resumed from
        # it rather than from the top: without it, allowing a gated call means
        # re-running every tool that already succeeded this turn, side effects
        # and all. Never forwarded — it is engine state, not an event.
        latest_progress: dict | None = None

        async for event in iter_with_heartbeat(
            run_react_loop(request, _server, should_stop=_stopped,
                           progress_events=True, resume_state=resume_state,
                           expand_mentions=expand_mentions)
        ):
            # Heartbeat comments are yielded as raw SSE strings — pass through.
            if isinstance(event, str):
                yield event
                continue
            etype = event["type"]

            # Whose event this is. The engine tags everything a delegate
            # produces (`react_engine`, the delegate_to_agent branch); rebuilt
            # without these the browser cannot tell a sub-agent's work from the
            # work of the agent the person actually asked (F7). Absent for the
            # main agent's own events, so the keys stay off those entirely.
            def whose(payload: dict) -> dict:
                if event.get("delegate_from"):
                    payload["delegate_from"] = event["delegate_from"]
                    payload["delegate_agent_name"] = event.get("delegate_agent_name")
                return payload

            if etype == "_step_progress":
                latest_progress = {k: v for k, v in event.items() if k != "type"}
                continue

            if etype == "status":
                yield f"data: {json.dumps({'type': 'status', 'message': event['message']})}\n\n"

            elif etype == "thinking":
                yield f"data: {json.dumps(whose({'type': 'thinking', 'message': event.get('message', ''), 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}))}\n\n"

            elif etype == "tool_execution":
                yield f"data: {json.dumps(whose({'type': 'tool_execution', 'tool_name': event['tool_name'], 'args': event['args'], 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}))}\n\n"

            elif etype == "tool_result":
                yield f"data: {json.dumps(whose({'type': 'tool_result', 'tool_name': event['tool_name'], 'preview': event['preview'], 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}))}\n\n"

            elif etype == "llm_reasoning":
                # Private chain-of-thought extracted from [REASONING] blocks.
                # Rendered as a dedicated "thinking" panel above the tool call
                # in the chat UI / orchestration step view.
                yield f"data: {json.dumps(whose({'type': 'llm_reasoning', 'reasoning': event['reasoning'], 'turn': event.get('turn', 1), 'orch_step_id': event.get('orch_step_id'), 'step_name': event.get('step_name')}), default=str)}\n\n"

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
                # A tool call waiting on a person is the one pause a chat turn
                # can hit and never come back from: an orchestration run
                # resumes from its own row, and a chat turn has no run. Park
                # the call and the snapshot so `/chat/{id}/approve` can pick
                # the turn up where it stopped.
                if event.get("kind") == "tool_approval":
                    from core.chat_approval import parked_payload
                    from core.store import sessions as _sessions
                    await _sessions.set_pending_approval(
                        getattr(request, "session_id", "") or "",
                        getattr(request, "agent_id", None) or "default",
                        parked_payload(
                            event,
                            message=getattr(request, "message", "") or "",
                            progress=latest_progress,
                        ),
                    )
                yield f"data: {json.dumps(event, default=str)}\n\n"
                # Send done so the loading spinner stops — user must submit the form
                yield f"data: {json.dumps({'type': 'done'})}\n\n"
                return

            elif etype in (
                "parallel_start", "parallel_complete", "branch_start",
                "loop_iteration", "loop_complete", "merge_complete",
                "transform_result", "routing_decision", "evaluator_decision",
                "loop_limit_reached", "step_warning",
                # Things the loop does to keep going, which the person is
                # entitled to know about. All four were emitted by the
                # engine and dropped here, so the only visible trace of a
                # truncated answer or a discarded middle was that the reply
                # read oddly.
                "truncated_output", "context_trimmed",
                "tool_call_unparsed", "tool_approval_replay",
                "cancelled",
            ):
                yield f"data: {json.dumps(event, default=str)}\n\n"

            await asyncio.sleep(0)

    except Exception as e:
        print(f"ERROR in SSE stream: {e}")
        import traceback
        traceback.print_exc()
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


@router.post("/chat/{session_id}/cancel")
async def cancel_chat(session_id: str):
    """Stop the turn running for this chat session.

    Answers `"cancelling"`, never `"cancelled"` — the same word
    `/orchestrations/runs/{id}/cancel` uses, for the same reason. The request
    asks; what it cannot do is promise the turn has already noticed. A turn
    inside a long provider call stops when that call returns.

    `stopped` says whether anything was found to stop: false means the turn had
    already finished, which is not an error and should not read as one.
    """
    stopped = False

    # In-process (single-process product): the task is right here.
    _STOP_REQUESTED.add(session_id)
    task = _ACTIVE_CHAT_TASKS.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        stopped = True

    # Scale mode: the turn is on a worker, so the signal goes through Redis.
    # Both paths run — a deployment can be serving chat in-process while the
    # queue is enabled, and the caller should not have to know which one ran
    # this turn.
    try:
        import core.server as _server
        redis = getattr(_server, "redis_client", None)
        if redis is not None:
            from core.scale.pubsub import publish_chat_cancellation
            await publish_chat_cancellation(redis, session_id)
            stopped = True
    except Exception:  # noqa: BLE001 — no Redis is not a failure to cancel
        pass

    return {"status": "cancelling", "session_id": session_id, "stopped": stopped}


class ApprovalDecision(BaseModel):
    """A person's answer to "may this tool call run?"."""
    approve: bool
    reason: str = ""
    agent_id: str | None = None


@router.post("/chat/{session_id}/approve")
async def approve_chat_tool(session_id: str, decision: ApprovalDecision):
    """Answer the tool-call question this conversation is waiting on.

    The gate itself has existed since tool policies landed, but only
    orchestration runs could answer it: a run resumes from its own row, and a
    chat turn has no run. So in chat the pause was a dead end — the turn
    stopped with a question, and nothing in the product could reply to it.

    The turn is picked up from its last turn-boundary snapshot rather than
    started again, which is the whole reason the snapshot is kept. Starting
    again would re-run every tool that already succeeded before the pause, and
    those are the calls with side effects.

    An approval is spent on the exact call it was given for — `approval_id` is
    derived from the tool *and its arguments* — so allowing `ls` does not also
    allow `rm`.
    """
    from core.store import sessions as _sessions

    agent_key = decision.agent_id or "default"
    pending = await _sessions.get_pending_approval(session_id, agent_key)
    if not pending:
        raise HTTPException(status_code=409, detail="This conversation is not waiting on an approval.")

    # Cleared before the turn runs, not after. A resume that crashed with the
    # row still set would re-open a question the person has already answered.
    await _sessions.set_pending_approval(session_id, agent_key, None)

    from core.chat_approval import resume_state_for
    resume_state = resume_state_for(pending, approve=decision.approve, reason=decision.reason)

    resumed = ChatRequest(
        message=pending.get("message") or "",
        session_id=session_id,
        agent_id=None if agent_key == "default" else agent_key,
    )
    return _sse(_guarded_stream(resumed, resume_state=resume_state, expand_mentions=True))
