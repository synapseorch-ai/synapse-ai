"""
Chat sessions in the store.

One table shared by the standalone server and the scale worker. It replaces two
things at once: the per-session JSON files under `DATA_DIR/chat_sessions`, and
the worker shim that copied history *out* of the database onto the winning
worker's local disk so that `core/session.py` could find it there.

Keyed `(tenant_id, session_id, agent_id)`. See `ChatSessionDB` for why the
agent belongs in the key rather than beside it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from core.tenancy import get_tenant


def _iso(dt: datetime | None) -> str | None:
    """The millisecond stamp `last_updated` has always been."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _to_dict(row) -> dict:
    return {
        "session_id": row.session_id,
        "agent_id": row.agent_id,
        "status": row.status,
        "messages": list(row.messages or []),
        "cli_session_ids": dict(row.cli_session_ids or {}),
        "last_updated": _iso(row.last_message_at),
    }


async def _row(s, session_id: str, agent_id: str):
    from core.store.models import ChatSessionDB

    return (
        await s.execute(
            select(ChatSessionDB).where(
                ChatSessionDB.tenant_id == get_tenant(),
                ChatSessionDB.session_id == session_id,
                ChatSessionDB.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()


async def get(session_id: str, agent_id: str) -> dict | None:
    from core.store import session

    if not session_id:
        return None
    async with session() as s:
        row = await _row(s, session_id, agent_id)
        return _to_dict(row) if row is not None else None


async def load(agent_id: str | None = None) -> list[dict]:
    """Every session for this tenant, most recently updated first."""
    from core.store import session
    from core.store.models import ChatSessionDB

    stmt = select(ChatSessionDB).where(ChatSessionDB.tenant_id == get_tenant())
    if agent_id:
        stmt = stmt.where(ChatSessionDB.agent_id == agent_id)

    async with session() as s:
        rows = (
            await s.execute(
                stmt.order_by(
                    ChatSessionDB.last_message_at.desc().nullslast(),
                    ChatSessionDB.session_id,
                )
            )
        ).scalars().all()
    return [_to_dict(r) for r in rows]


async def append_turn(session_id: str, agent_id: str, messages: list[dict]) -> None:
    """Append messages to a session, creating it if this is the first turn."""
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        if row is None:
            s.add(ChatSessionDB(
                tenant_id=get_tenant(),
                session_id=session_id,
                agent_id=agent_id,
                messages=list(messages),
                cli_session_ids={},
                last_message_at=datetime.now(timezone.utc),
            ))
        else:
            # Reassigned rather than mutated in place: SQLAlchemy does not track
            # in-place changes to a JSON column, so `row.messages.append(...)`
            # would be a write that silently does nothing.
            row.messages = list(row.messages or []) + list(messages)
            row.last_message_at = datetime.now(timezone.utc)


async def pop_last_turn(session_id: str, agent_id: str,
                        expect_user: str | None = None) -> dict | None:
    """Take the last user/assistant pair back off a session.

    What Retry and edit-and-resend both need, and the reason it is a *pair*:
    `core.session._turns` walks the list two at a time and stops at the first
    entry that does not pair, so a dangling user message does not merely look
    untidy — it truncates every turn after it out of the history the model
    sees. Removing one half is worse than removing none.

    `expect_user` is the guard, and it is not optional in practice. **A turn
    that fails persists nothing** — `_save_conversation_turn` runs after the
    loop, not in a `finally` — so the last stored turn after a failure is the
    previous, successful one. A Retry that popped unconditionally would delete
    the wrong conversation turn, which is the same class of bug as a failed
    turn wiping its own history. Pass what you believe the last question was
    and nothing is removed unless it matches.

    Returns the removed turn (`{"user": ..., "assistant": ...}`) so a caller can
    put the text back in the composer, or None when there was nothing to remove
    — an empty session, a tail that does not pair (left alone rather than
    guessed at), or a tail that is not the turn the caller meant.
    """
    from core.store import session

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        if row is None:
            return None
        messages = list(row.messages or [])
        if len(messages) < 2:
            return None
        user, assistant = messages[-2], messages[-1]
        if user.get("role") != "user" or assistant.get("role") != "assistant":
            return None
        if expect_user is not None and user.get("content", "") != expect_user:
            return None
        # Reassigned, not mutated: SQLAlchemy does not track in-place changes to
        # a JSON column, so `row.messages.pop()` would be a write that silently
        # does nothing. Same rule as `append_turn` above.
        row.messages = messages[:-2]
        row.last_message_at = datetime.now(timezone.utc)
        return {"user": user.get("content", ""), "assistant": assistant.get("content", "")}


async def set_pending_approval(session_id: str, agent_id: str, pending: dict | None) -> None:
    """Park (or clear) the tool call this conversation is waiting on.

    `pending` carries the call itself and the turn-boundary snapshot to resume
    from. Pass None to clear it — which must happen whether the person allows
    the call or refuses it, or the next turn re-opens a question already
    answered.
    """
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        if row is None:
            if pending is None:
                return
            # The row is written on the first *completed* turn, so a
            # conversation whose very first turn stops to ask permission has
            # nothing to hang the question on yet. Without this the pause was
            # dropped and answering it returned "not waiting on an approval".
            s.add(ChatSessionDB(
                tenant_id=get_tenant(),
                session_id=session_id,
                agent_id=agent_id,
                messages=[],
                cli_session_ids={},
                pending_approval=pending,
                last_message_at=datetime.now(timezone.utc),
            ))
            return
        row.pending_approval = pending


async def get_pending_approval(session_id: str, agent_id: str) -> dict | None:
    from core.store import session

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        return (row.pending_approval or None) if row is not None else None


async def set_cli_session_id(session_id: str, agent_id: str, provider_key: str, cli_id: str) -> None:
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        if row is None:
            s.add(ChatSessionDB(
                tenant_id=get_tenant(),
                session_id=session_id,
                agent_id=agent_id,
                messages=[],
                cli_session_ids={provider_key: cli_id},
            ))
        else:
            row.cli_session_ids = {**(row.cli_session_ids or {}), provider_key: cli_id}


async def delete_one(session_id: str, agent_id: str) -> bool:
    from core.store import session
    from core.store.models import ChatSessionDB

    if not session_id:
        return False
    async with session() as s:
        result = await s.execute(
            delete(ChatSessionDB).where(
                ChatSessionDB.tenant_id == get_tenant(),
                ChatSessionDB.session_id == session_id,
                ChatSessionDB.agent_id == agent_id,
            )
        )
    return bool(result.rowcount)


async def clear() -> int:
    """Delete every session for this tenant. Returns how many were removed."""
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(ChatSessionDB)
                .where(ChatSessionDB.tenant_id == get_tenant())
            )
        ).scalar() or 0
        await s.execute(
            delete(ChatSessionDB).where(ChatSessionDB.tenant_id == get_tenant())
        )
    return int(count)
