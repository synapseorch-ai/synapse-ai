"""A tool call an API turn stopped on, asked in the bell (OAI1, the shape of F268).

A caller of the OpenAI-compatible endpoint (a program, a voice platform) cannot
press Approve. So a call whose policy is `confirm` parks the turn where it
stands, as a scheduled run's does: the call and the turn's snapshot are kept in
the turn's own conversation, the bell asks a person in the person tone, and the
caller is told someone will follow up. Approve or Refuse in the bell resumes
the turn in the background from its snapshot, with the same conversation the
request carried. Nobody answering within the hours given expires it unrun.

Two doors park here, each in its own conversations:

* ``openai``: the OpenAI-compatible endpoint's (`oai_…`), never listed in Chat;
  its row is removed once its question is settled.
* ``voice``: a phone call's (`voice_…`, VOX1a, V16). The caller is a visitor, so
  the resumed turn is one too. The conversation is the call's transcript (V27),
  so it is kept, and the resumed turn is saved into it. Answered while the
  caller is still on the line, the call itself carries the turn on and says it
  (F474); only an answer it cannot say is resumed here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger("api_approvals")

#: The conversations the OpenAI door parks in.
SESSION_PREFIX = "oai_"
#: Why an expired one was not run.
EXPIRED = "nobody answered in time"


@dataclass(frozen=True)
class Door:
    """Where a parked turn came from, and what that changes."""
    name: str
    prefix: str
    #: Where the bell's Approve / Refuse post (an app route, behind the app's own auth).
    answer_path: str
    #: The conversation is the caller's record (a call's transcript), not scratch.
    keeps_conversation: bool
    #: The agent log's source for the resumed turn.
    log_source: str


DOORS = {
    "openai": Door("openai", SESSION_PREFIX, "/api/openai/approvals/{session_id}", False, "openai"),
    "voice": Door("voice", "voice_", "/api/messaging/twilio_voice/approvals/{session_id}", True, "voice"),
}


def door_for(session_id: str) -> Door | None:
    return next((d for d in DOORS.values() if session_id.startswith(d.prefix)), None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def answer_url(session_id: str) -> str:
    """Where the bell's Approve / Refuse post (an app route, behind the app's own auth)."""
    door = door_for(session_id) or DOORS["openai"]
    return door.answer_path.format(session_id=session_id)


async def park(*, session_id: str, agent: dict, event: dict, message: str, progress: dict | None,
               resume: dict, hours: int, door: str = "openai", caller: dict | None = None) -> str:
    """Keep the call and the turn's snapshot, ask in the bell; returns the approval id.

    `caller` (the voice door's) is who asked: ``{"number", "visitor"}``, the
    visitor scope as `VisitorScope.to_dict()`, so the resumed turn keeps it.
    """
    from core import tool_policy
    from core.chat_approval import parked_payload, to_ring
    from core.notifications import hub
    from core.session import _clip_args
    from core.store import sessions as _sessions

    name = agent.get("name") or agent.get("id") or "An agent"
    expires_at = (_now() + timedelta(hours=hours)).isoformat()
    pending = parked_payload(event, message=message, progress=progress)
    pending["api_turn"] = {"door": door, "agent_name": name, "expires_at": expires_at,
                           "history": resume.get("history") or [],
                           "caller_system": resume.get("caller_system")}
    data = {"approval_id": event.get("approval_id"), "tool_name": event.get("tool_name"),
            "tool_args": _clip_args(event.get("tool_args") or {}), "category": event.get("category"),
            "expires_at": expires_at, "session_id": session_id, "agent_id": agent["id"],
            "answer_url": answer_url(session_id)}
    if door == "voice":
        number = (caller or {}).get("number") or "an unknown number"
        pending["api_turn"]["caller"] = number
        pending["api_turn"]["visitor"] = (caller or {}).get("visitor")
        body = (f"A caller on {number} asked {name} for something that needs {event.get('tool_name')} — "
                f"{tool_policy.explain(event.get('category'))}. They were told someone will follow up.")
        data.update({"door": "voice", "caller": number})
    else:
        body = (f"An app talking to {name} asked something that needs {event.get('tool_name')} — "
                f"{tool_policy.explain(event.get('category'))}.")
    await _sessions.set_pending_approval(session_id, agent["id"], pending)
    if not pending.get("questions"):
        hub.publish("tool_approval", f"{name} needs approval", body=body, data=data)
        return event.get("approval_id") or ""
    # A team's members, each its own item (TEAM1c); one an earlier pause of this turn asked keeps its item.
    for q in to_ring(pending):
        who = q.get("member_name") or name
        hub.publish("tool_approval", f"{name}: {who} needs approval",
                    body=(f"An app talking to {name} asked something {who} needs {q.get('tool_name')} for — "
                          f"{tool_policy.explain(q.get('category'))}."),
                    data={**data, "approval_id": q.get("approval_id"), "question_id": q.get("question_id"),
                          "member_name": q.get("member_name"), "tool_name": q.get("tool_name"),
                          "tool_args": _clip_args(q.get("tool_args") or {}), "category": q.get("category")})
    return event.get("approval_id") or ""


def _resolve(pending: dict, how: str, question_ids: list[str] | None = None) -> None:
    """Resolve the bell items of `pending`'s questions (`question_ids` only, when given)."""
    from core.chat_approval import questions_of
    from core.notifications import hub

    for q in questions_of(pending):
        if question_ids is None or q.get("question_id") in question_ids:
            hub.resolve_approval(q.get("approval_id"), how, question_id=q.get("question_id"))


async def _settle(session_id: str, agent_id: str, pending: dict, how: str,
                  verdicts: list[dict] | None = None) -> None:
    from core.store import sessions as _sessions

    if verdicts is None:
        _resolve(pending, how)
    else:
        for verdict in verdicts:
            _resolve(pending, "allowed" if verdict.get("approve") else "refused", [verdict.get("question_id")])
    door = door_for(session_id)
    if door is not None and door.keeps_conversation:
        return
    try:
        await _sessions.delete_one(session_id, agent_id)
    except Exception as exc:  # noqa: BLE001 — a leftover row is only clutter, never listed
        log.warning("could not remove settled API conversation %s: %s", session_id, exc)


async def answer(session_id: str, *, approve: bool, reason: str = "", approval_id: str | None = None,
                 question_id: str | None = None):
    """Answer the call `session_id` waits on and carry its turn on, in the background.

    Returns the chat turn driving it, or None when a phone call still on the
    line took the answer to say itself (F474), or a team at work here took it
    (TEAM1c). Raises `LookupError` when that conversation is not waiting on that
    call (or it expired), and `chat_turns.TurnAlreadyRunning` while another
    answer is being acted on.

    A team's question is named by `question_id` (TEAM1c, Q6): answered while its
    turn runs again after an earlier answer, it reaches that member now, or is
    held for the turn and taken up with it when it pauses.
    """
    from core import chat_turns, team
    from core.chat_approval import find_question, take_answers
    from core.store import sessions as _sessions

    door = door_for(session_id)
    if door is None:
        raise LookupError("This is not an API turn's question.")
    verdict = {"question_id": question_id, "approve": approve, "reason": reason}
    if question_id:
        working = team.find(session_id)
        if working is not None and working.answer(question_id, approve=approve, reason=reason):
            _resolve_where_asked(session_id, question_id, approve)
            return None
    waiting = [(a, p) for a, p in await _sessions.pending_approvals(session_id) if p.get("api_turn")]
    if approval_id is not None or question_id is not None:
        waiting = [(a, p) for a, p in waiting
                   if find_question(p, question=question_id, approval_id=approval_id) is not None]
    if len(waiting) != 1:
        if question_id and chat_turns.busy(session_id) is not None:
            return _hold(session_id, verdict)
        raise LookupError("Nothing is waiting on that approval any more.")
    agent_id = waiting[0][0]
    try:
        with chat_turns.claim(session_id, agent_key=agent_id) as turn:
            pending = await _sessions.take_pending_approval(session_id, agent_id)
            asked = find_question(pending, question=question_id, approval_id=approval_id) if pending else None
            if asked is None:
                if pending:
                    await _sessions.set_pending_approval(session_id, agent_id, pending)
                raise LookupError("Nothing is waiting on that approval any more.")
            if _expired(pending):
                await _settle(session_id, agent_id, pending, "expired")
                raise LookupError("That approval expired: " + EXPIRED + ".")
            if door.name == "voice" and _to_the_live_call(session_id, pending, approve=approve, reason=reason):
                await _settle(session_id, agent_id, pending, "allowed" if approve else "refused")
                return None
            others = [q["question_id"] for q in pending.get("questions") or []
                      if q["question_id"] != asked["question_id"]]
            verdicts = [{**verdict, "question_id": asked["question_id"]},
                        *take_answers(session_id, agent_id, others)]
            turn.message = pending.get("message") or ""
            # A team's answers go with it (TEAM1c); one question's resume is called as it always was.
            more = {"verdicts": verdicts} if pending.get("questions") else {}
            turn.start(_resumed(session_id, agent_id, pending, approve=approve, reason=reason, **more))
    except chat_turns.TurnAlreadyRunning:
        if not question_id:
            raise
        return _hold(session_id, verdict)
    return turn


#: Answers held for a team's turn that was starting or pausing (TEAM1c), each awaiting that turn's end.
_HELD: set = set()


def _hold(session_id: str, verdict: dict):
    """Hold a team question's answer for the turn at work here; once that turn has ended, still held, it
    answers the turn's paused question. None: nothing more to drive now."""
    import asyncio

    from core import chat_turns
    from core.chat_approval import post_answer, take_answers

    agent_key = next(iter(_agents_working(session_id)), "default")
    if post_answer(session_id, agent_key, verdict) == "applied":
        _resolve_where_asked(session_id, verdict["question_id"], bool(verdict.get("approve")))
        return None

    async def when_free():
        try:
            await asyncio.wait_for(chat_turns.until_free(session_id), timeout=3600)
            if take_answers(session_id, agent_key, [verdict["question_id"]]):
                await answer(session_id, approve=bool(verdict.get("approve")),
                             reason=str(verdict.get("reason") or ""), question_id=verdict["question_id"])
        except Exception as exc:  # noqa: BLE001 — nothing asks it any more, or the resume could not start
            log.info("held answer for %s not used: %s", session_id, exc)

    task = asyncio.create_task(when_free(), name=f"held-api-answer-{session_id}")
    _HELD.add(task)
    task.add_done_callback(_HELD.discard)
    return None


def _agents_working(session_id: str) -> list[str]:
    from core import team
    working = team.find(session_id)
    return [working.agent_key] if working is not None else []


def _resolve_where_asked(session_id: str, question_id: str, approved: bool) -> None:
    from core.notifications import hub
    hub.resolve_approval(None, "allowed" if approved else "refused", question_id=question_id)


def voice_answer(pending: dict, *, approve: bool, reason: str) -> dict:
    """What a live call is handed to carry a parked turn on (F474): `VoiceCall.hand_answer`'s shape, with the
    parked payload kept for the host to resume it behind if the call cannot say it."""
    from core.chat_approval import resume_state_for

    return {"approval_id": pending.get("approval_id"), "approved": approve, "reason": reason,
            "tool_name": pending.get("tool_name"), "message": pending.get("message") or "",
            "resume_state": resume_state_for(pending, approve=approve, reason=reason), "pending": pending}


def _to_the_live_call(session_id: str, pending: dict, *, approve: bool, reason: str) -> bool:
    """A caller still on the line hears the answer on the call: True when that call took it."""
    from core.messaging.voice import call as voice_call

    live = voice_call.live_for_session(session_id)
    return live is not None and live.hand_answer(voice_answer(pending, approve=approve, reason=reason))


def resume_behind(session_id: str, agent_id: str, pending: dict, *, approve: bool, reason: str):
    """Carry an answered turn on in the background: an answer a live call took but could not say (F474).
    Raises `chat_turns.TurnAlreadyRunning` while another turn is acted on in that conversation."""
    from core import chat_turns

    with chat_turns.claim(session_id, agent_key=agent_id) as turn:
        turn.message = pending.get("message") or ""
        turn.start(_resumed(session_id, agent_id, pending, approve=approve, reason=reason))
    return turn


def _expired(pending: dict) -> bool:
    at = (pending.get("api_turn") or {}).get("expires_at")
    try:
        return bool(at) and datetime.fromisoformat(at) <= _now()
    except ValueError:
        return False


async def _resumed(session_id: str, agent_id: str, pending: dict, *, approve: bool, reason: str,
                   verdicts: list[dict] | None = None):
    """The turn picked up from its snapshot with the person's answer; what it ends with goes to the bell."""
    import contextlib

    import core.server as _server
    from core import visitor as _visitor
    from core.chat_approval import resume_state_for, settle_resumed
    from core.models import ChatRequest
    from core.notifications import hub
    from core.react_engine import run_react_loop
    from core.store import sessions as _sessions

    meta = pending.get("api_turn") or {}
    door = DOORS.get(meta.get("door") or "openai") or DOORS["openai"]
    name = meta.get("agent_name") or agent_id
    await _settle(session_id, agent_id, pending, "allowed" if approve else "refused", verdicts=verdicts)
    if pending.get("questions"):
        # A team's other questions stay asked while the turn runs again (TEAM1c); at its end, those it no
        # longer asks are settled — answered ones keep their answer.
        async def _rest():
            again = await _sessions.get_pending_approval(session_id, agent_id)
            settle_resumed(pending, again, lambda q: hub.resolve_approval(
                q.get("approval_id"), "expired", question_id=q.get("question_id")))
    else:
        _rest = None
    request = ChatRequest(message=pending.get("message") or "", session_id=session_id, agent_id=agent_id)
    latest_progress: dict | None = None
    answer_text = ""
    # A caller who was a visitor is still one when the turn carries on (V16).
    scope = _visitor.VisitorScope.from_dict(meta["visitor"]) if meta.get("visitor") else None
    done = {"session_id": session_id, "agent_id": agent_id}
    if door.name == "voice":
        done.update({"door": "voice", "caller": meta.get("caller")})
    try:
        with (_visitor.scope(scope) if scope is not None else contextlib.nullcontext()):
            async for event in run_react_loop(
                request, _server, progress_events=True,
                resume_state=resume_state_for(pending, approve=approve, reason=reason, verdicts=verdicts),
                history_override=None if door.keeps_conversation else (meta.get("history") or []),
                caller_system=meta.get("caller_system"),
                save_turn=door.keeps_conversation, log_source=door.log_source,
            ):
                kind = event.get("type")
                if kind == "_step_progress":
                    latest_progress = {k: v for k, v in event.items() if k != "type"}
                    continue
                if kind == "human_input_required" and event.get("kind") == "tool_approval":
                    # Another call needs a person: asked again, the same way.
                    await park(session_id=session_id, agent={"id": agent_id, "name": name}, event=event,
                               message=request.message, progress=latest_progress,
                               resume={"history": meta.get("history"), "caller_system": meta.get("caller_system")},
                               hours=24, door=door.name,
                               caller={"number": meta.get("caller"), "visitor": meta.get("visitor")})
                    if _rest is not None:
                        await _rest()
                    return
                if kind == "final" and not event.get("orch_step_id"):
                    answer_text = event.get("response", "")
                yield event
    except Exception as exc:  # noqa: BLE001 — the person who answered is told, the log keeps it
        log.error("resumed API turn %s failed: %s", session_id, exc)
        hub.publish("run_failed", f"{name} could not finish",
                    body="It failed after the approval was answered. Its log has the details.",
                    data=done)
        if _rest is not None:
            await _rest()
        return
    if _rest is not None:
        await _rest()
    if door.name == "voice":
        title = f"{name} finished after your answer — call {meta.get('caller') or 'the caller'} back"
    else:
        title = f"{name} finished after your answer"
    hub.publish("run_completed", title, body=answer_text[:500], data=done)


async def expire_parked() -> None:
    """Every API turn's call nobody answered in time expires unrun (the scheduler's tick runs this)."""
    from core.store import sessions as _sessions

    for door in DOORS.values():
        for session_id, agent_id, pending in await _sessions.parked_approvals(door.prefix):
            if pending.get("api_turn") and _expired(pending):
                taken = await _sessions.take_pending_approval(session_id, agent_id)
                if taken:
                    await _settle(session_id, agent_id, taken, "expired")
