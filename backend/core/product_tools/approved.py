"""A person's approved review, standing in for per-call confirm tokens (SAI2-P1, F778).

Synapse AI acts through `registry.invoke` as the person whose conversation it is (SAI2, decision 3). Its acts
otherwise need a token per call (`every_act`: a card the person presses). A change or a build is approved as a
whole at its review step, so `confirm_policy="approved_review"` lets that approval stand in for the tokens of the
calls it covered — and for nothing else (SAI2's binding, 10-21):

* `approve` hashes the approved Plan / Change at the yes and keeps who said it and every call it makes (tool and
  exact arguments, as `confirm.args_hash` hashes them);
* `uncovered` is asked at each call with the object as state holds it then: changed since, another person, or a
  call it did not make, and the call is not covered.

The registry checks scope and the live role first, as for every call, and falls back to `every_act` for anything
not covered. The record is plain JSON, kept in the run's checkpointed state (`core.native_builder.review`).
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from core.product_tools.caller import Caller
from core.product_tools.confirm import args_hash


def digest(obj: Any) -> str:
    """The approved object's fingerprint: its canonical JSON."""
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class ApprovedReview:
    digest: str
    approver: str
    covers: frozenset[tuple[str, str]]
    run_id: str = ""
    #: This yes's own id, minted when it is given: each call it covers is spent once under it (MCP2's re-check, A).
    #: Two approvals of an identical change (set it back, then again; undo, then redo) share a digest, never an id.
    id: str = field(default="", compare=False)

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "digest": self.digest, "approver": self.approver, "run_id": self.run_id,
                "covers": sorted([tool, h] for tool, h in self.covers)}

    @classmethod
    def from_json(cls, raw: Any) -> "ApprovedReview | None":
        if not isinstance(raw, dict) or not raw.get("digest") or not raw.get("approver"):
            return None
        covers = frozenset((str(t), str(h)) for t, h in raw.get("covers") or [] if t and h)
        return cls(digest=str(raw["digest"]), approver=str(raw["approver"]), covers=covers,
                   run_id=str(raw.get("run_id") or ""), id=str(raw.get("id") or raw["digest"]))


def approve(obj: Any, caller: Caller, calls: list[tuple[str, dict[str, Any]]], *, run_id: str = "") -> ApprovedReview:
    """The record of `caller` saying yes to `obj`, which makes `calls`."""
    return ApprovedReview(digest=digest(obj), approver=caller.who,
                          covers=frozenset((tool, args_hash(args)) for tool, args in calls), run_id=run_id,
                          id=secrets.token_urlsafe(12))


def uncovered(review: ApprovedReview | None, caller: Caller, tool: str, args: dict[str, Any], current: Any) -> str | None:
    """Why this call is not covered by the review, or None when it is."""
    if review is None:
        return "no approved review"
    if digest(current) != review.digest:
        return "what was approved has changed since"
    if caller.who != review.approver:
        return "someone else approved it"
    if (tool, args_hash(args)) not in review.covers:
        return "the approved review does not make this call"
    return None
