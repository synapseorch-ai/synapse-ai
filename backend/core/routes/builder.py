"""The AI builder's endpoints.

The builder is an ordinary orchestration (`orch_native_builder`) with ordinary
sub-agents, so these routes do very little: start a run, resume one, and stream
what the engine says. The stream is the engine's own events, unchanged.

That last part used to be false. This module translated engine events into a
private vocabulary and, because a tool result reaches it truncated to 500
characters, recovered "an orchestration was saved" by running a regex over the
truncation — with three fallbacks behind it, all three dead code, the last of
which announced *the most recently updated orchestration in the workspace*
whether or not this run had touched it. The dispatcher now emits
`resource_saved` with the whole resource, so there is nothing left to infer,
and one reducer in the frontend maps engine events to panel messages for both
this product and the commercial one.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.images import checked_images
from core.models_orchestration import Orchestration
from core.native_builder import (
    BUILDER_RUN_PREFIX,
    NATIVE_BUILDER_ORCH_ID,
    bundled_definitions,
)
from core.orchestration.engine import OrchestrationEngine
from core.orchestration.runner import (
    SENTINEL,
    _active_tasks,
    spawn_engine_run,
    stream_engine_events,
)
from core.log import get_logger
from core.react_engine import iter_with_heartbeat

_log = get_logger(__name__)

router = APIRouter()

#: Engine events that are internal bookkeeping — mid-step progress markers and
#: the tool-step's own LLM log. Underscore-prefixed by convention.
def _is_internal(event_type: str) -> bool:
    return event_type.startswith("_")


class BuilderChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    selected_agent_ids: list[str] = []
    can_create_agents: bool = False
    model: str | None = None
    current_orchestration_id: str | None = None
    #: Identifies one conversation. It reaches the engine as part of the
    #: session id, which scopes the tool cache and the agents' own history —
    #: this was the literal string "builder" for every conversation, and in the
    #: commercial edition that meant every conversation of every user.
    client_session_id: str = ""
    #: Base64 data-URI images, same transport and same limits as chat. Reaches
    #: the run out of band rather than in `initial_state` — see
    #: `run_builder_stream` for why.
    images: list[str] = []

    _check_images = checked_images()


class BuilderResumeRequest(BaseModel):
    run_id: str
    response: dict = {}
    #: Attachments sent *with the answer*. The images a run started with are
    #: not persisted anywhere — see `OrchestrationEngine.resume` — so a step
    #: re-entered after a pause sees only what was attached to the reply.
    images: list[str] = []

    _check_images = checked_images()


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    return "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history)


async def _previous_matches(session_id: str) -> str:
    """What the previous turn of this conversation asked about, if it asked.

    When a turn could not tell which orchestration was meant, its state lists
    the candidates; the next message in the same conversation is resolved
    against that list, so "the second one" is an answer (F16). Only the newest
    turn counts: a question two turns back has been answered or dropped since.
    """
    from sqlalchemy import select

    from core.native_builder.target import AMBIGUOUS
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        state = (
            await s.execute(
                select(OrchestrationRunDB.shared_state)
                .where(
                    OrchestrationRunDB.tenant_id == get_tenant(),
                    OrchestrationRunDB.session_id == session_id,
                )
                .order_by(OrchestrationRunDB.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or {}
    if state.get("current_orchestration_source") != AMBIGUOUS:
        return ""
    return str(state.get("current_orchestration_matches") or "")


async def _target_state(
    message: str, viewing_id: str | None, previous_matches: str = ""
) -> dict:
    """Which orchestration this message is about, as the run's state keys.

    This passed an id through if it existed, which let the builder's own
    orchestration through, and never read the message — "update … on the
    extended research orchestration" reached the router with no target and was
    built as something new. Both signals go to one resolver now, over the
    orchestrations the builder may touch; the commercial edition calls the same
    resolver with its own tenant's rows.
    """
    from core.builder_tools import _is_visible
    from core.native_builder.target import describe_page, resolve_target
    from core.store.resources import load_orchestrations

    visible = [o for o in await load_orchestrations() if _is_visible(o)]
    target = resolve_target(message, viewing_id, visible, previous_matches)
    viewing = next((o for o in visible if viewing_id and o["id"] == viewing_id), None)
    page = describe_page("orchestration", viewing.get("name", ""), viewing["id"]) if viewing else ""
    return {**target.state(), "current_page": page}


async def _workspace_profile() -> str:
    """What this workspace is for, in one line, for the analyst's first read.

    Open source has nowhere to record it: the `personal_details` collection is
    contact information — a name, a phone number, a postal address — not a
    description of the work, and piping that into a model prompt costs
    something and buys nothing. The commercial edition fills this key from the
    onboarding questionnaire, which does ask.
    """
    return "(not recorded — ask the user if the answer would change what you build)"


async def _selected_model(request_model: str | None) -> str | None:
    """The model this run uses: the caller's pick, else the workspace default."""
    if request_model:
        return request_model
    from core.config import load_settings
    return load_settings().get("builder_model") or None


#: Marks a conversation held by the builder panel. The chat page's builder-typed
#: agent starts runs of the same orchestration under its own chat session id, so
#: without this the panel could reopen a chat conversation as its own (F214).
PANEL = "panel:"


def _session_id(client_session_id: str, *, panel: bool = False) -> str:
    from core.tenancy import get_tenant
    key = client_session_id or "default"
    return f"builder:{get_tenant()}:{PANEL if panel else ''}{key}"


async def reject_unseeable_images(request: BuilderChatRequest) -> None:
    """Refuse attachments a model will throw away, rather than throwing them away.

    Four providers discard images — two with a log line nobody reads, two in
    silence — so the run answers as though nothing were attached, which reads as
    the agent ignoring you. The composer already disables its attach button from
    `vision_capable`; this is for every caller that is not the composer, and for
    the chat page's builder-typed agent, which reaches the same run by a
    different door.
    """
    if not request.images:
        return
    from core.config import load_settings
    from core.images import model_can_see_images

    resolved = await _selected_model(request.model) or load_settings().get("model", "")
    if not model_can_see_images(resolved):
        raise HTTPException(
            status_code=422,
            detail=f"{resolved} cannot see images. Pick a different builder model, "
                   f"or send the message without them.",
        )


MISSING_BUILDER = {
    "type": "orchestration_error",
    "error": f"The builder orchestration '{NATIVE_BUILDER_ORCH_ID}' is not installed.",
}


async def prepare_builder_run(request: BuilderChatRequest, server_module, *, panel: bool = False):
    """Everything up to the first step: the run's id and the engine's generator.

    Separate from the streaming so the caller can decide *where* the run is
    spawned, and that turns out to matter — see `builder_chat`. `panel` is the
    builder panel's own door, whose conversations it can reopen (F214).
    """
    from core.store.resources import get_orchestration

    session_id = _session_id(request.client_session_id, panel=panel)

    orch_data = await get_orchestration(NATIVE_BUILDER_ORCH_ID)
    if not orch_data:
        return None

    orch = Orchestration.model_validate(orch_data)
    engine = OrchestrationEngine(orch, server_module)
    # Deliberately NOT in `initial_state`. Shared state is echoed to the browser
    # twice at the end of a run, checkpointed at every step boundary, returned by
    # the run-detail endpoint and fed to the memory summariser — all fine for a
    # model name, none of it fine for up to 20 MB of image. `global_variables`
    # travels the same way for the same reason.
    engine.images = list(request.images or [])

    # Minted here rather than at the `engine.run` call: the pump is keyed on it,
    # and so is everything downstream that has to recognise a builder run.
    run_id = f"{BUILDER_RUN_PREFIX}{uuid.uuid4().hex[:12]}"

    initial_state = {
        "user_message": request.message,
        "chat_history": _format_history(request.history),
        "selected_agent_ids": list(request.selected_agent_ids or []),
        "can_create_agents": bool(request.can_create_agents),
        **await _target_state(
            request.message,
            request.current_orchestration_id,
            await _previous_matches(session_id),
        ),
        "org_profile": await _workspace_profile(),
        "requirements": "",
        "human_response": {},
        "final_orch": "",
        # Applied by the engine on run and re-applied on resume, so the second
        # half of a conversation runs on the model the first half did.
        OrchestrationEngine.MODEL_OVERRIDE_KEY: await _selected_model(request.model),
    }

    return run_id, engine.run(
        initial_input=request.message,
        run_id=run_id,
        session_id=session_id,
        initial_state=initial_state,
    )


async def run_builder_stream(request: BuilderChatRequest, server_module):
    """The run as a generator, for the chat page's adapter below."""
    prepared = await prepare_builder_run(request, server_module)
    if prepared is None:
        yield dict(MISSING_BUILDER)
        return
    run_id, agen = prepared
    async for event in stream_engine_events(agen, run_id):
        if not _is_internal(event.get("type", "")):
            yield event


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


def _stream(source) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in iter_with_heartbeat(source):
                if isinstance(event, str):  # heartbeat comment — pass through
                    yield event
                    continue
                yield _sse(event)
                await asyncio.sleep(0)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            yield _sse({"type": "orchestration_error", "error": str(exc)})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _queue_events(queue):
    """The pump's queue as the panel's events."""
    while True:
        event = await queue.get()
        if event is SENTINEL:
            return
        if not _is_internal(event.get("type", "")):
            yield event


def _one(event: dict) -> StreamingResponse:
    async def single():
        yield event
    return _stream(single())


def _refuse_if_active(run_id: str) -> None:
    """Two pumps for one run would interleave its journal and duplicate its
    work — the same guard the other resume routes carry."""
    active = _active_tasks.get(run_id)
    if active is not None and not active.done():
        raise HTTPException(status_code=409, detail="Run is already active")


#: How long a builder run may wait on its question before it is cancelled
#: (F214, Naveen 2026-09-26). Long enough for a plan left overnight.
PARKED_BUILDER_TTL = timedelta(hours=24)

#: How many of a conversation's turns come back with it: its newest runs.
HISTORY_TURNS = 10

#: A builder run someone can come back to: parked on a question, or working.
_OPEN = ("running", "paused")


def _parked_since(step_history: list | None, created_at: datetime | None) -> datetime | None:
    """When the run's newest question was asked: its pause entry, else the run."""
    for entry in reversed(step_history or []):
        if isinstance(entry, dict) and entry.get("status") == "paused" and entry.get("started_at"):
            try:
                return datetime.strptime(entry["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                break
    if created_at is not None and created_at.tzinfo is None:
        return created_at.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC
    return created_at


async def _expire_parked() -> list[str]:
    """Cancel this workspace's builder runs that have waited on a question too long.

    Nothing else ever ends one. The panel's own New conversation cancels the run
    it holds, but a reload, a closed tab or a second browser left it paused for
    good, on no screen (builder runs are hidden, F162). Run whenever the builder
    is used, rather than on a timer: an install nobody opens loses nothing by
    waiting. Only the builder's orchestration: a workflow's own run waiting on a
    person is that person's to answer, however long it takes.
    """
    from sqlalchemy import select

    from core.orchestration.cancel import cancel_run
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.step_history,
                   OrchestrationRunDB.created_at)
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                OrchestrationRunDB.status == "paused",
                OrchestrationRunDB.waiting_for_human.is_(True),
            )
        )).all()
    cutoff = datetime.now(timezone.utc) - PARKED_BUILDER_TTL
    expired = []
    for run_id, history, created_at in rows:
        since = _parked_since(history, created_at)
        if since is not None and since < cutoff:
            try:
                await cancel_run(run_id)
                expired.append(run_id)
            except Exception:  # noqa: BLE001 — one run that will not cancel must not stop the builder
                _log.warning("could not expire parked builder run %s", run_id, exc_info=True)
    return expired


async def _open_conversation() -> dict | None:
    """The panel's newest conversation that is still open, from its run rows (F214).

    The panel held its conversation in React state only, so a reload showed the
    greeting while the server finished the turn, and a plan waiting for approval
    sat paused on no screen. Cloud's F148, in open source: read back from the
    runs rather than the run's event journal, so the turns survive whatever the
    journal does not. A `running` run is offered only while this process drives
    it — after a restart one is left `running` with nothing writing to it, and
    following it would wait for ever.
    """
    from sqlalchemy import select

    from core.orchestration.pauses import asking_step_id
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    prefix = f"builder:{get_tenant()}:{PANEL}"
    async with session() as s:
        candidates = (await s.execute(
            select(OrchestrationRunDB)
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                OrchestrationRunDB.session_id.like(f"{prefix}%"),
                OrchestrationRunDB.status.in_(_OPEN),
            )
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(5)
        )).scalars().all()

        def live(row) -> bool:
            if not (row.session_id or "").startswith(prefix):
                return False  # LIKE's `_` matches any one character
            if row.status == "paused":
                return bool(row.waiting_for_human)
            task = _active_tasks.get(row.run_id)
            return task is not None and not task.done()

        run = next((row for row in candidates if live(row)), None)
        if run is None:
            return None
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.shared_state,
                   OrchestrationRunDB.human_prompt)
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.session_id == run.session_id,
            )
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(HISTORY_TURNS)
        )).all()

    turns = []
    for run_id, state, asked in reversed(rows):
        state = state or {}
        # The open run's question is `pending`, not something already said.
        said = "" if run_id == run.run_id else str(
            state.get("summary") or asked or state.get("requirements") or "")
        turns.append({"user": str(state.get("user_message") or ""), "assistant": said.strip()})

    # Where the run's journal stood when it was read. A working run is followed
    # from its journal's start, which replays the questions it has already had
    # answered; only one past this is the question it is asking now.
    from core.orchestration.journal import open_journal
    journal = open_journal(run.run_id) if run.status == "running" else None
    cursor = journal.last_id() if journal is not None else 0

    pending = None
    if run.status == "paused":
        fields = run.human_fields if isinstance(run.human_fields, list) else []
        first = fields[0] if fields and isinstance(fields[0], dict) else {}
        pending = {
            "run_id": run.run_id,
            "field": first.get("name") or "feedback",
            "step_id": asking_step_id(run.step_history, run.current_step_id),
            "prompt": run.human_prompt or "",
        }
    return {
        "client_session_id": run.session_id[len(prefix):],
        "run_id": run.run_id,
        "status": run.status,
        "turns": turns,
        "pending": pending,
        "cursor": str(cursor),
    }


@router.get("/api/builder/conversation")
async def builder_conversation():
    """The panel's conversation that is still open, if any, to redraw after a reload."""
    await _expire_parked()
    return {"conversation": await _open_conversation()}


@router.post("/api/builder/chat")
async def builder_chat(request: BuilderChatRequest, http_request: Request):
    """Start a build and stream it.

    The run is spawned **here**, in the handler, and not inside the response —
    which is how every other orchestration entry point does it, and the
    difference is not cosmetic. The response body is cancelled the moment the
    browser goes away, and whatever the body was advancing is cancelled with
    it — before this, `prepare_builder_run`'s reads and the run's spawn both
    ran inside it, and a real build stalled where it stood. (D99 put that down
    to `BaseHTTPMiddleware`'s cancel scope; anyio tracks scopes per task, so a
    task the body creates does not inherit one — F49 measured the real cause.)
    `tests/unit/test_response_bodies_start_no_work.py` holds the rule for
    every route.
    """
    await reject_unseeable_images(request)
    await _expire_parked()
    prepared = await prepare_builder_run(
        request, http_request.app.state.server_module, panel=True)
    if prepared is None:
        return _one(dict(MISSING_BUILDER))
    run_id, agen = prepared
    _task, queue = spawn_engine_run(agen, run_id)
    return _stream(_queue_events(queue))


@router.post("/api/builder/resume")
async def builder_resume(request: BuilderResumeRequest, http_request: Request):
    """Resume a paused builder run after the user answers.

    `OrchestrationEngine.resume` directly, and spawned here for the same reason
    `builder_chat` is. There used to be a hand-copied near-duplicate of the
    engine's resume in this module, and it had drifted: it never called
    `_complete_human_step`, so the approval stayed open in the run's history,
    and it re-applied the model itself because there was nowhere to carry it.
    Both are the engine's job.
    """
    _refuse_if_active(request.run_id)
    _task, queue = spawn_engine_run(
        OrchestrationEngine.resume(
            request.run_id, request.response,
            http_request.app.state.server_module, request.images,
        ),
        request.run_id,
    )
    return _stream(_queue_events(queue))


async def _save_chat_turn(request, answer: str) -> None:
    """Keep the turn in the conversation, as any agent's turn is kept (F115).

    A builder-typed agent's turn never reaches `run_agent_step`, where a turn is
    saved, so a reopened conversation showed it only while its event log was
    held. Saved before it is shown, in the order `run_agent_step` keeps.
    """
    from core.session import _save_conversation_turn
    try:
        await _save_conversation_turn(
            session_id=getattr(request, "session_id", None) or "default",
            agent_id=getattr(request, "agent_id", None), user=request.message or "",
            assistant=answer,
        )
    except Exception as exc:  # noqa: BLE001 — the answer is still shown
        _log.warning(f"builder chat turn not saved: {exc}")


async def run_builder_stream_compat(request, server_module):
    """SSE for the main chat page, which talks to a builder-typed agent.

    The chat page has no builder panel: no inline approval box, no canvas to
    open. So the run's events are flattened into the chat vocabulary, and a
    human step ends the turn with the question as an assistant message — the
    user answers by replying, which starts a fresh turn.
    """
    # The conversation so far is what `_save_chat_turn` keeps (F115), read
    # under the same key. This read `request.history_messages`, which
    # `ChatRequest` has never had, so every turn — a reply to the builder's own
    # question included — reached it with no history at all (F134).
    from core.session import get_recent_history_messages
    history = await get_recent_history_messages(
        getattr(request, "session_id", None) or "default",
        agent_id=getattr(request, "agent_id", None),
    )
    builder_req = BuilderChatRequest(
        message=request.message,
        history=history,
        can_create_agents=True,
        client_session_id=str(getattr(request, "session_id", "") or "chat"),
        # Chat has an attach button and the builder-typed agent is reached
        # through it, so dropping these here would lose the one attachment path
        # that already exists. Already validated on the way in as ChatRequest.
        images=list(getattr(request, "images", None) or []),
    )

    try:
        await reject_unseeable_images(builder_req)
    except HTTPException as exc:
        # This generator is already streaming SSE to the chat page, so the
        # refusal has to arrive as an event rather than as a status code.
        yield _sse({"type": "error", "message": str(exc.detail)})
        yield _sse({"type": "done"})
        return

    async for event in iter_with_heartbeat(run_builder_stream(builder_req, server_module)):
        if isinstance(event, str):  # heartbeat comment — pass through
            yield event
            continue
        etype = event.get("type")

        if etype == "step_start":
            yield _sse({"type": "status", "message": f"Running: {event.get('step_name', '')}"})

        elif etype == "thinking":
            yield _sse({"type": "status", "message": event.get("message", "Thinking…")})

        elif etype == "tool_execution":
            yield _sse({"type": "tool_execution", "tool_name": event.get("tool_name"), "args": event.get("args", {})})

        elif etype == "tool_result":
            yield _sse({"type": "tool_result", "tool_name": event.get("tool_name"), "preview": event.get("preview", "")[:200]})

        elif etype == "resource_saved":
            preview = f"✓ {event.get('kind', 'resource')} '{event.get('name') or event.get('id')}' {event.get('action', 'saved')}"
            yield _sse({"type": "tool_result", "tool_name": "resource_saved", "preview": preview})

        elif etype == "human_input_required":
            question = event.get("prompt", "")
            await _save_chat_turn(request, question)
            yield _sse({"type": "response", "content": question, "intent": "chat", "data": None, "tool_name": None})
            yield _sse({"type": "done"})
            return

        elif etype == "orchestration_complete" and event.get("status") == "failed":
            # Not "Done." (F133): the run failed, and its step's error has
            # already gone to the page. The conversation keeps the question.
            from core.chat_turns import FAILED_ANSWER
            await _save_chat_turn(request, FAILED_ANSWER)
            yield _sse({"type": "done"})

        elif etype == "orchestration_complete":
            answer = (event.get("final_state") or {}).get("summary") or "Done."
            await _save_chat_turn(request, answer)
            yield _sse({"type": "response", "content": answer, "intent": "chat", "data": None, "tool_name": None})
            yield _sse({"type": "done"})

        elif etype in ("orchestration_error", "step_error"):
            yield _sse({"type": "error", "message": event.get("error", "Unknown error")})

        await asyncio.sleep(0)


# ── "See how I'm built" ──────────────────────────────────────────────────────

@router.get("/api/builder/legend")
async def builder_legend():
    """The builder's own shape, read from the definitions it ships with.

    Rendered rather than written down, so it cannot drift from the thing it
    describes the way a hand-typed diagram would.
    """
    bundle = bundled_definitions()
    orch = bundle["orchestration"]
    agents = {a["id"]: a for a in bundle["agents"]}
    by_id = {s["id"]: s for s in orch["steps"]}

    entry = by_id.get(orch["entry_step_id"], {})
    return {
        "name": orch["name"],
        "description": orch["description"],
        "step_count": len(orch["steps"]),
        "entry_step": {"id": entry.get("id"), "name": entry.get("name"), "type": entry.get("type")},
        "routes": [
            {"label": label, "to": by_id.get(target, {}).get("name", target),
             "description": (entry.get("route_descriptions") or {}).get(label, "")}
            for label, target in (entry.get("route_map") or {}).items()
        ],
        "human_gates": [
            {"id": s["id"], "name": s["name"], "asks": (s.get("human_fields") or [{}])[0].get("label", "")}
            for s in orch["steps"] if s.get("type") == "human"
        ],
        "agents": [
            {
                "role": agents[s["agent_id"]].get("system_role"),
                "name": agents[s["agent_id"]]["name"],
                "description": agents[s["agent_id"]].get("description", ""),
                "step": s["name"],
                "tools": agents[s["agent_id"]].get("tools", []),
            }
            for s in orch["steps"]
            if s.get("agent_id") and s["agent_id"] in agents
        ],
        "step_types": sorted({s["type"] for s in orch["steps"]}),
        # What the modal's read-only canvas draws: the steps as shipped, and
        # the names their `agent_id`s resolve to.
        "canvas": {
            "orchestration": {
                "id": orch.get("id") or "native_builder",
                "name": orch["name"],
                "entry_step_id": orch["entry_step_id"],
                "steps": orch["steps"],
            },
            "agents": [{"id": a["id"], "name": a["name"]} for a in bundle["agents"]],
        },
    }


@router.post("/api/builder/duplicate")
async def builder_duplicate():
    """Copy the builder into the workspace as an ordinary, editable orchestration.

    Fresh ids, agent references remapped to them, and the `system` marker
    stripped — so the copy is the customer's, not another instance of ours.
    """
    from core.builder_tools import _random_id
    from core.routes.agents import load_user_agents
    from core.routes.orchestrations import load_orchestrations
    from core.store.resources import save_agent, save_orchestration

    bundle = bundled_definitions()
    taken_agents = {a.get("id") for a in await load_user_agents()}
    taken_orchs = {o.get("id") for o in await load_orchestrations()}

    def _mint(prefix: str, taken: set[str]) -> str:
        new_id = _random_id(prefix, length=10)
        while new_id in taken:
            new_id = _random_id(prefix, length=10)
        taken.add(new_id)
        return new_id

    mapping: dict[str, str] = {}
    for agent in bundle["agents"]:
        copy = {k: v for k, v in agent.items() if k not in ("system", "system_role")}
        copy["id"] = _mint("agent_", taken_agents)
        copy["name"] = f"{agent['name']} (Copy)"
        mapping[agent["id"]] = copy["id"]
        # The orchestrator shell points at the orchestration; fixed up below.
        if copy.get("orchestration_id"):
            copy["orchestration_id"] = None
        await save_agent(copy)

    orch = {k: v for k, v in bundle["orchestration"].items() if k != "system"}
    orch["id"] = _mint("orch_", taken_orchs)
    orch["name"] = f"{bundle['orchestration']['name']} (Copy)"
    orch["steps"] = [
        {**s, "agent_id": mapping.get(s["agent_id"], s["agent_id"])} if s.get("agent_id") else s
        for s in orch["steps"]
    ]
    await save_orchestration(orch)

    return {
        "status": "created",
        "orchestration_id": orch["id"],
        "name": orch["name"],
        "agent_ids": list(mapping.values()),
    }
