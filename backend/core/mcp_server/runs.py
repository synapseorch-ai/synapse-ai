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
import json
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


from core.product_tools.errors import ToolRefusal  # noqa: E402  one refusal for every product tool (D292)


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
                "orchestration_id": {"type": "string",
                                     "description": "The orchestration's id, or its exact name (see "
                                                    "list_orchestrations)."},
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
                "question_id": {
                    "type": "string",
                    "description": "Which question, when the run asks several at once (a team step's members, "
                                   "listed in the run's `questions`). Leave out when it asks one.",
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
            "approved stops with status `awaiting_approval`: answer it with answer_tool_approval."
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
    types.Tool(
        name="answer_tool_approval",
        description=(
            "Allow or refuse a tool call an agent stopped on: in a conversation (chat_with_agent's "
            "`awaiting_approval`, an app's or a phone call's), or in a scheduled run. Name where it waits "
            "with session_id or schedule_id; list_waiting shows each with its ids. Allowed, the turn "
            "carries on and this waits for its reply. A question is answered once."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approve": {"type": "boolean", "description": "true to let the call run, false to refuse it."},
                "reason": {"type": "string", "description": "Why, told to the agent (optional)."},
                "session_id": {"type": "string", "description": "The conversation that waits."},
                "schedule_id": {"type": "string", "description": "The schedule whose run waits."},
                "approval_id": {"type": "string",
                                "description": "The call this answers, so an older question is never answered "
                                               "by mistake."},
                "question_id": {"type": "string",
                                "description": "Which team member's question, when several wait."},
                "agent": {"type": "string", "description": "The agent whose question it is, when several wait."},
                "wait_seconds": {
                    "type": "integer", "minimum": 0, "maximum": 600, "default": DEFAULT_CHAT_WAIT_SECONDS,
                    "description": "Seconds to wait for the resumed turn's reply. It carries on after the wait.",
                },
            },
            "required": ["approve"],
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
    """The workspace's workflow with this id, or else with exactly this name (any case): a client names a
    workflow as a person does, and only some are tools of their own (F466)."""
    from core.builder_tools import _is_visible, _visible_orchestration
    orch = await _visible_orchestration(orch_id)
    if orch is not None:
        return orch
    from core.routes.orchestrations import load_orchestrations

    wanted = orch_id.strip().casefold()
    named = [o for o in await load_orchestrations()
             if _is_visible(o) and str(o.get("name") or "").strip().casefold() == wanted]
    if len(named) == 1:
        return named[0]
    if named:
        ids = ", ".join(f"{o['id']}" for o in named)
        raise ToolRefusal(f"{len(named)} workflows are named '{orch_id}': {ids}. Run one by its id.")
    raise ToolRefusal(f"Orchestration '{orch_id}' not found")


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


#: A step's output, as a run's step line shows it: the start of what it wrote.
_STEP_OUTPUT_CHARS = 500


def _step_line(h: dict, state: dict) -> dict:
    line = {k: h[k] for k in ("step_id", "step_name", "step_type", "status", "error", "duration_seconds",
                              "tasks") if h.get(k)}
    key = h.get("output_key")
    if key and h.get("status") == "completed" and state.get(key) not in (None, ""):
        value = state[key]
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        line["output"] = (text[:_STEP_OUTPUT_CHARS] + f"… [{len(text) - _STEP_OUTPUT_CHARS} more characters]"
                          if len(text) > _STEP_OUTPUT_CHARS else text)
    return line


def _for_model(view: dict) -> dict:
    """A run view a model can read: its steps as one line each, with the start of what each wrote; long values
    cut. "Did it finish, and what did each step do" is one read (ASSIST1, D291)."""
    out = {k: v for k, v in view.items()
           if k not in ("step_history", "shared_state") and not k.startswith("_")}
    if "step_history" in view:
        state = view.get("shared_state") or {}
        out["steps"] = [_step_line(h, state) for h in view["step_history"] if isinstance(h, dict)]
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

    async def answer(self, run_id: str, response: Any, *, question_id: str | None = None) -> asyncio.Queue | None:
        """Answer a paused run; its event queue when it resumes in this process. `question_id` names one of
        several questions (a team step's), and is passed only when given."""

    async def cancel(self, run_id: str) -> dict:
        """Stop a run: `{"run_id", "status", "stopped"}`."""

    async def list(self, *, limit: int, status: str | None, orchestration_id: str | None) -> list[dict]:
        """Recent runs, newest first, `status` applied before `limit`."""

    async def chat(self, *, message: str, agent: str | None, session_id: str | None, wait: float) -> dict:
        """One chat turn and its reply, or where it is when the wait runs out."""

    # Optional (MCP2, D292): a backend without it answers that tool calls are approved in the app.
    async def answer_tool_call(self, *, approve: bool, reason: str, session_id: str | None,
                               schedule_id: str | None, approval_id: str | None, question_id: str | None,
                               agent: str | None, wait: float) -> dict:
        """Allow or refuse the tool call a conversation or a scheduled run waits on; the resumed turn's reply,
        or where it is when the wait runs out."""

    # Optional (F1260): a backend without it answers that a run is resumed from its page.
    async def resume(self, run_id: str) -> dict:
        """Resume a failed or stopped run from the step it stopped at, as the run page's Replay does:
        `{"run_id", "status": "resuming"}`."""


class InProcessRuns:
    """The open-source backend: the engine runs in this process, as v1's routes run it."""

    async def start(self, orch: dict, message: str, inputs: dict | None) -> tuple[str, asyncio.Queue | None]:
        import core.server as _server
        from core.orchestration.engine import OrchestrationEngine
        from core.orchestration.read_shapes import load_to_run
        from core.orchestration.runner import spawn_engine_run
        from core.run_ids import new_run_id

        run_id = new_run_id(orch["id"])
        engine = OrchestrationEngine(load_to_run(orch), _server)
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

    async def answer(self, run_id: str, response: Any, *, question_id: str | None = None) -> asyncio.Queue | None:
        import core.server as _server
        from core.orchestration.answers import (
            BadAnswer, QuestionNotAsked, RunAlreadyActive, RunNotWaiting, answer_paused_run,
        )

        try:
            answered = await answer_paused_run(_server.app.state, run_id, response, question_id=question_id)
        except QuestionNotAsked:
            raise ToolRefusal(f"The run is not asking question '{question_id}' now; get_run lists what it asks.")
        except BadAnswer as exc:
            # An email, date, phone or dropdown answer that is not one (F455).
            raise ToolRefusal(str(exc))
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

    async def resume(self, run_id: str) -> dict:
        """The run page's Replay (`orchestration.replay`, F1260): its refusals, its worker or this process."""
        import core.server as _server
        from core.orchestration import replay

        try:
            await replay.resume(_server.app.state, run_id, _server)
        except replay.NotResumable as why:
            raise ToolRefusal(str(why)) from None
        return {"run_id": run_id, "status": "resuming"}

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


    async def answer_tool_call(self, *, approve: bool, reason: str, session_id: str | None,
                               schedule_id: str | None, approval_id: str | None, question_id: str | None,
                               agent: str | None, wait: float) -> dict:
        """The product's own answers, through the functions their routes call: the bell's for a schedule, an API
        door's (OpenAI-compatible, phone) for its turns, v1's approve for a conversation."""
        from fastapi import HTTPException

        from core import api_approvals, chat_turns
        from core.routes.chat import TURN_RUNNING

        decided = "allowed" if approve else "refused"
        try:
            if schedule_id:
                from core.scheduler import _session_of
                from core.store import schedules as schedule_store

                found = await schedule_store.get(schedule_id)
                if not found:
                    raise ToolRefusal(f"Schedule '{schedule_id}' not found")
                if question_id:
                    session_id, agent = _session_of(schedule_id), found.get("target_id") or None
                else:
                    import core.server as _server
                    manager = getattr(_server, "schedule_manager", None)
                    if manager is None:
                        raise ToolRefusal("The scheduler is not running here.")
                    try:
                        turn = await manager.answer_approval(schedule_id, approve=approve, reason=reason,
                                                             approval_id=approval_id)
                    except LookupError as exc:
                        raise ToolRefusal(str(exc))
                    return await _turn_reply(turn, {"schedule_id": schedule_id, "status_given": decided}, wait)
            if not session_id:
                raise ToolRefusal("Name where the call waits: session_id or schedule_id (list_waiting has both).")
            if api_approvals.door_for(session_id) is not None:
                try:
                    turn = await api_approvals.answer(session_id, approve=approve, reason=reason,
                                                      approval_id=approval_id, question_id=question_id)
                except LookupError as exc:
                    raise ToolRefusal(str(exc))
                if turn is None:
                    return {"status": decided, "session_id": session_id, "live": True}
                return await _turn_reply(turn, {"session_id": session_id, "status_given": decided}, wait)
            # A conversation an API caller (chat_with_agent) started resumes as v1 resumes it: an API turn,
            # not the app's chat (which reads a person's @[path] mentions from the vault).
            from core.routes.api_v1 import answer_v1_question
            turn = await answer_v1_question(session_id, approve=approve, reason=reason, approval_id=approval_id,
                                            question_id=question_id, agent=agent)
            if turn is None:
                return {"status": decided, "session_id": session_id, "live": True}
            return await _turn_reply(turn, {"session_id": session_id}, wait)
        except HTTPException as exc:
            raise ToolRefusal(str(exc.detail))
        except chat_turns.TurnAlreadyRunning:
            raise ToolRefusal(TURN_RUNNING)


async def _turn_reply(turn: Any, identity: dict, wait: float) -> dict:
    """A resumed turn's reply, as `chat_with_agent` gives one; where it is when the wait runs out."""
    try:
        await asyncio.wait_for(asyncio.shield(turn.wait()), timeout=wait)
    except asyncio.TimeoutError:
        return {"status": "running", **identity, "note": CHAT_STILL_GOING}
    return chat_result([entry.event for entry in turn.entries], identity)


#: What a chat wait that ran out says; the turn carries on.
CHAT_STILL_GOING = "The agent is still working. Its reply will be in this conversation in the Synapse app."


def chat_result(events: list[dict], identity: dict) -> dict:
    """A finished chat turn's events as the tool's answer. Every backend's turn ends here.

    The answer's `[K#]` labels come resolved, as v1's sync answer has them (D176)."""
    cited = next((e for e in reversed(events) if e.get("type") == "knowledge_citations"), {})
    identity = {**identity, "citations": cited.get("citations", []),
                "unresolved_citations": cited.get("unresolved_citations", [])}
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
                    "note": "Answer it with answer_tool_approval (this session_id and approval_id), "
                            "or in the Synapse app."}
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
    from core.orchestration.read_shapes import Unreadable, load_to_run
    try:
        load_to_run(orch)
    except Unreadable as e:  # said before any backend starts it, never a server error (F1235)
        raise ToolRefusal(str(e))
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
    from core.orchestration.answers import EmptyAnswer, refuse_empty
    try:
        refuse_empty(response)
    except EmptyAnswer as exc:
        raise ToolRefusal(str(exc))
    question_id = str(args["question_id"]) if args.get("question_id") else None
    backend = get_run_backend()
    # Passed only when given: an embedder's backend from before MCP2 takes no question.
    queue = await (backend.answer(run_id, response, question_id=question_id) if question_id
                   else backend.answer(run_id, response))
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


async def _answer_tool_approval(args: dict) -> dict:
    if not isinstance(args.get("approve"), bool):
        raise ToolRefusal("Say approve: true to let the call run, or false to refuse it.")
    answer = getattr(get_run_backend(), "answer_tool_call", None)
    if answer is None:
        raise ToolRefusal("Tool calls are approved in the Synapse app on this server.")
    try:
        wait = max(0, min(int(args.get("wait_seconds", DEFAULT_CHAT_WAIT_SECONDS)), 600))
    except (TypeError, ValueError):
        wait = DEFAULT_CHAT_WAIT_SECONDS

    def text(key: str) -> str | None:
        return str(args[key]) if args.get(key) else None

    return await answer(approve=args["approve"], reason=str(args.get("reason") or ""),
                        session_id=text("session_id"), schedule_id=text("schedule_id"),
                        approval_id=text("approval_id"), question_id=text("question_id"), agent=text("agent"),
                        wait=float(wait))


_HANDLERS = {
    "run_orchestration": _run_orchestration,
    "get_run": _get_run,
    "answer_run": _answer_run,
    "cancel_run": _cancel_run,
    "list_runs": _list_runs,
    "chat_with_agent": _chat_with_agent,
    "answer_tool_approval": _answer_tool_approval,
}


async def call_run_tool(name: str, args: dict) -> Any:
    return await _HANDLERS[name](args)
