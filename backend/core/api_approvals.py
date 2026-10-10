"""A tool call an API turn stopped on, asked in the bell (OAI1, the shape of F268).

A caller of the OpenAI-compatible endpoint (a program, a voice platform) cannot
press Approve. So a call whose policy is `confirm` parks the turn where it
stands, as a scheduled run's does: the call and the turn's snapshot are kept in
the turn's own conversation, the bell asks a person in the person tone, and the
caller is told someone will follow up. Approve or Refuse in the bell resumes
the turn in the background from its snapshot, with the same conversation the
request carried. Nobody answering within the hours given expires it unrun.

The conversation is the API's own (`oai_…`), never listed in Chat; its row is
removed once its question is settled. A voice call parks the same way (V16).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("api_approvals")

#: The conversations this module parks in.
SESSION_PREFIX = "oai_"
#: Why an expired one was not run.
EXPIRED = "nobody answered in time"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def answer_url(session_id: str) -> str:
    """Where the bell's Approve / Refuse post (an app route, behind the app's own auth)."""
    return f"/api/openai/approvals/{session_id}"


async def park(*, session_id: str, agent: dict, event: dict, message: str, progress: dict | None,
               resume: dict, hours: int) -> str:
    """Keep the call and the turn's snapshot, ask in the bell; returns the approval id."""
    from core import tool_policy
    from core.chat_approval import parked_payload
    from core.notifications import hub
    from core.session import _clip_args
    from core.store import sessions as _sessions

    name = agent.get("name") or agent.get("id") or "An agent"
    expires_at = (_now() + timedelta(hours=hours)).isoformat()
    pending = parked_payload(event, message=message, progress=progress)
    pending["api_turn"] = {"door": "openai", "agent_name": name, "expires_at": expires_at,
                           "history": resume.get("history") or [],
                           "caller_system": resume.get("caller_system")}
    await _sessions.set_pending_approval(session_id, agent["id"], pending)
    hub.publish(
        "tool_approval", f"{name} needs approval",
        body=(f"An app talking to {name} asked something that needs {event.get('tool_name')} — "
              f"{tool_policy.explain(event.get('category'))}."),
        data={"approval_id": event.get("approval_id"), "tool_name": event.get("tool_name"),
              "tool_args": _clip_args(event.get("tool_args") or {}), "category": event.get("category"),
              "expires_at": expires_at, "session_id": session_id, "agent_id": agent["id"],
              "answer_url": answer_url(session_id)},
    )
    return event.get("approval_id") or ""


async def _settle(session_id: str, agent_id: str, pending: dict, how: str) -> None:
    from core.notifications import hub
    from core.store import sessions as _sessions

    hub.resolve_approval(pending.get("approval_id"), how)
    try:
        await _sessions.delete_one(session_id, agent_id)
    except Exception as exc:  # noqa: BLE001 — a leftover row is only clutter, never listed
        log.warning("could not remove settled API conversation %s: %s", session_id, exc)


async def answer(session_id: str, *, approve: bool, reason: str = "", approval_id: str | None = None):
    """Answer the call `session_id` waits on and carry its turn on, in the background.

    Returns the chat turn driving it. Raises `LookupError` when that
    conversation is not waiting on that call (or it expired), and
    `chat_turns.TurnAlreadyRunning` while another answer is being acted on.
    """
    from core import chat_turns
    from core.store import sessions as _sessions

    if not session_id.startswith(SESSION_PREFIX):
        raise LookupError("This is not an API turn's question.")
    waiting = [(a, p) for a, p in await _sessions.pending_approvals(session_id) if p.get("api_turn")]
    if approval_id is not None:
        waiting = [(a, p) for a, p in waiting if p.get("approval_id") == approval_id]
    if len(waiting) != 1:
        raise LookupError("Nothing is waiting on that approval any more.")
    agent_id = waiting[0][0]
    with chat_turns.claim(session_id, agent_key=agent_id) as turn:
        pending = await _sessions.take_pending_approval(session_id, agent_id)
        if not pending or (approval_id is not None and pending.get("approval_id") != approval_id):
            if pending:
                await _sessions.set_pending_approval(session_id, agent_id, pending)
            raise LookupError("Nothing is waiting on that approval any more.")
        if _expired(pending):
            await _settle(session_id, agent_id, pending, "expired")
            raise LookupError("That approval expired: " + EXPIRED + ".")
        turn.message = pending.get("message") or ""
        turn.start(_resumed(session_id, agent_id, pending, approve=approve, reason=reason))
    return turn


def _expired(pending: dict) -> bool:
    at = (pending.get("api_turn") or {}).get("expires_at")
    try:
        return bool(at) and datetime.fromisoformat(at) <= _now()
    except ValueError:
        return False


async def _resumed(session_id: str, agent_id: str, pending: dict, *, approve: bool, reason: str):
    """The turn picked up from its snapshot with the person's answer; what it ends with goes to the bell."""
    import core.server as _server
    from core.chat_approval import resume_state_for
    from core.models import ChatRequest
    from core.notifications import hub
    from core.react_engine import run_react_loop

    meta = pending.get("api_turn") or {}
    name = meta.get("agent_name") or agent_id
    await _settle(session_id, agent_id, pending, "allowed" if approve else "refused")
    request = ChatRequest(message=pending.get("message") or "", session_id=session_id, agent_id=agent_id)
    latest_progress: dict | None = None
    answer_text = ""
    try:
        async for event in run_react_loop(
            request, _server, progress_events=True,
            resume_state=resume_state_for(pending, approve=approve, reason=reason),
            history_override=meta.get("history") or [], caller_system=meta.get("caller_system"),
            save_turn=False, log_source="openai",
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
                           hours=24)
                return
            if kind == "final" and not event.get("orch_step_id"):
                answer_text = event.get("response", "")
            yield event
    except Exception as exc:  # noqa: BLE001 — the person who answered is told, the log keeps it
        log.error("resumed API turn %s failed: %s", session_id, exc)
        hub.publish("run_failed", f"{name} could not finish",
                    body="It failed after the approval was answered. Its log has the details.",
                    data={"session_id": session_id, "agent_id": agent_id})
        return
    hub.publish("run_completed", f"{name} finished after your answer", body=answer_text[:500],
                data={"session_id": session_id, "agent_id": agent_id})


async def expire_parked() -> None:
    """Every API turn's call nobody answered in time expires unrun (the scheduler's tick runs this)."""
    from core.store import sessions as _sessions

    for session_id, agent_id, pending in await _sessions.parked_approvals(SESSION_PREFIX):
        if pending.get("api_turn") and _expired(pending):
            taken = await _sessions.take_pending_approval(session_id, agent_id)
            if taken:
                await _settle(session_id, agent_id, taken, "expired")
