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

Neither function touches the store or the network — they are shape, so that a
test can assert the shape without a database.
"""

from __future__ import annotations

from typing import Any


def parked_payload(event: dict, *, message: str, progress: dict | None) -> dict[str, Any]:
    """What to store when the loop yields `human_input_required`/`tool_approval`.

    `message` is the question that started the turn: a resume rebuilds the
    conversation around it, because the loop takes a request and not a
    transcript. `progress` is the last `_step_progress` the loop emitted —
    never forwarded to a client, because it is engine state and not an event.
    """
    return {
        "tool_name": event.get("tool_name"),
        "tool_args": event.get("tool_args") or {},
        "approval_id": event.get("approval_id"),
        "category": event.get("category"),
        "message": message or "",
        "progress": progress or {},
    }


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
    approval_id = pending.get("approval_id")
    progress = dict(pending.get("progress") or {})
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
    return state
