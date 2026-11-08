"""What counts as a regression.

Pure functions, no database and no I/O, for two reasons. The rule is the part
of this feature a person has to trust, so it has to be testable without
standing anything up; and the UI states the rule back in the same words the
judge recorded, which is only safe while there is exactly one place that
decides what fired.

Two rules, deliberately, because they catch different failures:

* **floor** — an absolute line. Answers "is this good enough", and is the only
  rule that works on a brand-new orchestration with no history to compare to.
* **drop** — a fall below the mean of recent runs. Answers "did this get
  worse", and catches the slow slide from 9.0 to 7.5 that never crosses any
  absolute line and is what a customer actually meets.

Either may be switched off by setting it to None. Both off means the rubric
scores runs and never flags them, which is a legitimate thing to want.
"""
from __future__ import annotations

#: Below this many prior scores a rolling mean is not a baseline, it is one
#: opinion with extra steps. The `drop` rule stays silent until there are at
#: least this many — the `floor` rule does not, which is why a new
#: orchestration is not left unguarded.
MIN_BASELINE_SAMPLES = 3


def baseline(prior_scores: list[float], window: int) -> float | None:
    """The mean of the most recent `window` scores, or None if there are too few.

    `prior_scores` is oldest-first, matching what the store returns, and must
    already be filtered to one rubric version: a mean taken across a rubric
    change averages the answers to two different questions.
    """
    if window <= 0:
        return None
    recent = [s for s in prior_scores[-window:] if s is not None]
    if len(recent) < MIN_BASELINE_SAMPLES:
        return None
    return sum(recent) / len(recent)


def verdict(
    score: float,
    base: float | None,
    floor: float | None = None,
    drop: float | None = None,
) -> tuple[bool, str | None]:
    """Did this score regress, and which rule says so.

    Returns `(regression, rule)` where rule is "floor", "drop" or None. The
    floor is checked first so that a run which is both below the line and below
    its baseline is reported as the simpler, more alarming fact.
    """
    if floor is not None and score < floor:
        return True, "floor"
    if drop is not None and base is not None and score < base - drop:
        return True, "drop"
    return False, None


def describe(rule: str | None, score: float, base: float | None,
             floor: float | None, drop: float | None) -> str:
    """One sentence naming what fired, for the screen and the notification.

    The dashboard must not paraphrase the rule in its own words: a rule the UI
    describes differently from the way it was applied is worse than no
    explanation, because it will be believed.
    """
    if rule == "floor":
        return f"Scored {score:.1f}, below the floor of {floor:.1f}."
    if rule == "drop":
        assert base is not None and drop is not None
        return (
            f"Scored {score:.1f}, more than {drop:.1f} below the recent "
            f"average of {base:.1f}."
        )
    return f"Scored {score:.1f}."


def agreement(scores: list[dict], feedback: dict[str, list[dict]],
              scale_max: float) -> dict:
    """How often the judge and the people who thumbed a run agree.

    The judge's number is only worth something if it can be checked, and this
    is the check. A run counts when it has both a score and at least one thumb;
    the thumbs are summed so a run with two ups and one down reads as positive.
    Agreement means the judge put it on the same side of the midpoint as the
    people did.

    Returns counts as well as the rate, because a rate computed from four runs
    is a number that should be shown with its denominator or not at all.
    """
    midpoint = scale_max / 2
    agreed = 0
    compared = 0
    for score in scores:
        thumbs = feedback.get(score["run_id"]) or []
        if not thumbs:
            continue
        net = sum(t["verdict"] for t in thumbs)
        if net == 0:
            continue
        compared += 1
        judge_positive = score["score"] >= midpoint
        human_positive = net > 0
        if judge_positive == human_positive:
            agreed += 1
    return {
        "compared": compared,
        "agreed": agreed,
        "rate": (agreed / compared) if compared else None,
    }
