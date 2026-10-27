"""One Synapse AI chat writes a workflow at a time (TABS2, D327; F1063).

Synapse AI has several chats now (F1061), and two of them can set out to change one workflow. Each route checked only
that the workflow had not moved since it read it (`change_apply._moved_since`, `build_save._moved_since_read`), and the
builder's writes take no lock, so two chats approved close together both wrote, the later over the earlier. Naveen,
2026-10-27: never two silent writers. The second chat is told the workflow is open in another Synapse AI chat, with a
way to go to it, and offers nothing to approve; a race at the write is refused in the same words.

A chat *holds* a workflow while its newest turn will write it: a change review waiting on its yes with a ready op on the
workflow, a build review of an update to it, a build waiting on a Connect card after its yes, or such a turn still
working. It is read from the builder's own run rows — the seam `read_turn` reads earlier turns through — so it needs no
table of its own, and a hold ends when the turn does: applied, stopped, set aside (the panel cancels a plan it sets
aside) or expired after a day (F214). A turn still classifying holds nothing, so two new chats never refuse each other.

At the card any other chat's hold refuses. At the write, under a lock on the workflow, only a hold from a turn that began
earlier does: two reviews that raced past their cards cannot then refuse each other, and the earlier one goes first.

Another person's chat is never named: its title and id stay theirs (SAI2's condition).
"""
from __future__ import annotations

import asyncio
import contextlib
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncContextManager, AsyncIterator, Callable

from core.native_builder import change_ops as C

#: A turn still `running` this long after it began is one a restart left behind: nothing will write for it.
RUNNING_FOR = timedelta(hours=2)


@dataclass(frozen=True)
class Holder:
    """The chat holding a workflow, as its newest turn shows it."""
    session_id: str
    run_id: str
    user_id: str | None
    #: What that turn was asked: the holder's own words, never shown to anyone else.
    title: str


def targets(state: dict) -> set[str]:
    """The workflows a builder turn will write, from what its checks left in its state: a change's ready ops on a
    workflow (writes and deletes, not a run or a create), and a build's update."""
    from core.native_builder.build_plan import EXISTING_KEY, _obj, checked_in as plan_checked
    from core.native_builder.change_apply import checked_in as change_checked

    found: set[str] = set()
    for item in change_checked(state).get("ops") or []:
        op = C.OPS.get(str(item.get("op") or ""))
        wid = (item.get("target") or {}).get("id")
        if item.get("status") == "ready" and op is not None and op.target == "workflow" and op.kind in (
                "write", "delete") and wid:
            found.add(str(wid))
    plan = plan_checked(state).get("plan")
    if isinstance(plan, dict) and plan.get("kind") == "update":
        existing = _obj(state.get(EXISTING_KEY)) or {}
        if wid := existing.get("id") or state.get("current_orchestration_id"):
            found.add(str(wid))
    return found


async def holds(session_id: str | None, *, run_id: str | None = None) -> dict[str, Holder]:
    """Every workflow another chat holds, by id, from one read of the builder's open turns.

    `session_id` is the asking chat's: its own turns never hold against it. With `run_id` (at the write) only a hold
    from a turn that began before that run counts.
    """
    from sqlalchemy import select

    from core.builder_access import CALLER_KEY
    from core.native_builder import NATIVE_BUILDER_ORCH_ID
    from core.store import session
    from core.store.models import OrchestrationRunDB
    from core.tenancy import get_tenant

    since = datetime.now(timezone.utc) - RUNNING_FOR
    async with session() as s:
        rows = (await s.execute(
            select(OrchestrationRunDB.run_id, OrchestrationRunDB.session_id, OrchestrationRunDB.status,
                   OrchestrationRunDB.waiting_for_human, OrchestrationRunDB.created_at, OrchestrationRunDB.shared_state)
            .where(OrchestrationRunDB.tenant_id == get_tenant(),
                   OrchestrationRunDB.orchestration_id == NATIVE_BUILDER_ORCH_ID,
                   OrchestrationRunDB.status.in_(("running", "paused")))
            .order_by(OrchestrationRunDB.created_at.asc())
        )).all()
        began = None
        if run_id:
            began = (await s.execute(select(OrchestrationRunDB.created_at)
                                     .where(OrchestrationRunDB.run_id == run_id))).scalar_one_or_none()
    began = _utc(began)
    held: dict[str, Holder] = {}
    for rid, sid, status, waiting, created_at, state in rows:
        if not sid or sid == session_id or rid == run_id:
            continue
        created_at = _utc(created_at)
        if status == "paused" and not waiting:
            continue
        if status == "running" and (created_at is None or created_at < since):
            continue
        if began is not None and (created_at is None or created_at >= began):
            continue
        state = state if isinstance(state, dict) else {}
        caller = state.get(CALLER_KEY)
        holder = Holder(session_id=sid, run_id=rid,
                        user_id=(caller.get("user_id") or None) if isinstance(caller, dict) else None,
                        title=str(state.get("user_message") or "").strip())
        for wid in targets(state):
            held.setdefault(wid, holder)  # the earliest turn first
    return held


async def held_by(orchestration_id: str, session_id: str | None, *, run_id: str | None = None) -> Holder | None:
    """Another chat holding `orchestration_id`, or None (see `holds`)."""
    if not orchestration_id:
        return None
    return (await holds(session_id, run_id=run_id)).get(orchestration_id)


def _utc(when: datetime | None) -> datetime | None:
    if when is not None and when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)  # SQLite hands back naive UTC
    return when


#: How a chat's session id becomes the id its tab and its panel know it by; None for a session that is no chat a
#: person can open. Each product's own (cloud's are `builder:<user>:<id>`): `set_chat_of` replaces it.
def _panel_chat(session_id: str) -> str | None:
    from core.tenancy import get_tenant

    prefix = f"builder:{get_tenant()}:panel:"
    return session_id[len(prefix):] if session_id.startswith(prefix) else None


_chat_of: Callable[[str], str | None] = _panel_chat


def set_chat_of(read: Callable[[str], str | None]) -> None:
    """A host's own reading of its builder session ids (cloud registers one at start-up)."""
    global _chat_of
    _chat_of = read


def refusal(holder: Holder, user_id: str | None) -> dict:
    """What the person is told about `holder`, and the chat to open when it is their own (`open_chat`, else None)."""
    if holder.user_id != (user_id or None):
        return {"say": "This workflow is being changed in another person's Synapse AI chat, so I changed nothing. Ask "
                       "me again once they are done.", "open_chat": None}
    title = holder.title if len(holder.title) <= 80 else holder.title[:79].rstrip() + "…"
    named = f' ("{title}")' if title else ""
    return {"say": f"This workflow is open in another Synapse AI chat{named}, so I changed nothing. Finish or stop it "
                   "there, then ask me again.",
            "open_chat": {"chat_id": _chat_of(holder.session_id), "title": title}}


def open_chat_event(refused: dict) -> dict | None:
    """The panel's "Open that chat" for a refusal that names the person's own chat."""
    chat = refused.get("open_chat")
    if not chat or not chat.get("chat_id"):
        return None
    return {"type": "open_chat", "chat_id": chat["chat_id"], "title": chat.get("title") or ""}


def guarded(op_name: str) -> bool:
    """Whether a change op writes a workflow: a write or a delete of one, not a run of it or a new one."""
    op = C.OPS.get(op_name)
    return op is not None and op.target == "workflow" and op.kind in ("write", "delete")


async def holds_now() -> dict[str, Holder]:
    """`holds` against the running builder turn's own chat: read once per check, however many workflows it names."""
    from core.native_builder.change_tools import _run

    _, run = _run()
    return await holds(run.session_id if run is not None else None)


def asked(held: dict[str, Holder], orchestration_id: str, events: list[dict]) -> str | None:
    """At the card: the words that leave out a write to a workflow another chat holds (`held`, from `holds_now`),
    with the person's "Open that chat" added to `events`; None when it is free."""
    from core.native_builder.change_tools import _caller

    holder = held.get(orchestration_id) if orchestration_id else None
    if holder is None:
        return None
    said = refusal(holder, _caller().user_id)
    if (event := open_chat_event(said)) is not None:
        events.append(event)
    return said["say"]


# ── the write ─────────────────────────────────────────────────────────────────────────────────────────────────────

_LOCKS: "weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock]" = weakref.WeakValueDictionary()


@contextlib.asynccontextmanager
async def _in_process(orchestration_id: str) -> AsyncIterator[None]:
    from core.tenancy import get_tenant

    key = (get_tenant(), orchestration_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    async with lock:
        yield


#: One writer at a time per workflow. Open source runs one process, so a lock in it is enough; cloud's API and workers
#: are many, and it registers a database lock held until the write is done (`set_write_lock`).
_lock: Callable[[str], AsyncContextManager[Any]] = _in_process


def set_write_lock(lock: Callable[[str], AsyncContextManager[Any]]) -> None:
    global _lock
    _lock = lock


def writing_op(op_name: str, orchestration_id: str, *, run_id: str, session_id: str | None) -> AsyncContextManager[
        Holder | None]:
    """`writing` for a change op that writes a workflow; for any other op, nothing to hold."""
    if not guarded(op_name) or not orchestration_id:
        return contextlib.nullcontext(None)
    return writing(orchestration_id, run_id=run_id, session_id=session_id)


@contextlib.asynccontextmanager
async def writing(orchestration_id: str, *, run_id: str, session_id: str | None) -> AsyncIterator[Holder | None]:
    """Hold the workflow's write lock and say who, if anyone, holds it from an earlier turn: the write goes ahead
    only when this yields None."""
    async with _lock(orchestration_id):
        yield await held_by(orchestration_id, session_id, run_id=run_id)
