"""What a paused chat turn stores, and what a resumed one is given.

A tool call that stops to ask permission is the one pause a chat turn can hit
and not come back from on its own: an orchestration run resumes from its own
row, and a chat turn has no run. The way back is to park the call together with
the loop's last turn-boundary snapshot, and to hand that snapshot back when
somebody answers.

Both halves live here because there are **two** callers of each, and they must
agree byte for byte or a turn resumes into a state the loop does not recognise:

* parking happens in `core/routes/chat.py` for a turn running in the API
  process, and in `core/scale/worker.py` for one running on a worker;
* the answer is turned into a resume state by `core/routes/chat.py`, and by the
  commercial control plane, which reaches this module the same way it reaches
  `core.skills`.

Those functions touch neither the store nor the network — they are shape, so
that a test can assert the shape without a database. The one that writes is
`settle_moved_past`: a new message in a conversation that is waiting settles
the question first (F247), at every door that starts a turn — the app's chat,
v1, the fleet's chat job and a schedule's next run.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

#: Set where a turn runs with nobody to answer an approval card: a messaging
#: channel's agent turn parks nothing, so a pause there ends the turn in silence.
_nobody_to_ask: ContextVar[bool] = ContextVar("synapse_nobody_to_ask", default=False)


def approvals_answerable() -> bool:
    """Whether a pause for approval in this turn can be answered by anyone."""
    return not _nobody_to_ask.get()


@contextmanager
def nobody_to_ask() -> Iterator[None]:
    """Run what starts inside this block where no approval card can be answered."""
    token = _nobody_to_ask.set(True)
    try:
        yield
    finally:
        _nobody_to_ask.reset(token)


def parked_payload(event: dict, *, message: str, progress: dict | None) -> dict[str, Any]:
    """What to store when the loop yields `human_input_required`/`tool_approval`.

    `message` is the question that started the turn: a resume rebuilds the
    conversation around it, because the loop takes a request and not a
    transcript. `progress` is the last `_step_progress` the loop emitted —
    never forwarded to a client, because it is engine state and not an event.
    """
    payload = {
        "tool_name": event.get("tool_name"),
        "tool_args": event.get("tool_args") or {},
        "approval_id": event.get("approval_id"),
        "category": event.get("category"),
        "message": message or "",
        "progress": progress or {},
    }
    if event.get("run_id"):
        # A workflow's call, asked in a chat with its agent: the answer resumes
        # that run (F290). Re-running the chat turn started the workflow again.
        payload["run_id"] = event["run_id"]
    return payload


def approval_prompt(tool_name: str | None, category: str | None) -> str:
    """The question the approval card asks — live, and again on a reopened page.

    `explain()` returns an independent clause beginning with "it", so it is
    joined with a dash rather than spliced after "which": "…run **scrape_url**,
    which it reaches systems outside this workspace" is what a live approval
    card once said.
    """
    from core import tool_policy
    return f"The agent wants to run **{tool_name}** — {tool_policy.explain(category)}. Allow it?"


def public_view(pending: dict | None) -> dict[str, Any] | None:
    """A parked call as a page may show it: the card, never the snapshot.

    `progress` is engine state — the context text the loop had built, the
    tools it had used — and a reopened page has no use for it.
    """
    if not pending:
        return None
    return {
        "kind": "tool_approval",
        "tool_name": pending.get("tool_name"),
        "tool_args": pending.get("tool_args") or {},
        "category": pending.get("category"),
        "approval_id": pending.get("approval_id"),
        "message": pending.get("message") or "",
        "prompt": approval_prompt(pending.get("tool_name"), pending.get("category")),
    }


def resume_state_for(pending: dict, *, approve: bool, reason: str = "") -> dict[str, Any]:
    """Turn a parked call and a person's answer into a state the loop resumes from.

    The turn is picked up from its snapshot rather than started again, which is
    the whole reason the snapshot is kept: starting again would re-run every
    tool that already succeeded before the pause, and those are the calls with
    side effects.

    The verdict is recorded against `approval_id`, which is derived from the
    tool *and its arguments* — so allowing `ls` does not also allow `rm`.
    """
    progress = dict(pending.get("progress") or {})
    delegated = progress.pop("delegate_pending", None)
    if delegated:
        return _delegated_resume_state(progress, delegated, approve=approve, reason=reason)
    approval_id = pending.get("approval_id")
    bucket = "approved_calls" if approve else "denied_calls"

    state: dict[str, Any] = {
        **progress,
        bucket: sorted(set(progress.get(bucket) or []) | {approval_id}),
        "pending_call": {
            "approval_id": approval_id,
            "tool": pending.get("tool_name"),
            "arguments": pending.get("tool_args") or {},
            "approved": approve,
            "reason": reason,
        },
    }
    if not approve and reason:
        state["denied_reasons"] = {**(progress.get("denied_reasons") or {}), approval_id: reason}
    if pending.get("run_id"):
        state["run_id"] = pending["run_id"]
    return state


def _delegated_resume_state(progress: dict, delegated: dict, *, approve: bool, reason: str) -> dict[str, Any]:
    """The lead's resume state when the question was its delegate's (F286).

    The lead replays its own `delegate_to_agent` call — approved, since it had
    already passed the lead's gate this turn — and hands the delegate its own
    resume state: the delegate's snapshot and the person's answer on the
    delegate's call. So the call runs, or is refused, under the delegate's tools
    and policy. The lead's state approves the lead's call alone: approving the
    delegate's call here would let the lead later run it as itself.
    """
    from core.react_engine import _approval_id

    approval = delegated.get("approval") or {}
    inner = resume_state_for({
        "approval_id": approval.get("approval_id"),
        "tool_name": approval.get("tool_name"),
        "tool_args": approval.get("tool_args") or {},
        "progress": delegated.get("progress") or {},
    }, approve=approve, reason=reason)
    arguments = {"agent_id": delegated.get("agent_id"), "task": delegated.get("task")}
    lead_key = _approval_id("delegate_to_agent", arguments)
    return {
        **progress,
        "approved_calls": sorted(set(progress.get("approved_calls") or []) | {lead_key}),
        "pending_call": {"approval_id": lead_key, "tool": "delegate_to_agent", "arguments": arguments,
                         "approved": True, "reason": ""},
        "delegate": {"arguments": arguments, "resume_state": inner},
    }


# ── a question nobody answered (F247) ───────────────────────────────────────

#: Why a parked call was not run, as the turn that asked keeps it.
MOVED_ON = "you sent a new message instead"
NEWER_RUN = "a newer run of this schedule started"
#: What a workflow run that asked is stopped with, when its question is moved past.
RUN_STOPPED = "Stopped: the conversation moved on before its question was answered."


def moved_past_turn(pending: dict, *, why: str) -> list[dict[str, Any]]:
    """The turn a question nobody answered leaves behind, in its own place.

    The question it started from, and an answer saying the call was not run and
    why, with the calls the turn had made before it asked and the gated call
    itself recorded as not run — so the next turn's history tells the model
    (F242's digest), and a reopened page draws the card where it was asked,
    marked expired, instead of last and live.
    """
    from core.session import _now, call_record, tools_from_calls, turn_calls

    tool = pending.get("tool_name") or "a tool"
    calls = turn_calls(pending.get("progress") or {})
    calls.append(call_record(tool, pending.get("tool_args") or {}, f"Not run — {why}."))
    card = public_view(pending) or {}
    card.pop("message", None)
    return [
        {"role": "user", "content": pending.get("message") or ""},
        {"role": "assistant",
         "content": f"This turn stopped to ask permission to run `{tool}`. It was not run: {why}.",
         "tools": tools_from_calls(calls), "calls": calls, "timestamp": _now(),
         "approval": {**card, "outcome": "expired", "why": why}},
    ]


async def settle_moved_past(session_id: str, agent_key: str, *, save_as: str | None,
                            why: str = MOVED_ON, redis: Any = None) -> list[dict[str, Any]] | None:
    """Settle the question this conversation waits on, before a new turn starts.

    The call is not run and can no longer be allowed: the parked approval is
    taken in one write. The turn that asked is saved where it was asked
    (`save_as` is the agent id the conversation's turns are kept under; None
    leaves the saving to a caller that holds the history itself, the fleet's
    chat job). A workflow's call (F290) stops the run that asked — its question
    would otherwise stay answerable from the run's page, and allowing it there
    runs the abandoned call. Returns the two messages saved, or None when
    nothing was waiting.
    """
    from core.store import sessions as _sessions

    pending = await _sessions.take_pending_approval(session_id or "", agent_key)
    if not pending:
        return None
    messages = moved_past_turn(pending, why=why)
    if pending.get("run_id"):
        from core.orchestration.cancel import cancel_run
        await cancel_run(pending["run_id"], redis=redis, reason=RUN_STOPPED)
    if save_as is not None:
        await _sessions.append_turn(session_id or "default", save_as, messages)
    return messages
