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

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.images import checked_images
from core.models_orchestration import Orchestration
from core.builder_access import CALLER_KEY
from core.builder_research import previous_findings
from core.native_builder import (
    BUILDER_RUN_PREFIX,
    NATIVE_BUILDER_ORCH_ID,
    bundled_definitions,
    door,
    export_state,
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
    #: The person's own timezone, as the browser reports it (F948): a schedule they name no timezone for runs on it.
    timezone: str | None = None
    #: "Try again on a stronger model" (F1018): the failed run this turn is pressed from. The server checks it and
    #: picks the model (`try_stronger.panel_retry`); `model` is then the server's, never the caller's.
    retry_of: str | None = None
    #: "Use <stronger model>" on the effort offer before a build (F1153): the turn that offered it. The server checks
    #: it and picks the model (`effort.effort_press`); the build runs on it, for that build only.
    effort_of: str | None = None

    _check_images = checked_images()


def _person_timezone(name: str | None) -> str | None:
    """The browser's timezone when it is one this server knows; else none, and a schedule runs in UTC."""
    from core.models_schedule import _is_timezone

    return name if name and _is_timezone(name) else None


class BuilderResumeRequest(BaseModel):
    run_id: str
    response: dict = {}
    #: Attachments sent *with the answer*. The images a run started with are
    #: not persisted anywhere — see `OrchestrationEngine.resume` — so a step
    #: re-entered after a pause sees only what was attached to the reply.
    images: list[str] = []

    _check_images = checked_images()


#: What a conversation keeps in place of a message the door refused.
NOT_KEPT = door.NOT_KEPT


def _as_kept(message: str) -> str:
    """The message as a run or a conversation may keep it (BWA1, D274): one with a
    card number or a password is not kept at all, and every other loses its keys
    and tokens. SW1 took credentials only out of pasted Agents SDK code; the door
    (`core/native_builder/door.py`) does it for every message."""
    checked = door.check(message)
    return NOT_KEPT if checked.refused else checked.text


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    # The panel sends every earlier message back, the paste with the
    # credentials its own turn took out among them.
    return "\n".join(f"{m.get('role', 'user')}: {_as_kept(str(m.get('content', '')))}" for m in history)


async def _newest_state(session_id: str) -> dict:
    """The shared state of this conversation's newest turn, before this message's own run exists."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        return (
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


async def _previous_matches(session_id: str, state: dict | None = None) -> str:
    """What the previous turn of this conversation asked about, if it asked.

    When a turn could not tell which orchestration was meant, its state lists
    the candidates; the next message in the same conversation is resolved
    against that list, so "the second one" is an answer (F16). Only the newest
    turn counts: a question two turns back has been answered or dropped since.
    """
    from core.native_builder.target import AMBIGUOUS

    state = await _newest_state(session_id) if state is None else state
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



def _knowledge_steps() -> str:
    """The Retrieve and Ingest palette while Knowledge is on, else nothing."""
    from core.knowledge.config import knowledge_enabled
    from core.native_builder import KNOWLEDGE_STEPS_PALETTE

    return KNOWLEDGE_STEPS_PALETTE if knowledge_enabled() else ""


def _quick_roles() -> dict[str, str]:
    """`{"quick": model}` when the workspace sets "Model for quick steps", else nothing (BWA1, D274)."""
    from core.config import load_settings

    quick = (load_settings().get("builder_quick_model") or "").strip()
    return {"quick": quick} if quick else {}


async def prepare_builder_run(request: BuilderChatRequest, server_module, *, panel: bool = False,
                              retried: dict | None = None, pressed: dict | None = None):
    """Everything up to the first step: the run's id and the engine's generator.

    Separate from the streaming so the caller can decide *where* the run is
    spawned, and that turns out to matter — see `builder_chat`. `panel` is the
    builder panel's own door, whose conversations it can reopen (F214).
    `retried` is a pressed "Try again on a stronger model" (F1018): the run it tries again and the model, recorded.
    `pressed` is a pressed effort offer (F1153): the turn it was pressed from, recorded.
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

    # Pasted Agents SDK code (an Agent Builder export, SW1): the guide to what
    # it becomes and the code itself, credentials taken out before the run,
    # its checkpoints or any model sees the message. Both empty otherwise.
    export = export_state(request.message)
    message = export["user_message"]
    previous = await _newest_state(session_id)
    # The findings card the previous turn ended in, if it did (F766): its "go ahead and build it" goes to the build,
    # which plans from them instead of researching again.
    found = previous_findings(previous)

    initial_state = {
        "user_message": message,
        "chat_history": _format_history(request.history),
        "selected_agent_ids": list(request.selected_agent_ids or []),
        "can_create_agents": bool(request.can_create_agents),
        "person_timezone": _person_timezone(request.timezone),
        # Knowledge's two step types, for the builder's prompts while Knowledge
        # is on; empty while off, so no workspace is offered steps that would
        # fail its runs (R1).
        "knowledge_steps": _knowledge_steps(),
        "export_guide": export["export_guide"],
        "export_source": export["export_source"],
        **await _target_state(
            # A name in pasted code is the code's, not a workflow being named:
            # only the one on screen counts.
            "" if export["export_source"] else message,
            request.current_orchestration_id,
            await _previous_matches(session_id, previous),
        ),
        "prior_findings": found,
        "plan_findings": found,
        "org_profile": await _workspace_profile(),
        "requirements": "",
        "human_response": {},
        "final_orch": "",
        # Applied by the engine on run and re-applied on resume, so the second
        # half of a conversation runs on the model the first half did.
        OrchestrationEngine.MODEL_OVERRIDE_KEY: await _selected_model(request.model),
        # The builder's quick steps (its routing evaluators, `model_role: "quick"`) on their own model when the
        # workspace names one (BWA1); empty leaves them on the builder model.
        OrchestrationEngine.MODEL_ROLES_KEY: _quick_roles(),
        # Whom the run works for (ASSIST1, F681): self-hosted has one person, who may build and change anything.
        # A host with roles writes its person and whether their role may (`builder_access`).
        CALLER_KEY: {"user_id": None, "may_change": True},
        # The router's door before a build or a change (F682): read by `step_may_change`, said by `step_not_yours`.
        "may_change": True,
        # The change route's door (SAI2-P2): approver and up reach it, and each op's role is checked there.
        "may_act": True,
        "asker_role": "owner",
        **({k: v for k, v in retried.items() if k != "model"} if retried else {}),
        **({k: v for k, v in pressed.items() if k not in ("model", "message")} if pressed else {}),
    }

    return run_id, engine.run(
        initial_input=message,
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


def _panel_prefix() -> str:
    """Every panel chat's session id starts with this: one workspace's Synapse AI chats."""
    from core.tenancy import get_tenant
    return f"builder:{get_tenant()}:{PANEL}"


def _driven(run) -> bool:
    """Whether a run someone could come back to still is one: parked on its question, or working on this process.

    A `running` row is offered only while this process drives it: after a restart one is left `running` with nothing
    writing to it, and following it would wait for ever.
    """
    if run.status == "paused":
        return bool(run.waiting_for_human)
    task = _active_tasks.get(run.run_id)
    return task is not None and not task.done()


async def _open_conversation() -> dict | None:
    """The panel's newest conversation that is still open, from its run rows (F214).

    The panel held its conversation in React state only, so a reload showed the
    greeting while the server finished the turn, and a plan waiting for approval
    sat paused on no screen. Cloud's F148, in open source: read back from the
    runs rather than the run's event journal, so the turns survive whatever the
    journal does not.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    prefix = _panel_prefix()
    async with session() as s:
        candidates = (await s.execute(
            select(OrchestrationRunDB)
            # Not the run's copy of the builder's workflow (F443): ~60 KB each.
            .options(defer(OrchestrationRunDB.definition))
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                OrchestrationRunDB.session_id.like(f"{prefix}%"),
                OrchestrationRunDB.status.in_(_OPEN),
            )
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(5)
        )).scalars().all()
        # LIKE's `_` matches any one character.
        run = next((row for row in candidates if (row.session_id or "").startswith(prefix) and _driven(row)), None)
        return await _drawn(s, run) if run is not None else None


async def _chat(client_session_id: str) -> dict | None:
    """One Synapse AI chat by its id, open or finished, to carry on with (TABS2, F1061).

    A chat is picked from History or the panel's list long after it ended, so this reads it whatever its newest turn
    did; only a turn still working on this process is `running`, to be followed.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        run = (await s.execute(
            select(OrchestrationRunDB)
            .options(defer(OrchestrationRunDB.definition))
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                OrchestrationRunDB.session_id == _panel_prefix() + client_session_id,
            )
            .order_by(OrchestrationRunDB.created_at.desc())
            .limit(1)
        )).scalars().first()
        return await _drawn(s, run) if run is not None else None


async def _drawn(s, run) -> dict:
    """A chat as the panel draws it: its newest turns, the question its newest run is asking, and where to follow from."""
    from sqlalchemy import select

    from core.orchestration.pauses import asking_step_id
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    live = _driven(run)
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
        said = "" if (run_id == run.run_id and live) else _said(state, asked)
        turns.append({"user": str(state.get("user_message") or ""), "assistant": said})

    # Where the run's journal stood when it was read. A working run is followed
    # from its journal's start, which replays the questions it has already had
    # answered; only one past this is the question it is asking now.
    from core.orchestration.journal import open_journal
    journal = open_journal(run.run_id) if (run.status == "running" and live) else None
    cursor = journal.last_id() if journal is not None else 0

    pending = None
    if run.status == "paused" and live:
        fields = run.human_fields if isinstance(run.human_fields, list) else []
        first = fields[0] if fields and isinstance(fields[0], dict) else {}
        pending = {
            "run_id": run.run_id,
            "field": first.get("name") or "feedback",
            "step_id": asking_step_id(run.step_history, run.current_step_id),
            "prompt": run.human_prompt or "",
        }
    status = run.status
    if status == "running" and not live:
        status = "stopped"  # left `running` by a restart: nothing will finish it
    return {
        "client_session_id": run.session_id[len(_panel_prefix()):],
        "run_id": run.run_id,
        "status": status,
        "turns": turns,
        "pending": pending,
        "cursor": str(cursor),
    }


def _said(state: dict, asked: str | None) -> str:
    """What a finished turn said: its summary, else the question it asked, else what it read the ask as."""
    return str(state.get("summary") or asked or state.get("requirements") or "").strip()


#: Where a chat's turns go when History deletes it: kept, as every run is (what it cost is on it), but no longer one
#: of the panel's, so no list, no reopen and no next turn finds them.
REMOVED = "removed:"

#: The most chats one list returns: History and the panel draw a page, not an archive.
MAX_CHATS = 200


async def assistant_chats(containing: str | None = None, limit: int = MAX_CHATS) -> list[dict]:
    """This workspace's Synapse AI chats, newest first, one row each (TABS2, F1062).

    Read from the builder's runs, a turn each, grouped by the chat's id. `containing` (already case-folded) keeps the
    chats where any turn's ask or answer holds it: History's search asks the server (TABS1, F926). Shaped like a chat
    session's row, so History draws both alike; `title` is the chat's first ask.
    """
    from sqlalchemy import select

    from core.default_agent import DEFAULT_AGENT_ID
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    prefix = _panel_prefix()
    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.session_id, OrchestrationRunDB.status,
                   OrchestrationRunDB.waiting_for_human, OrchestrationRunDB.created_at,
                   OrchestrationRunDB.shared_state["user_message"].as_string(),
                   OrchestrationRunDB.shared_state["summary"].as_string(),
                   OrchestrationRunDB.human_prompt)
            .where(
                OrchestrationRunDB.tenant_id == get_tenant(),
                OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                OrchestrationRunDB.session_id.like(f"{prefix}%"),
            )
            .order_by(OrchestrationRunDB.created_at.asc())
        )).all()

    chats: dict[str, dict] = {}
    for run_id, session_id, status, waiting, created_at, asked, summary, prompt in rows:
        if not (session_id or "").startswith(prefix):
            continue  # LIKE's `_` matches any one character
        chat = chats.setdefault(session_id, {
            "session_id": session_id[len(prefix):], "agent_id": DEFAULT_AGENT_ID,
            "title": "", "first_user_message": None, "last_response": None, "last_updated": None,
            "turn_count": 0, "running": False, "waiting": False, "_words": [],
        })
        said = str(summary or prompt or "").strip()
        chat["turn_count"] += 1
        if chat["first_user_message"] is None and asked:
            chat["first_user_message"] = chat["title"] = str(asked).strip()
        chat["last_response"] = said or chat["last_response"]
        chat["last_updated"] = _stamp(created_at)
        # The newest turn says how the chat stands now.
        chat["running"] = status == "running" and _driven(_Row(run_id, status, waiting))
        chat["waiting"] = status == "paused" and bool(waiting)
        chat["_words"] += [str(asked or ""), said]

    listed = sorted(chats.values(), key=lambda c: c["last_updated"] or "", reverse=True)
    if containing:
        listed = [c for c in listed if any(containing in w.casefold() for w in c["_words"])]
    for chat in listed:
        del chat["_words"]
    return listed[:limit]


class _Row:
    """The three columns `_driven` reads, for a row read as columns."""
    def __init__(self, run_id: str, status: str, waiting_for_human: bool | None):
        self.run_id, self.status, self.waiting_for_human = run_id, status, waiting_for_human


def _stamp(when: datetime | None) -> str | None:
    """A run's time as a chat session's `last_updated` is written, so History orders both alike."""
    from core.store.sessions import _iso
    return _iso(when)


async def assistant_chat_status(client_session_id: str) -> dict:
    """How one Synapse AI chat stands, for a tab behind the front one (TABS1's status read; F1061)."""
    found = next((c for c in await assistant_chats() if c["session_id"] == client_session_id), None)
    return {
        "running": bool(found and found["running"]),
        "pending_approval": bool(found and found["waiting"]),
        "turn_count": found["turn_count"] if found else 0,
        "last_updated": found["last_updated"] if found else None,
    }


async def remove_assistant_chat(client_session_id: str) -> bool:
    """Take a Synapse AI chat out of every list (History's Delete; F1062). True when there was one.

    A question it was waiting on is cancelled first, so nothing is left paused on no screen. Its runs are kept, as
    every run is, under a session id no list reads: what they cost is on them.
    """
    from sqlalchemy import select, update

    from core.orchestration.cancel import cancel_run
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    held = _panel_prefix() + client_session_id
    where = (OrchestrationRunDB.tenant_id == get_tenant(),
             OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
             OrchestrationRunDB.session_id == held)
    async with session() as s:
        waiting = (await s.execute(select(OrchestrationRunDB.run_id).where(
            *where, OrchestrationRunDB.status == "paused"))).scalars().all()
    for run_id in waiting:
        try:
            await cancel_run(run_id)
        except Exception:  # noqa: BLE001 — a run that will not cancel must not keep the chat listed
            _log.warning("could not cancel builder run %s of a removed chat", run_id, exc_info=True)
    async with session() as s:
        moved = await s.execute(update(OrchestrationRunDB).where(*where).values(
            session_id=f"builder:{get_tenant()}:{REMOVED}{client_session_id}"))
        return bool(moved.rowcount)


@router.get("/api/builder/conversation")
async def builder_conversation(session: str | None = Query(default=None, min_length=1, max_length=255)):
    """A Synapse AI chat to draw: the one `session` names, open or finished (TABS2), else the panel's conversation
    that is still open, if any, to redraw after a reload (F214)."""
    await _expire_parked()
    return {"conversation": await (_chat(session) if session else _open_conversation())}


@router.get("/api/builder/conversations")
async def builder_conversations(q: str | None = Query(default=None, max_length=200)):
    """Synapse AI's chats and nothing else, newest first: the panel's own list (TABS2, F1062)."""
    await _expire_parked()
    words = (q or "").strip().casefold()
    return {"conversations": await assistant_chats(containing=words or None)}


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
    # The door (BWA1): a card number or a password ends here, before a run, a
    # model or a log sees it; a key is masked and the rest still runs.
    checked = door.check(request.message)
    if checked.refused:
        return _one(checked.event())
    request = request.model_copy(update={"message": checked.text})
    await _expire_parked()
    from core import try_stronger

    retried = None
    if request.retry_of:
        # The press is checked as the turn it tries again, and the model is the server's own pick (F1018).
        retried = await try_stronger.panel_retry(request.retry_of, _session_id(request.client_session_id, panel=True))
        if isinstance(retried, str):
            return _one({"type": "orchestration_error", "error": retried})
        request = request.model_copy(update={"model": retried["model"]})
    pressed = None
    if request.effort_of and not request.retry_of:
        # The effort offer's press (F1153): checked as that chat's latest turn, the ask is the build's message, and
        # the model is the server's own pick now.
        from core.native_builder import effort

        pressed = await effort.effort_press(request.effort_of, _session_id(request.client_session_id, panel=True))
        if isinstance(pressed, str):
            return _one({"type": "orchestration_error", "error": pressed})
        request = request.model_copy(update={"model": pressed["model"], "message": pressed["message"]})
    prepared = await prepare_builder_run(
        request, http_request.app.state.server_module, panel=True, retried=retried, pressed=pressed)
    if prepared is None:
        return _one(dict(MISSING_BUILDER))
    run_id, agen = prepared
    _task, queue = spawn_engine_run(try_stronger_after(agen, run_id, retried=retried is not None), run_id)
    return _stream(_after(checked.event(), _queue_events(queue)))


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
    # The door (BWA1), with the question the answer is for: a refused answer
    # leaves the question open, so the panel shows why and the person answers again.
    try:
        response, removed = door.check_answer(request.response, question=await _pending_question(request.run_id))
    except door.Refused as exc:
        return _one(door.notice(exc.reason, door.NOT_KEPT, final=False))
    _task, queue = spawn_engine_run(
        OrchestrationEngine.resume(
            request.run_id, response,
            http_request.app.state.server_module, request.images,
        ),
        request.run_id,
    )
    masked = door.notice("masked", door.kept_answer(response), final=False) if removed else None
    return _stream(_after(masked, _queue_events(queue)))


async def _pending_question(run_id: str) -> str | None:
    """What a paused builder run asked, for reading its answer (the door's `question`)."""
    from sqlalchemy import select

    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    async with session() as s:
        return (await s.execute(
            select(OrchestrationRunDB.human_prompt).where(
                OrchestrationRunDB.tenant_id == get_tenant(), OrchestrationRunDB.run_id == run_id)
        )).scalar_one_or_none()


async def try_stronger_after(agen, run_id: str, *, retried: bool):
    """The run's events, then the offer a failed turn ends with (F1018): once the run has ended, from its stored
    state. A turn tried again is not offered again."""
    async for event in agen:
        yield event
    from core.native_builder.effort import failure_notice

    # A build that failed is said plainly first, on whichever model it ran (F1153); then F1018's one offer.
    if (said := await failure_notice(run_id)) is not None:
        yield said
    if not retried:
        from core import try_stronger

        if (offered := await try_stronger.panel_offer(run_id)) is not None:
            yield offered


async def _after(first: dict | None, events):
    """`events`, with `first` (the door's notice) ahead of them when there is one."""
    if first:
        yield first
    async for event in events:
        yield event


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
            agent_id=getattr(request, "agent_id", None), user=_as_kept(request.message or ""),
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
    # The door (BWA1): a refused message is answered here and kept nowhere,
    # not even as this conversation's turn.
    checked = door.check(request.message or "")
    if checked.refused:
        yield _sse({"type": "response", "content": checked.notice, "intent": "chat", "data": None,
                    "tool_name": None})
        yield _sse({"type": "done"})
        return
    if checked.removed:
        yield _sse({"type": "status", "message": checked.notice})
    builder_req = BuilderChatRequest(
        message=checked.text,
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
    from core.native_builder.classify import KINDS

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
        # The four kinds the front door types a request into (SAI2-P4), and the step each goes to first.
        "routes": [
            {"label": label, "to": by_id.get(target, {}).get("name", target), "description": description}
            for label, (target, description) in KINDS.items()
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

    orch_id = _mint("orch_", taken_orchs)
    mapping: dict[str, str] = {}
    for agent in bundle["agents"]:
        copy = {k: v for k, v in agent.items() if k not in ("system", "system_role")}
        copy["id"] = _mint("agent_", taken_agents)
        copy["name"] = f"{agent['name']} (Copy)"
        mapping[agent["id"]] = copy["id"]
        # The orchestrator shell runs the orchestration copied with it (F289).
        # It was set to None here, "fixed up below", and nothing below did: the
        # copy's workflow agent ran nothing and failed on its first message.
        if copy.get("orchestration_id"):
            copy["orchestration_id"] = orch_id
        await save_agent(copy)

    orch = {k: v for k, v in bundle["orchestration"].items() if k != "system"}
    orch["id"] = orch_id
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
