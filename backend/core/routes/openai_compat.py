"""
Every agent behind an OpenAI-compatible endpoint (OAI1, D243)
--------------------------------------------------------------
Any OpenAI client can talk to an agent: the official SDKs, tools that take a
`base_url`, and voice platforms' "custom LLM". A client sets

    base_url = {origin}/api/v1/openai        (self-host: http://localhost:3000/api/v1/openai)
    api_key  = sk-syn-…                       (a workspace API key)

and names the agent as the `model`, by id or by exact name.

  GET  /api/v1/openai/models             — the agents a key may use, workflow agents included
  POST /api/v1/openai/chat/completions   — one agent turn; `stream: true` for SSE chunks

What the agent is and does stays the agent's: its system prompt, model, tools,
skills and knowledge. The request's messages are the conversation (OpenAI is
stateless): earlier `user` / `assistant` messages are the history, the last
`user` message is the turn, and a client `system` / `developer` message is
added after the agent's own prompt as the caller's instructions. Nothing is
saved as a Chat conversation; each turn is in Logs (source `openai`).

A client's own `tools` are refused (400 `tools_unsupported`): the agent's tools
run here. Sampling fields (`temperature`, `top_p`, `max_tokens`, …) are
accepted and ignored; the agent's settings rule. A tool call that needs a
person's approval parks the turn and asks in the bell; the answer says it is
waiting for a person, and `synapse.status` is `awaiting_approval`.

Errors are OpenAI's shape: `{"error": {"message", "type", "param", "code"}}`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from core.api_keys import validate_api_key

router = APIRouter(prefix="/openai", tags=["OpenAI-compatible"])
#: The bell's Approve / Refuse for a turn parked here: an app route, behind the app's own auth.
approvals_router = APIRouter()
log = logging.getLogger("api_openai")

#: How long a parked approval waits for a person, as a schedule's does (F268).
APPROVAL_HOURS = 24


# ── Errors, in OpenAI's shape ────────────────────────────────────────────────

class OpenAIError(Exception):
    """An error a route answers with OpenAI's body (the app's handler renders it)."""

    def __init__(self, status: int, message: str, *, type_: str = "invalid_request_error",
                 param: str | None = None, code: str | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.body = {"error": {"message": message, "type": type_, "param": param, "code": code}}
        self.headers = headers


def openai_error_response(exc: OpenAIError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content=exc.body, headers=exc.headers)


async def handle_openai_error(_request: Request, exc: OpenAIError) -> JSONResponse:
    """Installed on the app (`server.py`): a route raises, the client reads OpenAI's body."""
    return openai_error_response(exc)


async def require_openai_key(request: Request) -> dict:
    """The workspace API key, as `Authorization: Bearer sk-syn-…`; 401 in OpenAI's shape."""
    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise OpenAIError(401, "No API key: send `Authorization: Bearer sk-syn-…`.",
                          code="missing_api_key", headers={"WWW-Authenticate": "Bearer"})
    record = await validate_api_key(token.strip())
    if not record:
        raise OpenAIError(401, "Invalid or revoked API key.", code="invalid_api_key",
                          headers={"WWW-Authenticate": "Bearer"})
    return record


# ── The request ──────────────────────────────────────────────────────────────

class _Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    """OpenAI's chat-completion body. Fields this endpoint does not use are accepted and ignored."""
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[_Message]
    stream: bool = False
    stream_options: dict | None = None
    tools: list | None = None
    functions: list | None = None
    tool_choice: Any = None
    function_call: Any = None
    n: int | None = None
    user: str | None = None


class Conversation(BaseModel):
    """The request's messages, read as a turn: the history, the message, the caller's instructions."""
    history: list[dict]
    message: str
    images: list[str]
    caller_system: str | None


def _text_and_images(content: Any, *, index: int, keep_images: bool) -> tuple[str, list[str]]:
    """A message's text and its images (data URIs), from a string or OpenAI's content parts."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise OpenAIError(400, f"messages[{index}].content must be a string or a list of content parts.",
                          param=f"messages[{index}].content")
    texts: list[str] = []
    images: list[str] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            texts.append(str(part.get("text") or ""))
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) \
                else part.get("image_url")
            if not isinstance(url, str) or not url.startswith("data:image/"):
                raise OpenAIError(400, "Images must be sent inline as data URIs (data:image/…;base64,…); "
                                       "this endpoint does not fetch URLs.",
                                  param=f"messages[{index}].content", code="image_url_unsupported")
            if keep_images:
                images.append(url)
        else:
            raise OpenAIError(400, f"Content part type '{kind}' is not supported: send text, or images as "
                                   "data URIs.", param=f"messages[{index}].content", code="unsupported_content")
    return "\n".join(t for t in texts if t), images


def read_conversation(body: ChatCompletionRequest) -> Conversation:
    """The turn a request describes, or the 400 that says why it is not one."""
    if body.tools or body.functions:
        raise OpenAIError(400, "This endpoint runs the agent's own tools; a request's `tools` are not "
                               "supported. Send the conversation only.",
                          param="tools" if body.tools else "functions", code="tools_unsupported")
    for name, value in (("tool_choice", body.tool_choice), ("function_call", body.function_call)):
        if value not in (None, "none", "auto"):
            raise OpenAIError(400, f"`{name}` is not supported: the agent decides which of its tools to use.",
                              param=name, code="tools_unsupported")
    if body.n is not None and body.n != 1:
        raise OpenAIError(400, "Only one choice per request (`n` = 1).", param="n")
    if not body.messages:
        raise OpenAIError(400, "`messages` is empty: send at least one user message.", param="messages")
    last = len(body.messages) - 1
    if body.messages[last].role != "user":
        raise OpenAIError(400, "The last message must be the user's: it is the turn the agent answers.",
                          param=f"messages[{last}].role")

    history: list[dict] = []
    system: list[str] = []
    message, images = "", []
    for i, msg in enumerate(body.messages):
        role = msg.role
        if role in ("tool", "function") or (role == "assistant" and (msg.model_extra or {}).get("tool_calls")):
            raise OpenAIError(400, "Tool messages are not supported: the agent's tools run on our side.",
                              param=f"messages[{i}]", code="tools_unsupported")
        if role not in ("system", "developer", "user", "assistant"):
            raise OpenAIError(400, f"Unknown role '{role}'.", param=f"messages[{i}].role")
        text, found = _text_and_images(msg.content, index=i, keep_images=(i == last))
        if role in ("system", "developer"):
            if text.strip():
                system.append(text.strip())
        elif i == last:
            message, images = text, found
        elif text.strip():
            history.append({"role": role, "content": text})
    if not message.strip() and not images:
        raise OpenAIError(400, "The last user message is empty.", param=f"messages[{last}].content")
    return Conversation(history=history, message=message, images=images,
                        caller_system="\n\n".join(system) or None)


async def read_body(request: Request) -> ChatCompletionRequest:
    try:
        raw = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise OpenAIError(400, "The body is not JSON.") from None
    try:
        return ChatCompletionRequest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or None
        raise OpenAIError(400, f"{first.get('msg', 'Invalid request')}" + (f" ({where})" if where else ""),
                          param=where) from None


# ── Agents as models ─────────────────────────────────────────────────────────

def model_entry(agent: dict) -> dict:
    created = agent.get("created_at")
    try:
        created = int(created) if isinstance(created, (int, float)) else int(time.time())
    except (TypeError, ValueError):
        created = int(time.time())
    return {"id": agent["id"], "object": "model", "created": created, "owned_by": "synapse",
            "name": agent.get("name") or agent["id"],
            "synapse": {"type": "workflow" if agent.get("type") == "orchestrator" else "agent"}}


async def _agent_for(model: str) -> dict:
    from core.routes.api_v1 import _resolve_agent_for_api

    agent = await _resolve_agent_for_api(model) if model else None
    if agent is None:
        raise OpenAIError(404, f"The model '{model}' does not exist: name an agent by its id or its exact name "
                               "(GET /models lists them).", param="model", code="model_not_found")
    return agent


@router.get("/models", summary="List the agents a key may use, as models")
async def list_models(key_record: dict = Depends(require_openai_key)):
    """Every agent in the workspace, deployed workflow agents included; never the AI builder's own."""
    from core.routes.api_v1 import _api_agents

    return {"object": "list", "data": [model_entry(a) for a in await _api_agents()]}


@router.get("/models/{model_id}", summary="One agent, as a model")
async def get_model(model_id: str, key_record: dict = Depends(require_openai_key)):
    return model_entry(await _agent_for(model_id))


# ── The turn ─────────────────────────────────────────────────────────────────

def _completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def waiting_sentence() -> str:
    """What the caller reads when the turn parks for a person (the bell asks them)."""
    return ("I need a person to approve the next step before I can go on. "
            "They have been asked, and someone will follow up.")


async def _oai_turn_events(chat_request, agent: dict, conv: Conversation, _server, *, stream: bool):
    """One turn's events, in this endpoint's own few kinds — what the turn's task drives.

    `delta` (answer text), `reset`, `answer` (the answer of record), `waiting`
    (parked for a person), `usage`, `error`. Started through `core.chat_turns`,
    so `/api/v1/chat/{session_id}/cancel` stops it like any API turn.
    """
    from core import chat_turns, turn_usage
    from core.llm_providers import LLMError
    from core.react_engine import RunFailed, run_react_loop
    from core.routes.api_v1 import _model_failure

    turn = chat_turns.current_turn.get()

    async def _stopped() -> bool:
        return bool(turn is not None and turn.stop_requested)

    latest_progress: dict | None = None
    meter = turn_usage.start()
    try:
        async for event in run_react_loop(
            chat_request, _server, should_stop=_stopped, progress_events=True,
            stream_answer=stream, history_override=conv.history, caller_system=conv.caller_system,
            save_turn=False, log_source="openai",
        ):
            kind = event.get("type")
            if kind == "_step_progress":
                latest_progress = {k: v for k, v in event.items() if k != "type"}
            elif kind == "answer_delta":
                yield {"type": "delta", "text": event["text"]}
            elif kind == "answer_reset":
                yield {"type": "reset", "reason": event.get("reason")}
            elif kind == "final" and not event.get("orch_step_id"):
                yield {"type": "answer", "content": event.get("response", ""),
                       "streamed": bool(event.get("streamed"))}
            elif kind == "human_input_required":
                from core import api_approvals
                said = waiting_sentence()
                if event.get("kind") == "tool_approval":
                    approval = await api_approvals.park(
                        session_id=chat_request.session_id, agent=agent, event=event,
                        message=chat_request.message, progress=latest_progress,
                        resume={"history": conv.history, "caller_system": conv.caller_system},
                        hours=APPROVAL_HOURS)
                    yield {"type": "waiting", "content": said, "approval_id": approval,
                           "session_id": chat_request.session_id}
                else:
                    # A workflow agent's run asking a person: it is answered on the run's page.
                    yield {"type": "waiting", "content": said, "run_id": event.get("run_id"),
                           "session_id": chat_request.session_id}
        yield {"type": "usage", **meter.totals()}
    except RunFailed as exc:
        log.error("[openai] workflow failed session=%s run=%s: %s", chat_request.session_id, exc.run_id, exc)
        yield {"type": "error", "status": 500, "code": "workflow_failed",
               "message": "The agent's workflow failed before answering."
                          + (f" Read run {exc.run_id} at /api/v1/orchestrations/runs/{exc.run_id}."
                             if exc.run_id else "")}
    except LLMError as exc:
        log.error("[openai] model error session=%s: %s", chat_request.session_id, exc)
        status = 429 if exc.code in ("rate_limited", "quota_exhausted") else 502
        yield {"type": "error", "status": status, "code": exc.code or "model_error",
               "message": _model_failure(exc.code)}
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("[openai] unhandled error session=%s", chat_request.session_id)
        yield {"type": "error", "status": 500, "code": "internal_error",
               "message": "An internal error occurred. Check server logs for details."}
    finally:
        turn_usage.stop(meter)


def _usage_body(usage: dict | None) -> dict:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    body = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if usage.get("cached_tokens"):
        body["prompt_tokens_details"] = {"cached_tokens": int(usage["cached_tokens"])}
    return body


def _start(chat_request, agent: dict, conv: Conversation, _server, *, stream: bool):
    from core import chat_turns

    with chat_turns.claim(chat_request.session_id, message=chat_request.message,
                          agent_key=agent["id"]) as turn:
        turn.start(_oai_turn_events(chat_request, agent, conv, _server, stream=stream))
    return turn


@router.post(
    "/chat/completions",
    summary="Talk to an agent, as an OpenAI chat completion",
    responses={
        200: {"description": "A chat completion, or with `stream: true` an SSE stream of "
                             "`chat.completion.chunk`s ending `data: [DONE]`.",
              "content": {"application/json": {}, "text/event-stream": {}}},
        400: {"description": "The request is not a turn this endpoint runs (tools, `n`, roles, images)."},
        401: {"description": "No API key, or an invalid or revoked one."},
        404: {"description": "No agent with that id or name."},
    },
)
async def chat_completions(request: Request, key_record: dict = Depends(require_openai_key)):
    """One turn with the agent the `model` names. See this module's docstring for what is read and what is not."""
    from core.models import ChatRequest
    from core.routes.api_v1 import refuse_unread_images

    body = await read_body(request)
    conv = read_conversation(body)
    agent = await _agent_for(body.model)

    import core.server as _server
    if not _server.agent_sessions:
        raise OpenAIError(503, "No agent sessions available: the server may still be starting.",
                          type_="server_error", code="not_ready")
    if conv.images:
        try:
            await refuse_unread_images(agent, conv.images)
        except Exception as exc:  # an HTTPException carrying the sentence (F396)
            raise OpenAIError(400, getattr(exc, "detail", None) or str(exc), param="messages",
                              code="images_unsupported") from None

    chat_request = ChatRequest(message=conv.message, session_id=f"oai_{uuid.uuid4().hex[:16]}",
                               agent_id=agent["id"], images=conv.images)
    completion = {"id": _completion_id(), "created": int(time.time()), "model": body.model}
    turn = _start(chat_request, agent, conv, _server, stream=body.stream)
    if body.stream:
        include_usage = bool((body.stream_options or {}).get("include_usage"))
        return StreamingResponse(_sse(turn, completion, chat_request.session_id, include_usage),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                          "X-Synapse-Session": chat_request.session_id})

    # Waiting is not owning: this request going away leaves the turn to finish.
    await turn.wait()
    outcome = _outcome(turn)
    if outcome.get("error"):
        err = outcome["error"]
        raise OpenAIError(err["status"], err["message"], type_="server_error" if err["status"] >= 500
                          else "invalid_request_error", code=err["code"])
    message = {"role": "assistant", "content": outcome["content"]}
    return JSONResponse({
        "id": completion["id"], "object": "chat.completion", "created": completion["created"],
        "model": completion["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": "stop", "logprobs": None}],
        "usage": _usage_body(outcome.get("usage")),
        "synapse": outcome["synapse"],
    }, headers={"X-Synapse-Session": chat_request.session_id})


def _outcome(turn) -> dict:
    """What a finished turn answered: content, usage, the synapse block, or an error."""
    out: dict = {"content": "", "usage": None,
                 "synapse": {"status": "completed", "session_id": turn.session_id}}
    for entry in turn.entries:
        ev = entry.event
        kind = ev.get("type")
        if kind == "answer":
            out["content"] = ev.get("content", "")
        elif kind == "waiting":
            out["content"] = ev.get("content", "")
            out["synapse"] = {"status": "awaiting_approval", "session_id": turn.session_id,
                              **({"approval_id": ev["approval_id"]} if ev.get("approval_id") else {}),
                              **({"run_id": ev["run_id"]} if ev.get("run_id") else {})}
        elif kind == "usage":
            out["usage"] = ev
        elif kind == "error":
            if ev.get("status"):
                out["error"] = ev
            else:   # the turn itself failed (chat_turns' own `error`)
                out["error"] = {"status": 500, "code": "internal_error",
                                "message": "An internal error occurred. Check server logs for details."}
        elif kind == "cancelled":
            out["content"] = ev.get("message") or "Stopped."
            out["synapse"] = {"status": "cancelled", "session_id": turn.session_id}
    return out


def _chunk(completion: dict, delta: dict, finish: str | None = None) -> str:
    body = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"],
            "model": completion["model"],
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}]}
    return f"data: {json.dumps(body)}\n\n"


async def _sse(turn, completion: dict, session_id: str, include_usage: bool):
    """The turn as OpenAI's chunks: role first, the answer's text, then the finish and `[DONE]`.

    Text a model wrote before calling a tool streams too; the stream cannot take
    it back, so the next text after a reset starts a new paragraph. An answer
    that did not stream (a workflow agent, a provider that does not stream, a
    parked turn) is sent whole as one chunk. Tool rounds keep the line open with
    comments, which every OpenAI client skips.
    """
    from core import chat_turns

    yield _chunk(completion, {"role": "assistant", "content": ""})
    sent = ""            # everything sent as content
    since_reset = ""     # what was sent since the last reset
    broke = False        # a reset is owed a paragraph break before the next text
    held_space = ""      # trailing whitespace not sent yet
    usage = None
    status = "completed"
    try:
        async for item in chat_turns.read(turn):
            if isinstance(item, str):
                yield item   # the heartbeat comment
                continue
            ev = item.event
            kind = ev.get("type")
            if kind == "delta":
                text = held_space + (ev.get("text") or "")
                if broke and sent and text.strip():
                    text = "\n\n" + text.lstrip()
                    broke = False
                # Trailing spaces wait for the next words: a reset may follow,
                # and a paragraph break after a space reads as a typo.
                body = text.rstrip()
                held_space = text[len(body):]
                if body:
                    sent += body
                    since_reset += body
                    yield _chunk(completion, {"content": body})
            elif kind == "reset":
                if since_reset:
                    broke = True
                since_reset = ""
                held_space = ""
            elif kind in ("answer", "waiting"):
                content = ev.get("content") or ""
                if kind == "waiting":
                    status = "awaiting_approval"
                if not (kind == "answer" and ev.get("streamed") and since_reset.strip() == content.strip()):
                    # Not what streamed: the answer of record, whole.
                    text = ("\n\n" + content) if sent and content else content
                    if text:
                        sent += text
                        yield _chunk(completion, {"content": text})
            elif kind == "usage":
                usage = ev
            elif kind == "cancelled":
                status = "cancelled"
            elif kind == "error":
                err = ev if ev.get("status") else {"status": 500, "code": "internal_error",
                                                   "message": "An internal error occurred."}
                body = {"error": {"message": err["message"], "type": "server_error" if err["status"] >= 500
                                  else "invalid_request_error", "param": None, "code": err["code"]}}
                yield f"data: {json.dumps(body)}\n\n"
                yield "data: [DONE]\n\n"
                return
        final = _chunk(completion, {}, "stop")
        if status != "completed":
            payload = json.loads(final[6:])
            payload["synapse"] = {"status": status, "session_id": session_id}
            final = f"data: {json.dumps(payload)}\n\n"
        yield final
        if include_usage:
            body = {"id": completion["id"], "object": "chat.completion.chunk", "created": completion["created"],
                    "model": completion["model"], "choices": [], "usage": _usage_body(usage)}
            yield f"data: {json.dumps(body)}\n\n"
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        # The client went away; the turn finishes on its own (F49).
        raise


# ── The bell's answer ────────────────────────────────────────────────────────

class ApprovalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str = ""
    approval_id: str | None = None


@approvals_router.post("/api/openai/approvals/{session_id}", include_in_schema=False)
async def answer_openai_approval(session_id: str, body: ApprovalAnswer):
    """Answer the tool call an OpenAI-endpoint turn parked on, and carry the turn on.

    The bell's Approve / Refuse (the item's `answer_url`). The turn resumes in
    the background; the bell item is resolved at once and a second item says
    how it ended.
    """
    from fastapi import HTTPException

    from core import api_approvals, chat_turns
    from core.routes.chat import TURN_RUNNING

    try:
        await api_approvals.answer(session_id, approve=body.approve, reason=body.reason,
                                   approval_id=body.approval_id)
    except LookupError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except chat_turns.TurnAlreadyRunning:
        raise HTTPException(status_code=409, detail=TURN_RUNNING)
    return {"status": "allowed" if body.approve else "refused", "session_id": session_id}
