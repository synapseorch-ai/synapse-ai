"""
Chat endpoints: /chat and /chat/stream
Thin wrappers around the shared ReAct engine (core.react_engine).

A turn is started by the route *handler* and runs in a task of its own
(`core.chat_turns`); the response only reads it. So a reader leaving — a closed
tab, a reload, a laptop waking up — ends that reader, never the turn (F49). Stop
is `/chat/{id}/cancel`, and `GET /chat/{id}/events` reads a turn again from a
cursor.
"""
import json
import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core import chat_turns
from core.models import ChatRequest, ChatResponse
from pydantic import BaseModel
from core.react_engine import run_react_loop, parse_tool_call, iter_with_heartbeat  # noqa: F401 — re-export for backwards compat

router = APIRouter()

#: What a second message gets while a turn is still running on its conversation.
TURN_RUNNING = "A reply is still being written in this conversation."


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


def _start(request: ChatRequest, resume_state: dict | None = None, *,
           expand_mentions: bool) -> chat_turns.ChatTurn:
    """Start the turn from the handler. Raises `TurnAlreadyRunning`.

    `expand_mentions` has no default on purpose: every caller says whose text
    this is — a member's (`@[path]` reads the vault) or a widget visitor's (it
    never does).
    """
    import core.server as _server

    with chat_turns.claim(request.session_id, message=request.message or "",
                          agent_key=request.agent_id or "default") as turn:
        turn.start(_event_stream(request, _server, resume_state,
                                 expand_mentions=expand_mentions))
    return turn


def _read(turn: chat_turns.ChatTurn, after: int = 0):
    """The turn's events for one reader, each with its cursor."""
    async def body():
        async for item in chat_turns.read(turn, after):
            yield item if isinstance(item, str) else chat_turns.frame(turn, item)
    return body()


def _summarize(turn: chat_turns.ChatTurn) -> ChatResponse:
    """A finished turn as the one-shot `/chat` answer."""
    pause = None
    for entry in turn.entries:
        event = entry.event
        kind = event.get("type")
        if kind == "response":
            return ChatResponse(
                response=event.get("content", ""), intent=event.get("intent", "chat"),
                data=event.get("data"), tool_name=event.get("tool_name"),
            )
        if kind == "error":
            return ChatResponse(response=event.get("message", ""), intent="chat", data=None, tool_name=None)
        if kind == "orchestration_error":
            # An orchestrator agent failed — same outcome, different event shape.
            return ChatResponse(response=event.get("error", ""), intent="chat", data=None, tool_name=None)
        if kind == "cancelled":
            return ChatResponse(response=event.get("message") or "Stopped.", intent="cancelled",
                                data=None, tool_name=None)
        if kind == "human_input_required" and pause is None:
            from core.routes.api_v1 import _pause_of
            pause = _pause_of(event)

    if pause:
        # Stopped for a person, not finished (F68): a tool call awaiting approval
        # or an orchestration paused on a human step.
        status = pause.pop("status")
        prompt = pause.pop("prompt")
        return ChatResponse(response=prompt, intent=status, data=pause, tool_name=None)
    return ChatResponse(response="I completed the requested actions.", intent="chat", data=None, tool_name=None)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    import core.server as _server
    if not _server.agent_sessions:
        raise HTTPException(status_code=500, detail="No agents connected")
    try:
        turn = _start(request, expand_mentions=True)
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    # Waiting is not owning: this request going away leaves the turn running.
    await turn.wait()
    return _summarize(turn)


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Real-time streaming endpoint with SSE"""
    try:
        turn = _start(request, expand_mentions=True)
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return _sse(_read(turn))


@router.get("/chat/{session_id}/events")
async def chat_events(session_id: str, request: Request, after: str | None = None):
    """Read a conversation's turn again — the live one, or the one just ended.

    `after` (or `Last-Event-ID`) is the cursor of the last event already seen;
    without it the turn is read from its start. 404 when the conversation has no
    turn held here (it finished long ago, or never ran in this process — its
    answer is in the conversation); 410 when the cursor belongs to a turn that
    has since been replaced.
    """
    turn = chat_turns.find(session_id)
    if turn is None:
        return JSONResponse(status_code=404, content={"detail": "No turn is running in this conversation."})
    try:
        after_seq = chat_turns.parse_cursor(turn, after or request.headers.get("last-event-id"))
    except chat_turns.TurnGone:
        return JSONResponse(status_code=410, content={"detail": "That turn has ended; read the conversation instead."})
    return _sse(_read(turn, after_seq))


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
    """One chat turn's events as SSE frames — the producer a turn's task drives."""
    # Route builder agents to the dedicated builder stream
    agent = await _resolve_agent(request)
    if agent and agent.get("type") == "builder":
        from core.routes.builder import run_builder_stream_compat
        async for chunk in run_builder_stream_compat(request, _server):
            yield chunk
        return

    turn = chat_turns.current_turn.get()
    if turn is not None and agent and agent.get("type") != "orchestrator":
        # Stopped before it answered, a turn still keeps its question — as the
        # fleet's chat job does. Otherwise Stop destroyed the exchange rather
        # than ending it, and a reopened conversation lost what was asked.
        # Saved under the id the engine saves a finished turn under. An
        # orchestrator agent's turns are not saved at all, stopped or not.
        async def _save_stopped(agent_id=agent.get("id"), session_id=request.session_id,
                                message=request.message or ""):
            from core.session import _save_conversation_turn
            await _save_conversation_turn(session_id=session_id or "default", agent_id=agent_id,
                                          user=message, assistant=chat_turns.STOPPED_ANSWER)
        turn.on_stop = _save_stopped

    async def _stopped() -> bool:
        # The turn's own flag: a Stop that arrived after an earlier turn ended
        # must not end this one.
        return bool(turn is not None and turn.stop_requested)

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
    # In-process (single-process product): the turn's task is right here. Only
    # a live turn is flagged — a flag left for a turn that had already ended
    # used to end the conversation's *next* turn at its first boundary.
    stopped = await chat_turns.stop(session_id)

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
    import core.server as _server

    agent_key = decision.agent_id or "default"
    # The conversation is claimed before the question is spent: answering while
    # another turn runs here is refused with the question still open, rather
    # than cleared and then refused.
    try:
        with chat_turns.claim(session_id, agent_key=agent_key) as turn:
            pending = await _sessions.get_pending_approval(session_id, agent_key)
            if not pending:
                raise HTTPException(status_code=409, detail="This conversation is not waiting on an approval.")

            # Cleared before the turn runs, not after. A resume that crashed with
            # the row still set would re-open a question the person has already
            # answered.
            await _sessions.set_pending_approval(session_id, agent_key, None)

            from core.chat_approval import resume_state_for
            resume_state = resume_state_for(pending, approve=decision.approve, reason=decision.reason)

            resumed = ChatRequest(
                message=pending.get("message") or "",
                session_id=session_id,
                agent_id=None if agent_key == "default" else agent_key,
            )
            turn.message = resumed.message
            turn.start(_event_stream(resumed, _server, resume_state, expand_mentions=True))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return _sse(_read(turn))
