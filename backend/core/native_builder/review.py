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

#: F754's yes, word for word as the review gates' `if_condition` and the panel's `APPROVAL_WORDS` take it (a test
#: holds the three equal): code that acts on a yes checks it again rather than trusting the branch it is on.
APPROVAL_WORDS = ("approve", "approved", "ok", "okay", "yes", "looks good", "lgtm", "go ahead", "build it", "proceed",
                  "sounds good", "do it", "👍")


def is_yes(answer: Any) -> bool:
    """The gate's own reading: trimmed, lower-cased, with a trailing space, dot or bang dropped."""
    return isinstance(answer, str) and answer.strip().lower().strip(" .!") in APPROVAL_WORDS


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
    """The steps on a path from `start` to `end` (neither counted, and not passing `start` again) that write one of
    `keys`: what could change an approved object after the yes and before it is applied. A path that goes back
    through `start` brings a new review, and a new yes, before any apply: a redraft on it is not between the two
    (SAI2-P2: the change route's "change something" loop re-checks, then reviews again)."""
    from core.builder_tools import step_successors

    by_id = {s["id"]: s for s in orch.get("steps") or [] if isinstance(s, dict) and s.get("id")}
    if start not in by_id:
        return []
    after: set[str] = set()
    todo = list(step_successors(by_id[start], by_id))
    while todo:
        sid = todo.pop(0)
        if sid in after or sid in (start, end) or sid not in by_id:
            continue
        after.add(sid)
        todo += step_successors(by_id[sid], by_id)
    # Which of them still reach `end` without passing `start` again.
    reaches = {sid for sid in after if end in step_successors(by_id[sid], by_id)}
    grew = True
    while grew:
        grew = False
        for sid in after - reaches:
            if any(n in reaches for n in step_successors(by_id[sid], by_id)):
                reaches.add(sid)
                grew = True
    found: list[str] = []
    for sid in sorted(reaches, key=list(by_id).index):
        step = by_id[sid]
        written = {step.get("output_key")}
        if step.get("type") == "loop":
            written |= {step.get("loop_item_key") or "loop_item", step.get("loop_index_key") or "loop_index"}
        if written & keys:
            found.append(sid)
    return found
