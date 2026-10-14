"""
Chat sessions in the store.

One table shared by the standalone server and the scale worker. It replaces two
things at once: the per-session JSON files under `DATA_DIR/chat_sessions`, and
the worker shim that copied history *out* of the database onto the winning
worker's local disk so that `core/session.py` could find it there.

Keyed `(tenant_id, session_id, agent_id)`. See `ChatSessionDB` for why the
agent belongs in the key rather than beside it.

**One writer at a time per conversation (F475).** Every write reads the row,
changes it in Python and writes it back, so two at once kept only the later
one's: a phone call's turn was lost to the fleet resuming its approval (VOX1e),
and F247's "two turns cannot both settle the same question" held for neither.
A write now holds the conversation from its read to its commit (`_writing`):
the row's lock where the database has one (`FOR UPDATE`, Postgres, across
processes), and a lock in this process, which SQLite needs (it has no row locks,
and its driver begins a transaction only at the first write, after the read).
Self-hosted SQLite is one process; scale mode runs on Postgres.
"""
from __future__ import annotations

import asyncio
import contextlib
import weakref
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

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


async def _row(s, session_id: str, agent_id: str, *, for_update: bool = False):
    from core.store.models import ChatSessionDB

    stmt = select(ChatSessionDB).where(
        ChatSessionDB.tenant_id == get_tenant(),
        ChatSessionDB.session_id == session_id,
        ChatSessionDB.agent_id == agent_id,
    )
    if for_update:
        stmt = stmt.with_for_update()  # nothing on SQLite, which `_writing`'s own lock covers
    return (await s.execute(stmt)).scalar_one_or_none()


#: The conversations being written in this process, each with its lock (F475). Weak: a lock lives while a writer
#: holds or waits for it, so the map never outgrows the writes in flight. Keyed by event loop too: a lock belongs to
#: the loop it was first awaited in.
_WRITERS: "weakref.WeakValueDictionary[tuple, asyncio.Lock]" = weakref.WeakValueDictionary()


@contextlib.asynccontextmanager
async def _writing(session_id: str, agent_id: str) -> AsyncIterator:
    """A store session that holds this conversation from its read to its commit (F475): read the row in it with
    `_row(…, for_update=True)`. Not reentrant: a write never calls another."""
    from core.store import session

    key = (id(asyncio.get_running_loop()), get_tenant(), session_id, agent_id)
    lock = _WRITERS.get(key)
    if lock is None:
        lock = _WRITERS[key] = asyncio.Lock()
    async with lock:
        async with session() as s:
            yield s


async def _first_write_wins(write) -> None:
    """Run `write()`, once more if it lost a race to create the row (two first turns from two processes: the
    second's insert meets the first's key, and its retry then finds the row and appends to it)."""
    try:
        await write()
    except IntegrityError:
        await write()


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
    from core.store.models import ChatSessionDB

    async def write() -> None:
        async with _writing(session_id, agent_id) as s:
            row = await _row(s, session_id, agent_id, for_update=True)
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
    await _first_write_wins(write)


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
    async with _writing(session_id, agent_id) as s:
        row = await _row(s, session_id, agent_id, for_update=True)
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


def cut_at_heard(answer: str, heard: str) -> str:
    """`answer` up to the end of the clause the caller last heard (see `cut_last_answer`)."""
    heard = (heard or "").strip()
    if not heard:
        return answer
    at = answer.find(heard)
    if at < 0:
        at = answer.lower().find(heard.lower())
    return answer if at < 0 else answer[:at + len(heard)]


async def cut_last_answer(session_id: str, agent_id: str, heard: str) -> bool:
    """The last answer as the person actually heard it (a phone call's barge-in, VOX1a V14).

    The caller talked over the agent, so the rest was never said; the next turn
    must not believe it was. `heard` is what the provider says played last —
    ConversationRelay's `utteranceUntilInterrupt` is only the clause that was
    playing (measured, VOX1a P1), so the answer is cut at the end of that clause
    and everything before it is kept. A clause not found in the answer (the
    answer was reworded for speech), or none at all, leaves the words as they
    are; either way the answer is marked interrupted. Only the content changes,
    the turn's tools and calls stay. False when the tail is not an answer.
    """
    async with _writing(session_id, agent_id) as s:
        row = await _row(s, session_id, agent_id, for_update=True)
        if row is None:
            return False
        messages = list(row.messages or [])
        if not messages or messages[-1].get("role") != "assistant":
            return False
        last = dict(messages[-1])
        last["content"] = cut_at_heard(str(last.get("content") or ""), heard)
        last["interrupted"] = True
        # Reassigned, not mutated (see append_turn).
        row.messages = messages[:-1] + [last]
        return True


async def mark_last_answer(session_id: str, agent_id: str, *, question: str, **fields) -> bool:
    """Note `fields` on the last answer, when it answers `question` (a phone call's filler, VOX1c).

    The words are not touched. `question` is the guard, as `pop_last_turn`'s
    `expect_user` is: a turn that failed saved nothing, so the last answer then
    belongs to an earlier question, which must not be marked. False when the
    tail is not that question's answer.
    """
    async with _writing(session_id, agent_id) as s:
        row = await _row(s, session_id, agent_id, for_update=True)
        if row is None:
            return False
        messages = list(row.messages or [])
        if len(messages) < 2:
            return False
        user, last = messages[-2], messages[-1]
        if user.get("role") != "user" or last.get("role") != "assistant" or user.get("content", "") != question:
            return False
        # Reassigned, not mutated (see append_turn).
        row.messages = messages[:-1] + [{**last, **fields}]
        return True


async def set_pending_approval(session_id: str, agent_id: str, pending: dict | None) -> None:
    """Park (or clear) the tool call this conversation is waiting on.

    `pending` carries the call itself and the turn-boundary snapshot to resume
    from. Pass None to clear it — which must happen whether the person allows
    the call or refuses it, or the next turn re-opens a question already
    answered.
    """
    from core.store.models import ChatSessionDB

    async def write() -> None:
        async with _writing(session_id, agent_id) as s:
            row = await _row(s, session_id, agent_id, for_update=True)
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
    await _first_write_wins(write)


async def get_pending_approval(session_id: str, agent_id: str) -> dict | None:
    from core.store import session

    async with session() as s:
        row = await _row(s, session_id, agent_id)
        return (row.pending_approval or None) if row is not None else None


async def take_pending_approval(session_id: str, agent_id: str) -> dict | None:
    """The parked call, cleared in the same write — or None when nothing waits.

    One write holding the conversation (`_writing`, F475), so two turns
    starting at once cannot both settle the same question (F247): the second
    finds it gone.
    """
    async with _writing(session_id, agent_id) as s:
        row = await _row(s, session_id, agent_id, for_update=True)
        if row is None or not row.pending_approval:
            return None
        pending = dict(row.pending_approval)
        row.pending_approval = None
        return pending


async def parked_approvals(session_prefix: str) -> list[tuple[str, str, dict]]:
    """Every parked call in conversations whose id starts with `session_prefix`,
    as `(session_id, agent_id, pending)` — the scheduler's expiry sweep (F268)."""
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        rows = (
            await s.execute(
                select(ChatSessionDB.session_id, ChatSessionDB.agent_id, ChatSessionDB.pending_approval).where(
                    ChatSessionDB.tenant_id == get_tenant(),
                    ChatSessionDB.session_id.startswith(session_prefix, autoescape=True),
                )
            )
        ).all()
    return [(session_id, agent_id, pending) for session_id, agent_id, pending in rows if pending]


async def pending_approvals(session_id: str) -> list[tuple[str, dict]]:
    """Every agent in this conversation with a tool call parked, as `(agent_id, pending)`.

    A session id names one conversation per agent (see `ChatSessionDB`), so an
    answer that does not name the agent finds it here (F211).
    """
    from core.store import session
    from core.store.models import ChatSessionDB

    async with session() as s:
        rows = (
            await s.execute(
                select(ChatSessionDB.agent_id, ChatSessionDB.pending_approval).where(
                    ChatSessionDB.tenant_id == get_tenant(),
                    ChatSessionDB.session_id == session_id,
                )
            )
        ).all()
    return [(agent_id, pending) for agent_id, pending in rows if pending]


async def set_cli_session_id(session_id: str, agent_id: str, provider_key: str, cli_id: str) -> None:
    from core.store.models import ChatSessionDB

    async def write() -> None:
        async with _writing(session_id, agent_id) as s:
            row = await _row(s, session_id, agent_id, for_update=True)
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
    await _first_write_wins(write)


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
