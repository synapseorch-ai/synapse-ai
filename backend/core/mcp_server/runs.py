"""Run tools: what an MCP client needs that the builder never had (D125).

The builder builds; it never starts, answers, stops or reads the result of a run.
These do, through the same engine calls the v1 routes make — not v1 over HTTP.

A run belongs to the server, not to the call that started it (F49): a tool waits
up to `wait_seconds` and then answers with where the run is, and the run carries
on. `get_run` picks it up again. Only runs of workspace orchestrations are served:
the AI builder's own runs (and the plan approvals they wait on) are not an MCP
client's to read or answer.

Where a run executes is a `RunBackend`'s business. The open-source backend runs
the engine in this process (`InProcessRuns`); a deployment whose runs execute
elsewhere — cloud's worker fleet — installs its own with `set_run_backend`, and
every tool here, the waiting and the refusals included, stays the same.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

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
    if not await get_run_backend().served(run_id):
        raise ToolRefusal(f"Run '{run_id}' not found")


#: A run that has not paused or ended. `queued` is a fleet's: accepted, not yet picked up.
_GOING = ("running", "resuming", "queued")

#: Which question a paused view is asking (`pauses.pause_ordinal`). A backend
#: adds it to a paused view so the question just answered is never reported as
#: the run's next one; it is the tools' bookkeeping and never reaches the client.
PAUSE_KEY = "_pause"


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _STATE_VALUE_CHARS:
        return value[:_STATE_VALUE_CHARS] + f"… [{len(value) - _STATE_VALUE_CHARS} more characters]"
    return value


def _for_model(view: dict) -> dict:
    """A run view a model can read: its steps as one line each, long values cut."""
    out = {k: v for k, v in view.items()
           if k not in ("step_history", "shared_state") and not k.startswith("_")}
    if "step_history" in view:
        out["steps"] = [
            {k: h[k] for k in ("step_id", "step_name", "step_type", "status", "error") if h.get(k)}
            for h in view["step_history"]
        ]
    if "shared_state" in view:
        out["shared_state"] = {k: _clip(v) for k, v in view["shared_state"].items()}
    return out


async def _settle(run_id: str, queue: asyncio.Queue | None, wait: float,
                  answered: int | None = None) -> dict:
    """Follow a run for up to `wait` seconds, then read it.

    With a queue (the run is advancing in this process) the run's own events say
    when it has paused or ended. Without one (it went to a worker fleet) the
    checkpoint is polled. Either way the answer comes from the checkpoint, so it
    is the same thing `get_run` would say.

    `answered` is the question an answer was just given to. Until a worker picks
    the answer up the run still reads as paused on it, and a client shown that
    question again would take its answer as lost; so that pause counts as still
    going, and reads as `resuming`.
    """
    from core.orchestration.runner import SENTINEL

    backend_read = get_run_backend().read

    async def read_run(rid: str) -> dict | None:
        view = await backend_read(rid)
        if (answered is not None and view and view.get("status") == "paused"
                and view.get(PAUSE_KEY) == answered):
            return {"status": "resuming", "run_id": rid,
                    "note": "The answer was delivered; the run has not picked it up yet."}
        return view

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
            if view and view["status"] not in _GOING:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
    if view is None:
        view = {"status": "running", "run_id": run_id}
    if view["status"] in _GOING:
        view["note"] = " ".join(filter(None, (
            view.get("note"), "The run is still going. Call get_run with wait_seconds to follow it.")))
    return _for_model(view)


# ── where runs execute ───────────────────────────────────────────────────────

class RunBackend(Protocol):
    """Where a run executes and is read from. Every method raises `ToolRefusal`
    for what the caller should hear as an error.

    The tools refuse what no backend may serve before a backend is called: an
    orchestration that is not the workspace's, an input key that is the engine's.
    """

    async def start(self, orch: dict, message: str, inputs: dict | None) -> tuple[str, asyncio.Queue | None]:
        """Start a run; its id, and its event queue when it advances in this process."""

    async def served(self, run_id: str) -> bool:
        """Whether the run exists and is a workspace orchestration's."""

    async def read(self, run_id: str) -> dict | None:
        """`run_view.read_run`'s shape, or None when there is no such run. A
        paused view carries `PAUSE_KEY`, the question's ordinal."""

    async def answer(self, run_id: str, response: Any) -> asyncio.Queue | None:
        """Answer a paused run; its event queue when it resumes in this process."""

    async def cancel(self, run_id: str) -> dict:
        """Stop a run: `{"run_id", "status", "stopped"}`."""

    async def list(self, *, limit: int, status: str | None, orchestration_id: str | None) -> list[dict]:
        """Recent runs, newest first, `status` applied before `limit`."""

    async def chat(self, *, message: str, agent: str | None, session_id: str | None, wait: float) -> dict:
        """One chat turn and its reply, or where it is when the wait runs out."""


class InProcessRuns:
    """The open-source backend: the engine runs in this process, as v1's routes run it."""

    async def start(self, orch: dict, message: str, inputs: dict | None) -> tuple[str, asyncio.Queue | None]:
        import core.server as _server
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.runner import spawn_engine_run

        run_id = f"run_{orch['id']}_{int(time.time() * 1000)}"
        engine = OrchestrationEngine(Orchestration.model_validate(orch), _server)
        _task, queue = spawn_engine_run(engine.run(message, run_id, initial_state=inputs or None), run_id)
        return run_id, queue

    async def served(self, run_id: str) -> bool:
        from core.builder_tools import _visible_orchestration
        from core.orchestration.run_view import run_orchestration_id

        orch_id = await run_orchestration_id(run_id)
        return bool(orch_id) and await _visible_orchestration(orch_id) is not None

    async def read(self, run_id: str) -> dict | None:
        from core.orchestration.pauses import pause_ordinal
        from core.orchestration.run_view import read_run
        from core.orchestration.state import SharedState

        view = await read_run(run_id)
        if view and view["status"] == "paused":
            try:
                view[PAUSE_KEY] = pause_ordinal((await SharedState.restore(run_id)).run.step_history)
            except FileNotFoundError:
                pass
        return view

    async def answer(self, run_id: str, response: Any) -> asyncio.Queue | None:
        import core.server as _server
        from core.orchestration.answers import RunAlreadyActive, RunNotWaiting, answer_paused_run

        try:
            answered = await answer_paused_run(_server.app.state, run_id, response)
        except RunAlreadyActive:
            raise ToolRefusal("The run is already being answered or resumed.")
        except RunNotWaiting as exc:
            if exc.status is None:
                raise ToolRefusal(f"Run '{run_id}' not found")
            raise ToolRefusal(f"The run is {exc.status} and is not waiting for an answer.")
        return getattr(answered, "queue", None)

    async def cancel(self, run_id: str) -> dict:
        import core.server as _server
        from core.orchestration.cancel import cancel_run

        result = await cancel_run(run_id, redis=getattr(_server.app.state, "redis", None))
        if not result.found:
            raise ToolRefusal(f"Run '{run_id}' not found")
        return {"run_id": run_id, "status": result.status, "stopped": result.stopped}

    async def list(self, *, limit: int, status: str | None, orchestration_id: str | None) -> list[dict]:
        from core.orchestration.run_view import list_runs
        return await list_runs(limit=limit, status=status, orchestration_id=orchestration_id)

    async def chat(self, *, message: str, agent: str | None, session_id: str | None, wait: float) -> dict:
        """One chat turn, as `/api/v1/chat` runs it."""
        import core.server as _server
        from fastapi import HTTPException

        from core.routes.api_v1 import V1ChatRequest, _agent_or_error, _v1_start

        try:
            found = await _agent_or_error(agent)
        except HTTPException as exc:
            raise ToolRefusal(str(exc.detail))
        if not _server.agent_sessions:
            raise ToolRefusal("No agent sessions are available; the server may still be starting.")
        body = V1ChatRequest(message=message, agent=found["id"], session_id=session_id)
        try:
            chat_request, turn = _v1_start(body, found, _server)
        except HTTPException as exc:
            raise ToolRefusal(str(exc.detail))

        identity = {"agent_id": found["id"], "agent_name": found["name"], "session_id": chat_request.session_id}
        try:
            # Waiting is not owning: a wait that runs out leaves the turn running.
            await asyncio.wait_for(asyncio.shield(turn.wait()), timeout=wait)
        except asyncio.TimeoutError:
            return {"status": "running", **identity, "note": CHAT_STILL_GOING}
        return chat_result([entry.event for entry in turn.entries], identity)


#: What a chat wait that ran out says; the turn carries on.
CHAT_STILL_GOING = "The agent is still working. Its reply will be in this conversation in the Synapse app."


def chat_result(events: list[dict], identity: dict) -> dict:
    """A finished chat turn's events as the tool's answer. Every backend's turn ends here."""
    for event in events:
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


_backend: RunBackend = InProcessRuns()


def set_run_backend(backend: RunBackend | None) -> None:
    """Install where runs execute; None restores the in-process default."""
    global _backend
    _backend = backend if backend is not None else InProcessRuns()


def get_run_backend() -> RunBackend:
    return _backend


# ── the tools ────────────────────────────────────────────────────────────────

async def start_run(orch_id: str, message: str, inputs: dict | None, wait: float) -> dict:
    orch = await _visible_orchestration_or_refuse(orch_id)
    if inputs is not None and not isinstance(inputs, dict):
        raise ToolRefusal("`inputs` must be an object of state keys")
    # A leading underscore is the engine's own bookkeeping (`_model_override`, …),
    # as v1 refuses it too (D126).
    engine_keys = sorted(k for k in inputs or {} if str(k).startswith("_"))
    if engine_keys:
        raise ToolRefusal(f"these keys are the engine's own and cannot be set: {', '.join(engine_keys)}")
    run_id, queue = await get_run_backend().start(orch, message, inputs)
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
    run_id = str(args["run_id"])
    await _served_run(run_id)
    before = await get_run_backend().read(run_id)
    answered = before.get(PAUSE_KEY) if before and before.get("status") == "paused" else None
    answer = args["answer"]
    # v1's shape: text is `{"response": text}`, which a step with no fields
    # keeps as the text itself (F106).
    response = {"response": answer} if isinstance(answer, str) else answer
    queue = await get_run_backend().answer(run_id, response)
    return await _settle(run_id, queue, _wait(args, DEFAULT_WAIT_SECONDS), answered)


async def _cancel_run(args: dict) -> dict:
    run_id = str(args["run_id"])
    await _served_run(run_id)
    return await get_run_backend().cancel(run_id)


async def _list_runs(args: dict) -> list[dict]:
    try:
        limit = max(1, min(int(args.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    return await get_run_backend().list(limit=limit, status=args.get("status") or None,
                                        orchestration_id=args.get("orchestration_id") or None)


async def _chat_with_agent(args: dict) -> dict:
    try:
        wait = max(1, min(int(args.get("wait_seconds") or DEFAULT_CHAT_WAIT_SECONDS), 600))
    except (TypeError, ValueError):
        wait = DEFAULT_CHAT_WAIT_SECONDS
    return await get_run_backend().chat(
        message=str(args["message"]), agent=args.get("agent"),
        session_id=args.get("session_id"), wait=float(wait),
    )


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
