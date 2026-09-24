"""Run tools: what an MCP client needs that the builder never had (D125).

The builder builds; it never starts, answers, stops or reads the result of a run.
These do, through the same engine calls the v1 routes make — not v1 over HTTP.

A run belongs to the server, not to the call that started it (F49): a tool waits
up to `wait_seconds` and then answers with where the run is, and the run carries
on. `get_run` picks it up again. Only runs of workspace orchestrations are served:
the AI builder's own runs (and the plan approvals they wait on) are not an MCP
client's to read or answer.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from mcp import types

MAX_WAIT_SECONDS = 120
DEFAULT_WAIT_SECONDS = 30
# A chat turn has no read-back tool, so it is waited for longer by default.
DEFAULT_CHAT_WAIT_SECONDS = 120
# How long a finished stream may take to reach the checkpoint a read comes from.
_SETTLE_GRACE_SECONDS = 3.0
# A single shared-state value past this is cut for the model.
_STATE_VALUE_CHARS = 4000


class ToolRefusal(Exception):
    """A call the tool refuses: returned to the client as an error result."""


def _wait_prop(default: int) -> dict:
    return {
        "type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": default,
        "description": (
            f"Seconds to wait for the run to finish or ask a question (0-{MAX_WAIT_SECONDS}, "
            f"default {default}). The run carries on after the wait either way; call "
            "get_run to pick it up again."
        ),
    }


RUN_TOOLS: tuple[types.Tool, ...] = (
    types.Tool(
        name="run_orchestration",
        description=(
            "Start a run of an orchestration (a Synapse workflow) and wait for it to finish or "
            "ask a question. Returns the run's status: `completed` with its `response`, `paused` "
            "with the question in `human_input_required` (answer it with answer_run), `failed`, "
            "or `running` if the wait ran out. Runs spend model credits."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "orchestration_id": {"type": "string", "description": "The orchestration's id (see list_orchestrations)."},
                "message": {"type": "string", "description": "What the run should work on — its goal or input text."},
                "inputs": {
                    "type": "object",
                    "description": "Optional starting values for the orchestration's declared state keys.",
                },
                "wait_seconds": _wait_prop(DEFAULT_WAIT_SECONDS),
            },
            "required": ["orchestration_id", "message"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    ),
    types.Tool(
        name="get_run",
        description=(
            "Read a run: its status, the question it is waiting on, or its result and per-step "
            "record. Pass wait_seconds to wait for a running run to finish or pause."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "wait_seconds": _wait_prop(0),
            },
            "required": ["run_id"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    ),
    types.Tool(
        name="answer_run",
        description=(
            "Answer the question a paused run is waiting on (a human step or an approval), then "
            "wait for the run to finish or ask again. `answer` is text, or an object keyed by the "
            "question's field names; for an approval pass {\"approve\": true} or {\"approve\": false}. "
            "A run is answered once: a second answer is refused."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "answer": {
                    "anyOf": [{"type": "string"}, {"type": "object"}],
                    "description": "The answer: text, or an object keyed by the question's field names.",
                },
                "wait_seconds": _wait_prop(DEFAULT_WAIT_SECONDS),
            },
            "required": ["run_id", "answer"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    ),
    types.Tool(
        name="cancel_run",
        description="Stop a run that is running or waiting. A stopped run cannot be resumed.",
        inputSchema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    ),
    types.Tool(
        name="list_runs",
        description="Recent runs, newest first, optionally for one orchestration or of one status.",
        inputSchema={
            "type": "object",
            "properties": {
                "orchestration_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["running", "paused", "completed", "failed", "cancelled"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    ),
    types.Tool(
        name="chat_with_agent",
        description=(
            "Send one message to a Synapse agent and return its reply. Pass the returned "
            "session_id to continue the same conversation. An agent that needs a tool call "
            "approved stops with status `awaiting_approval`; approvals are given in the Synapse app."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "agent": {"type": "string", "description": "Agent id or exact name; the workspace's first agent if omitted."},
                "session_id": {"type": "string", "description": "Continue this conversation."},
                "wait_seconds": {
                    "type": "integer", "minimum": 1, "maximum": 600,
                    "default": DEFAULT_CHAT_WAIT_SECONDS,
                    "description": "Seconds to wait for the reply. The turn carries on after the wait.",
                },
            },
            "required": ["message"],
        },
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    ),
)

RUN_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in RUN_TOOLS)


# ── helpers ──────────────────────────────────────────────────────────────────

def _wait(args: dict, default: int) -> float:
    try:
        seconds = int(args.get("wait_seconds", default))
    except (TypeError, ValueError):
        seconds = default
    return float(max(0, min(seconds, MAX_WAIT_SECONDS)))


async def _visible_orchestration_or_refuse(orch_id: str) -> dict:
    from core.builder_tools import _visible_orchestration
    orch = await _visible_orchestration(orch_id)
    if orch is None:
        raise ToolRefusal(f"Orchestration '{orch_id}' not found")
    return orch


async def _served_run(run_id: str) -> None:
    """Refuse a run that does not exist or is not a workspace orchestration's."""
    from core.builder_tools import _visible_orchestration
    from core.orchestration.run_view import run_orchestration_id

    orch_id = await run_orchestration_id(run_id)
    if not orch_id or await _visible_orchestration(orch_id) is None:
        raise ToolRefusal(f"Run '{run_id}' not found")


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _STATE_VALUE_CHARS:
        return value[:_STATE_VALUE_CHARS] + f"… [{len(value) - _STATE_VALUE_CHARS} more characters]"
    return value


def _for_model(view: dict) -> dict:
    """A run view a model can read: its steps as one line each, long values cut."""
    out = {k: v for k, v in view.items() if k not in ("step_history", "shared_state")}
    if "step_history" in view:
        out["steps"] = [
            {k: h[k] for k in ("step_id", "step_name", "step_type", "status", "error") if h.get(k)}
            for h in view["step_history"]
        ]
    if "shared_state" in view:
        out["shared_state"] = {k: _clip(v) for k, v in view["shared_state"].items()}
    return out


async def _settle(run_id: str, queue: asyncio.Queue | None, wait: float) -> dict:
    """Follow a run for up to `wait` seconds, then read it.

    With a queue (the run is advancing in this process) the run's own events say
    when it has paused or ended. Without one (it went to a worker fleet) the
    checkpoint is polled. Either way the answer comes from the checkpoint, so it
    is the same thing `get_run` would say.
    """
    from core.orchestration.run_view import read_run
    from core.orchestration.runner import SENTINEL

    deadline = time.monotonic() + wait
    last: str | None = None
    if queue is not None:
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if event is SENTINEL:
                last = "ended"
                break
            if event.get("type") == "human_input_required":
                last = "paused"
                break
        # The checkpoint is written as the run pauses or ends; give it a moment
        # to agree with the event that was just seen.
        want = {"paused": {"paused"}, "ended": {"completed", "failed", "cancelled", "paused"}}.get(last)
        grace = time.monotonic() + (_SETTLE_GRACE_SECONDS if want else 0)
        while True:
            view = await read_run(run_id)
            if not want or (view and view["status"] in want) or time.monotonic() >= grace:
                break
            await asyncio.sleep(0.05)
    else:
        while True:
            view = await read_run(run_id)
            if view and view["status"] not in ("running", "resuming"):
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
    if view is None:
        view = {"status": "running", "run_id": run_id}
    if view["status"] == "running":
        view["note"] = "The run is still going. Call get_run with wait_seconds to follow it."
    return _for_model(view)


# ── the tools ────────────────────────────────────────────────────────────────

async def start_run(orch_id: str, message: str, inputs: dict | None, wait: float) -> dict:
    import core.server as _server
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.runner import spawn_engine_run

    orch = await _visible_orchestration_or_refuse(orch_id)
    if inputs is not None and not isinstance(inputs, dict):
        raise ToolRefusal("`inputs` must be an object of state keys")
    # A leading underscore is the engine's own bookkeeping (`_model_override`, …),
    # as v1 refuses it too (D126).
    engine_keys = sorted(k for k in inputs or {} if str(k).startswith("_"))
    if engine_keys:
        raise ToolRefusal(f"these keys are the engine's own and cannot be set: {', '.join(engine_keys)}")
    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
    engine = OrchestrationEngine(Orchestration.model_validate(orch), _server)
    _task, queue = spawn_engine_run(
        engine.run(message, run_id, initial_state=inputs or None), run_id,
    )
    return await _settle(run_id, queue, wait)


async def _run_orchestration(args: dict) -> dict:
    return await start_run(
        str(args["orchestration_id"]), str(args["message"]),
        args.get("inputs"), _wait(args, DEFAULT_WAIT_SECONDS),
    )


async def _get_run(args: dict) -> dict:
    run_id = str(args["run_id"])
    await _served_run(run_id)
    return await _settle(run_id, None, _wait(args, 0))


async def _answer_run(args: dict) -> dict:
    import core.server as _server
    from core.orchestration.answers import RunAlreadyActive, RunNotWaiting, answer_paused_run

    run_id = str(args["run_id"])
    await _served_run(run_id)
    answer = args["answer"]
    # v1's shape: text is `{"response": text}`, which a step with no fields
    # keeps as the text itself (F106).
    response = {"response": answer} if isinstance(answer, str) else answer
    try:
        answered = await answer_paused_run(_server.app.state, run_id, response)
    except RunAlreadyActive:
        raise ToolRefusal("The run is already being answered or resumed.")
    except RunNotWaiting as exc:
        if exc.status is None:
            raise ToolRefusal(f"Run '{run_id}' not found")
        raise ToolRefusal(f"The run is {exc.status} and is not waiting for an answer.")
    return await _settle(run_id, getattr(answered, "queue", None), _wait(args, DEFAULT_WAIT_SECONDS))


async def _cancel_run(args: dict) -> dict:
    import core.server as _server
    from core.orchestration.cancel import cancel_run

    run_id = str(args["run_id"])
    await _served_run(run_id)
    result = await cancel_run(run_id, redis=getattr(_server.app.state, "redis", None))
    if not result.found:
        raise ToolRefusal(f"Run '{run_id}' not found")
    return {"run_id": run_id, "status": result.status, "stopped": result.stopped}


async def _list_runs(args: dict) -> list[dict]:
    from core.orchestration.run_view import list_runs

    try:
        limit = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    return await list_runs(limit=limit, status=args.get("status") or None,
                           orchestration_id=args.get("orchestration_id") or None)


async def _chat_with_agent(args: dict) -> dict:
    """One chat turn, as `/api/v1/chat` runs it."""
    import core.server as _server
    from fastapi import HTTPException

    from core.routes.api_v1 import V1ChatRequest, _agent_or_error, _v1_start

    try:
        agent = await _agent_or_error(args.get("agent"))
    except HTTPException as exc:
        raise ToolRefusal(str(exc.detail))
    if not _server.agent_sessions:
        raise ToolRefusal("No agent sessions are available; the server may still be starting.")
    body = V1ChatRequest(message=str(args["message"]), agent=agent["id"], session_id=args.get("session_id"))
    try:
        chat_request, turn = _v1_start(body, agent, _server)
    except HTTPException as exc:
        raise ToolRefusal(str(exc.detail))

    identity = {"agent_id": agent["id"], "agent_name": agent["name"], "session_id": chat_request.session_id}
    try:
        wait = max(1, min(int(args.get("wait_seconds") or DEFAULT_CHAT_WAIT_SECONDS), 600))
    except (TypeError, ValueError):
        wait = DEFAULT_CHAT_WAIT_SECONDS
    try:
        # Waiting is not owning: a wait that runs out leaves the turn running.
        await asyncio.wait_for(asyncio.shield(turn.wait()), timeout=wait)
    except asyncio.TimeoutError:
        return {"status": "running", **identity,
                "note": "The agent is still working. Its reply will be in this conversation in the Synapse app."}

    for entry in turn.entries:
        event = entry.event
        kind = event.get("type")
        if kind == "error":
            raise ToolRefusal(event.get("message") or "The agent encountered an error.")
        if kind == "cancelled":
            return {"response": event.get("message") or "Stopped.", "status": "cancelled", **identity}
        if kind in ("awaiting_approval", "paused"):
            pause = {k: v for k, v in event.items() if k not in ("type", "session_id")}
            prompt = pause.pop("prompt", "")
            return {"response": prompt, "status": kind, **pause, **identity,
                    "note": "Approve or answer this in the Synapse app."}
        if kind == "response":
            return {"response": event.get("content", ""), "status": "completed", **identity}
    return {"response": "I completed the requested actions.", "status": "completed", **identity}


_HANDLERS = {
    "run_orchestration": _run_orchestration,
    "get_run": _get_run,
    "answer_run": _answer_run,
    "cancel_run": _cancel_run,
    "list_runs": _list_runs,
    "chat_with_agent": _chat_with_agent,
}


async def call_run_tool(name: str, args: dict) -> Any:
    return await _HANDLERS[name](args)
