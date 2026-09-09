"""
Session history REST API.

GET  /api/sessions                          → list all sessions (sorted by last_updated desc)
GET  /api/sessions?agent_id={id}            → filter by agent
GET  /api/sessions/{session_id}/history     → full turn list for a session
GET  /api/sessions/{session_id}/snapshot    → {last_response, last_updated}
DELETE /api/sessions/{session_id}           → delete a session
POST /api/sessions/{session_id}/last-turn/pop → take the last turn back (retry)
"""
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.session import (
    list_chat_sessions,
    _get_conversation_history,
    get_last_response_snapshot,
    delete_chat_session,
    pop_last_conversation_turn,
)

router = APIRouter()


@router.get("/api/sessions")
async def get_sessions(agent_id: str | None = Query(default=None)):
    """List all persisted chat sessions, newest first."""
    return await list_chat_sessions(agent_id=agent_id)


@router.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str, agent_id: str | None = Query(default=None)):
    """Return the full list of turns for a session."""
    turns = await _get_conversation_history(session_id, agent_id=agent_id)
    return {"session_id": session_id, "agent_id": agent_id, "turns": turns}


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
    turn = await pop_last_conversation_turn(
        session_id, agent_id=body.agent_id, expect_user=body.expect_user)
    return {"session_id": session_id, "removed": turn is not None, "turn": turn}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, agent_id: str | None = Query(default=None)):
    """Delete a session's persisted history."""
    deleted = await delete_chat_session(session_id, agent_id=agent_id)
    if deleted:
        return {"status": "deleted", "session_id": session_id}
    return JSONResponse(status_code=404, content={"detail": "Session not found"})
