"""A person's yes to a Plan or a Change, kept in the run, and the rule that nothing rewrites what they approved before
it is applied (SAI2-P1, F778; SAI2's binding, 10-21).

The review step's yes gate (code, F754) records the approval with `record_approval`; `step_apply` / `step_save`
(P2/P3) read it with `approved_review` and hand it to `registry.invoke(confirm_policy="approved_review", ...)` with the
object as state then holds it. The record is in the run's shared state, which every checkpoint stores, so a resume on
another worker finds it. `writes_between` is the check P2's and P3's definition tests run: no step on a path from the
review to the apply may write the approved object's key.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.approved import ApprovedReview, approve
from core.product_tools.caller import Caller

#: The run-state key the approval is kept under (private: a step's prompt never reads it).
REVIEW_KEY = "_approved_review"


def record_approval(state: dict[str, Any], obj: Any, caller: Caller, calls: list[tuple[str, dict[str, Any]]], *,
                    run_id: str = "") -> ApprovedReview:
    """Keep `caller`'s yes to `obj`, which makes `calls`, in the run's state."""
    review = approve(obj, caller, calls, run_id=run_id)
    state[REVIEW_KEY] = review.to_json()
    return review


def approved_review(state: dict[str, Any]) -> ApprovedReview | None:
    """The run's approval, as recorded; None when there is none."""
    return ApprovedReview.from_json(state.get(REVIEW_KEY))


def writes_between(orch: dict[str, Any], start: str, end: str, keys: set[str]) -> list[str]:
    """The steps on a path from `start` to `end` (neither counted) that write one of `keys`: what could change an
    approved object after the yes and before it is applied."""
    from core.builder_tools import step_successors

    by_id = {s["id"]: s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")}
    found: list[str] = []
    seen: set[str] = set()
    todo = [n for n in step_successors(by_id[start], by_id)] if start in by_id else []
    while todo:
        sid = todo.pop(0)
        if sid in seen or sid == end or sid not in by_id:
            continue
        seen.add(sid)
        step = by_id[sid]
        written = {step.get("output_key")}
        if step.get("type") == "loop":
            written |= {step.get("loop_item_key") or "loop_item", step.get("loop_index_key") or "loop_index"}
        if written & keys:
            found.append(sid)
        todo += step_successors(step, by_id)
    return found
