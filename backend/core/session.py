"""
Session and conversation state management.

Backed by the `chat_sessions` table, which the standalone server and the scale
worker now share. They used to disagree: the worker held the canonical history
in Postgres and then *wrote a session file onto its own local disk* so that
`run_react_loop` could find it here — which meant, in a shared fleet, that a
conversation's history materialised on whichever worker happened to win the
job. That shim is gone; both paths read and write the same rows.

A conversation is keyed by `(session_id, agent_id)`, not by `session_id` alone.
The UI keeps one session id per browser and switches agents underneath it, so
the same id genuinely names several conversations — see `ChatSessionDB`.

Two shapes, one column
----------------------
`messages` is stored as `[{role, content}]`, which is what the worker writes
and what `GET /v2/chat/{id}/status` returns. The per-turn extras this module
has always kept — the tools used, the timestamp — ride along as additional keys
on the assistant entry, so a reader that only knows about role and content is
unaffected. `turns` is the paired view of the same list, rebuilt on read.
"""
import json
from datetime import datetime, timezone
from typing import Any

from core.models import ChatRequest

# ---------------------------------------------------------------------------
# Session-scoped in-memory state (non-persistent by design)
# ---------------------------------------------------------------------------
session_state: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _agent(agent_id: str | None) -> str:
    return agent_id or "default"


def _turns(messages: list[dict]) -> list[dict]:
    """The message list as user/assistant turns.

    Every writer appends the two together, so the pairing is exact. A message
    that does not pair — a tool or system entry written by something else —
    ends the reconstruction rather than being silently folded into a turn.
    """
    turns = []
    i = 0
    while i + 1 < len(messages):
        user, assistant = messages[i], messages[i + 1]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            break
        turns.append({
            "user": user.get("content", ""),
            "assistant": assistant.get("content", ""),
            "tools": assistant.get("tools", []),
            "timestamp": assistant.get("timestamp"),
        })
        i += 2
    return turns


# ---------------------------------------------------------------------------
# Public API — used by react_engine and routes
# ---------------------------------------------------------------------------

def _get_session_id(request: ChatRequest) -> str:
    return request.session_id or "default"


async def _get_conversation_history(session_id: str, agent_id: str | None = None) -> list[dict]:
    """Return the list of conversation turns for this session."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    return _turns(row["messages"]) if row else []


async def _save_conversation_turn(
    session_id: str,
    agent_id: str | None,
    user: str,
    assistant: str,
    tools: list[str] | None = None,
):
    """Append a turn to the session and update last_message_at."""
    from core.store import sessions as store

    now = _now()
    await store.append_turn(
        session_id,
        _agent(agent_id),
        [
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant,
             "tools": tools or [], "timestamp": now},
        ],
    )


async def pop_last_conversation_turn(session_id: str, agent_id: str | None = None) -> dict | None:
    """Take the last turn back, so it can be run again.

    The facade half of `store.pop_last_turn`; see there for why it is a pair
    and not a message. Retry uses it to drop the answer that failed before
    resending, and edit-and-resend to drop both halves before sending the
    rewritten question.
    """
    from core.store import sessions as store

    return await store.pop_last_turn(session_id, _agent(agent_id))


async def get_cli_session_id(session_id: str, agent_id: str | None, provider_key: str) -> str | None:
    """Return the stored CLI session ID for the given provider, or None."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    return (row or {}).get("cli_session_ids", {}).get(provider_key)


async def save_cli_session_id(session_id: str, agent_id: str | None, provider_key: str, cli_id: str):
    """Persist a CLI session ID for this agent+session combination and provider."""
    from core.store import sessions as store

    await store.set_cli_session_id(session_id, _agent(agent_id), provider_key, cli_id)


async def get_last_response_snapshot(session_id: str, agent_id: str | None = None) -> dict:
    """Return {last_response, last_updated} for a session."""
    from core.store import sessions as store

    row = await store.get(session_id, _agent(agent_id))
    if not row:
        return {"last_response": None, "last_updated": None}

    turns = _turns(row["messages"])
    return {
        "last_response": turns[-1]["assistant"] if turns else None,
        "last_updated": row["last_updated"],
    }


async def get_recent_history_messages(session_id: str, agent_id: str | None = None) -> list[dict]:
    """Return last N turns as [role/content] message dicts for the LLM API."""
    RECENT_TURNS = 10
    turns = await _get_conversation_history(session_id, agent_id)
    recent = turns[-RECENT_TURNS:] if len(turns) > RECENT_TURNS else turns
    messages = []
    for turn in recent:
        messages.append({"role": "user", "content": turn["user"]})
        messages.append({"role": "assistant", "content": turn["assistant"]})
    return messages


async def list_chat_sessions(agent_id: str | None = None) -> list[dict]:
    """
    List all persisted chat sessions, sorted by last_updated descending.
    Optionally filter by agent_id.
    """
    from core.store import sessions as store

    sessions = []
    for row in await store.load(agent_id=agent_id):
        turns = _turns(row["messages"])
        sessions.append({
            "session_id": row["session_id"],
            "agent_id": row["agent_id"],
            "last_response": turns[-1]["assistant"] if turns else None,
            "last_updated": row["last_updated"],
            "turn_count": len(turns),
            "first_user_message": turns[0]["user"] if turns else None,
        })
    return sessions


async def delete_chat_session(session_id: str, agent_id: str | None = None) -> bool:
    """Delete a session. Returns True if deleted."""
    from core.store import sessions as store

    return await store.delete_one(session_id, _agent(agent_id))


async def clear_all_chat_sessions() -> int:
    """Delete every session for this tenant. Returns the number removed."""
    from core.store import sessions as store

    return await store.clear()


# ---------------------------------------------------------------------------
# Shared session state (in-memory, ephemeral)
# ---------------------------------------------------------------------------

def _get_session_state(session_id: str) -> dict[str, Any]:
    if session_id not in session_state:
        session_state[session_id] = {}
    return session_state[session_id]


def _apply_sticky_args(session_id: str, tool_name: str, tool_args: Any, tool_schema: dict | None = None) -> Any:
    """Normalize tool arguments. No session state tracking."""
    if not isinstance(tool_args, dict):
        tool_args = {}
    return tool_args


def _clear_session_embeddings(session_id: str):
    """Clear session-scoped embeddings (used internally by report auto-embed)."""
    from core.server import memory_store
    if memory_store:
        memory_store.clear_session_embeddings(session_id)
        print(f"DEBUG: Cleared session embeddings for {session_id}")
