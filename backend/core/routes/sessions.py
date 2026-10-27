"""
Session history REST API.

GET  /api/sessions                          → list all sessions (sorted by last_updated desc)
GET  /api/sessions?agent_id={id}            → filter by agent
GET  /api/sessions?q={text}                 → only those whose questions or answers contain it
POST /api/sessions/status                   → how each of up to 10 open chat tabs stands
GET  /api/sessions/{session_id}/history     → full turn list for a session
GET  /api/sessions/{session_id}/snapshot    → {last_response, last_updated}
DELETE /api/sessions/{session_id}           → delete a session
POST /api/sessions/{session_id}/last-turn/pop → take the last turn back (retry)
"""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import chat_turns
from core.default_agent import DEFAULT_AGENT_ID
from core.routes.builder import assistant_chat_status, assistant_chats, remove_assistant_chat

from core.session import (
    _turns,
    list_chat_sessions,
    _get_conversation_history,
    get_last_response_snapshot,
    delete_chat_session,
    pop_last_conversation_turn,
)

router = APIRouter()


def _the_builders_own(session: dict) -> bool:
    """A conversation the AI builder keeps for one of its own sub-agents.

    Every builder run keeps one per sub-agent, under a `builder:` session id,
    and chat's History listed them — each previewing its whole step prompt
    (F228, F186's class). A builder-typed agent the person made keeps its chat
    turns under the chat's own id, so it is not caught here.
    """
    return (str(session.get("session_id") or "").startswith("builder:")
            or str(session.get("agent_id") or "").startswith("agent_native_builder"))


def _an_api_turns_own(session: dict) -> bool:
    """A conversation the OpenAI-compatible endpoint holds while a call waits on a person (OAI1).

    The endpoint saves no chat; its turn's row exists only to park the call,
    and goes once it is answered or expires (`core.api_approvals`).
    """
    return str(session.get("session_id") or "").startswith("oai_")


def _a_runs_own(session: dict) -> bool:
    """A conversation an orchestration or a sub-agent left, not a person (F261).

    A workflow's step agents saved each rendered step prompt under the run's
    session — `orch_<run id>` for a run started from the editor, the API or a
    delegation — and every `spawn_subtask` its own `subtask_*` agent's. They
    save nothing now; the rows they left are hidden here, not deleted.
    """
    return (str(session.get("session_id") or "").startswith("orch_")
            or str(session.get("agent_id") or "").startswith("subtask_"))


@router.get("/api/sessions")
async def get_sessions(agent_id: str | None = Query(default=None),
                       q: str | None = Query(default=None, max_length=200)):
    """List the person's chat sessions, newest first.

    `q` is History's search (TABS1, F926). It asks here, not in the page, so a
    conversation the drawer hasn't drawn is still found: any question or answer
    in any turn, ignoring case.
    """
    words = (q or "").strip().casefold()
    listed = [s for s in await list_chat_sessions(agent_id=agent_id, containing=words or None)
              if not _the_builders_own(s) and not _a_runs_own(s) and not _an_api_turns_own(s)]
    if agent_id in (None, DEFAULT_AGENT_ID):
        # Synapse AI's chats, kept as the builder's runs rather than chat rows (TABS2, F1062): one list, newest first.
        listed += await assistant_chats(containing=words or None)
        listed.sort(key=lambda s: s.get("last_updated") or "", reverse=True)
    return listed


#: The most tabs a person keeps open in one area (TABS1); the status read takes no more.
MAX_TABS = 10


class TabSession(BaseModel):
    session_id: str = Field(min_length=1, max_length=255)
    agent_id: str = Field(min_length=1, max_length=255)


class TabStatusRequest(BaseModel):
    sessions: list[TabSession] = Field(max_length=MAX_TABS)


@router.post("/api/sessions/status")
async def get_sessions_status(body: TabStatusRequest):
    """How each open chat tab's conversation stands, in one request (TABS1, F925).

    Only the tab in front streams: plain HTTP allows a page about six
    connections per host, so the others ask here. A tab is one conversation —
    a session under one agent — so a turn another agent runs in the same
    session id (one id carried every agent's chat before tabs) isn't this
    tab's. A conversation with no row yet (its first turn is saved when it
    ends) still gets one, so no tab is left without an answer.
    """
    from core.store import sessions as store

    rows = []
    for tab in body.sessions:
        if tab.agent_id == DEFAULT_AGENT_ID:
            # A Synapse AI chat is its builder runs, not a chat row (TABS2, F1061).
            rows.append({"session_id": tab.session_id, "agent_id": tab.agent_id, "automated": False,
                         **await assistant_chat_status(tab.session_id)})
            continue
        saved = await store.get(tab.session_id, tab.agent_id)
        turn = chat_turns.live(tab.session_id)
        pending = await store.get_pending_approval(tab.session_id, tab.agent_id)
        running = turn is not None and turn.agent_key in (tab.agent_id, "default")
        rows.append({
            "session_id": tab.session_id, "agent_id": tab.agent_id,
            "running": running,
            # A turn the person didn't send (a helper's follow-up): its end is news, not their answer.
            "automated": running and bool(turn.automated),
            "pending_approval": pending is not None,
            "turn_count": len(_turns(saved["messages"])) if saved else 0,
            "last_updated": saved["last_updated"] if saved else None,
        })
    return {"sessions": rows}


async def _background_helpers_as_they_are(turns: list[dict]) -> None:
    """A background helper's record as its own conversation holds it now (SUB1a).

    The turn that sent it saved it as it started; it works on, and ends, in a
    conversation of its own (`helperc_…`), whose row says how it went and what it
    did. A reopened page draws its lane from that.
    """
    from core.store import sessions as store

    for turn in turns:
        for kept in turn.get("helpers") or []:
            if kept.get("mode") != "background" or not kept.get("session_id"):
                continue
            row = await store.get(kept["session_id"], kept.get("id") or "")
            if row is None:
                continue
            ended = next((m.get("helper") for m in reversed(row.get("messages") or []) if m.get("helper")), None)
            if ended:
                kept.update({k: ended[k] for k in ("status", "seconds", "calls", "answer", "error") if k in ended})
            else:
                kept["status"] = "running" if row.get("status") == "running" else (row.get("status") or "running")


@router.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str, agent_id: str | None = Query(default=None)):
    """Return the full list of turns for a session — and what is still open in it.

    A turn is saved when it ends, so a conversation reopened mid-turn is told
    about it here (`running`, readable at `/chat/{id}/events`), and one waiting
    on a tool call gets its question back (`pending_approval`, the card without
    the loop's snapshot).
    """
    from core.chat_approval import public_view
    from core.store import sessions as store

    turns = await _get_conversation_history(session_id, agent_id=agent_id)
    await _background_helpers_as_they_are(turns)
    turn = chat_turns.live(session_id)
    pending = await store.get_pending_approval(session_id, agent_id or "default")
    return {
        "session_id": session_id, "agent_id": agent_id, "turns": turns,
        "running": turn.summary() if turn else None,
        "pending_approval": public_view(pending),
    }


@router.get("/api/sessions/{session_id}/snapshot")
async def get_session_snapshot(session_id: str, agent_id: str | None = Query(default=None)):
    """Return just the last response and timestamp."""
    return await get_last_response_snapshot(session_id, agent_id=agent_id)


class PopTurnRequest(BaseModel):
    agent_id: str | None = None
    #: The question the caller believes is last. Nothing is removed unless it
    #: matches — see `store.pop_last_turn`, and note that a failed turn
    #: persists nothing, so without this a Retry deletes the previous turn.
    expect_user: str | None = None


@router.post("/api/sessions/{session_id}/last-turn/pop")
async def pop_session_last_turn(session_id: str, body: PopTurnRequest | None = None):
    """Take the last user/assistant pair back off a session.

    What Retry and edit-and-resend call before sending again: without it the
    failed answer stays in the history the model is given, and the model reads
    its own broken attempt as context for the retry.

    A POST rather than a DELETE because it carries a body: the guard is the
    text of the expected question, and a question does not belong in a URL.

    Removing nothing is the ordinary case, not an error — a turn that failed
    persisted nothing, which is exactly when someone retries. `removed` says
    which happened.
    """
    body = body or PopTurnRequest()
    if chat_turns.live(session_id) is not None:
        # The running turn saves when it ends; a pop now would remove the turn
        # before it, and that save would land after.
        raise HTTPException(status_code=409, detail="A reply is still being written in this conversation.")
    turn = await pop_last_conversation_turn(
        session_id, agent_id=body.agent_id, expect_user=body.expect_user)
    return {"session_id": session_id, "removed": turn is not None, "turn": turn}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, agent_id: str | None = Query(default=None)):
    """Delete a session's persisted history."""
    if agent_id == DEFAULT_AGENT_ID and await remove_assistant_chat(session_id):
        return {"status": "deleted", "session_id": session_id}
    deleted = await delete_chat_session(session_id, agent_id=agent_id)
    if deleted:
        return {"status": "deleted", "session_id": session_id}
    return JSONResponse(status_code=404, content={"detail": "Session not found"})
