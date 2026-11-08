"""A workflow's cap on what one run may spend (B9, CAP1, D272).

A workflow can carry `max_run_credits` and/or `max_run_usd`. The engine asks
before every step it starts — at the top of the run, in a Loop's body, in a
Parallel's branches and in a nested run — what the run has spent, and a run that
has reached its cap stops with its own sentence as its reason, as a run its
length or its host's gate stops does (`run_gate.stop_reason` asks the cap first).

The engine `max_total_cost_usd` was the field this replaces, and it was read by
nothing (F76): a cap that does not cap is worse than none. The old name is not
reused, so a document that still carries it never becomes a cap.

## What counts

* **Dollars** are the run's model cost: what its calls cost, with the runs
  nested under it. Self-hosted, that is the rate card's estimate of each call on
  your own keys (`usage_logs`); a model the rate card does not price adds 0, so
  the cap cannot see it. Cloud adds what its model wallet paid. Checked as
  *spent*: what the next step's calls will cost is not known before it runs.
* **Credits** are the host's: self-hosted has none. Cloud's are what the run
  would be charged — the greater of its steps' prices and its minutes, the way it
  is charged — with the next step's price added, so a run stops before the step
  that would take it past its cap.

A step that is already working finishes: the cap is asked between steps, so a
run can pass it by what its last step spent.

## Whose cap

The workflow that was started: its cap covers the run and every run nested under
it, which are its work (a nested run's id is its root's plus `__…`). A called
workflow's own cap applies when it is run on its own. So a nested engine is
handed its root's cap (`OrchestrationEngine.run_cap`) and never reads its own.

A cap that cannot be read stops nothing: a lookup error must not end a run. With
no cap, nothing is read at all.
"""
from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.log import get_logger

_log = get_logger(__name__)


def _positive(value: Any, *, at_least: float | None = None) -> float | None:
    """A cap's value, or None when it is not one.

    Lenient on purpose: a definition is read on every run and resume, and one
    the builder or an API client wrote with a nonsense cap must still run — as
    uncapped, never as stopped before its first step.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    if at_least is not None and number < at_least:
        return None
    return number


#: Every run is charged at least a credit, so a credit cap under one would stop
#: every run before its first step.
MIN_CREDITS = 1.0


@dataclass(frozen=True)
class RunCap:
    """What one run of a workflow may spend: credits, dollars, or both."""

    credits: float | None = None
    usd: float | None = None

    @classmethod
    def of(cls, orch: Any) -> RunCap | None:
        """The cap a definition sets, or None. `orch` is a model or a dict."""
        get = orch.get if isinstance(orch, dict) else (lambda key: getattr(orch, key, None))
        credits = _positive(get("max_run_credits"), at_least=MIN_CREDITS)
        usd = _positive(get("max_run_usd"))
        if credits is None and usd is None:
            return None
        return cls(credits=credits, usd=usd)


def clean(value: Any, *, credits: bool) -> float | None:
    """A cap field's stored value: None for unset, the number for a cap.

    Raises ValueError, in a sentence fit to show, for a value that is not a cap.
    For the save paths that skip the model (the builder's tools).
    """
    if value is None or value == "":
        return None
    number = _positive(value, at_least=MIN_CREDITS if credits else None)
    if number is None:
        raise ValueError(
            "max_run_credits must be a number of credits, 1 or more, or null for no cap"
            if credits else
            "max_run_usd must be a dollar amount above 0, or null for no cap")
    return number


@dataclass(frozen=True)
class Spend:
    """What a run has spent, as the host counts it. None: not counted."""

    #: Model dollars its calls have cost so far.
    usd: float | None = None
    #: Credits it would be charged if it ended now.
    credits: float | None = None
    #: Credits it would be charged once the step about to start is entered.
    credits_after: float | None = None
    #: That step's own price, for the sentence.
    step_credits: float | None = None


#: `(run_id, step_type, cap) -> Spend`. A reader reads only the halves `cap` sets.
SpendReader = Callable[[str, str, RunCap], Awaitable[Spend]]


def root_of(run_id: str) -> str:
    """The run a nested run's spending counts toward."""
    return run_id.split("__", 1)[0]


async def _own_keys(run_id: str, step_type: str, cap: RunCap) -> Spend:
    """Self-hosted: dollars from the rate card's estimate of each call; no credits."""
    if cap.usd is None:
        return Spend()
    from core.store.usage import spent_usd

    return Spend(usd=await spent_usd(root_of(run_id)))


_reader: SpendReader = _own_keys


def set_spend_reader(reader: SpendReader | None) -> None:
    """Register the host's reader (cloud's wallet and credits), or with None
    go back to self-hosted's own-keys reader."""
    global _reader
    _reader = reader if reader is not None else _own_keys


def _number(n: float) -> str:
    """4.6, 5, 0.1 — a credit figure as the run card shows it."""
    rounded = round(n, 1)
    return f"{rounded:,.0f}" if rounded == int(rounded) else f"{rounded:,.1f}"


def credits_words(n: float) -> str:
    """'1 credit', '2.5 credits'."""
    text = _number(n)
    return f"{text} credit" if text == "1" else f"{text} credits"


def usd_words(usd: float) -> str:
    """'$0.52', '$0.0042' — the run page's own rule (`components/run/model.ts` money)."""
    if usd <= 0:
        return "$0"
    if usd < 0.0001:
        return "<$0.0001"
    whole, _, frac = f"{usd:,.4f}".rstrip("0").partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"


#: Every sentence ends here. No double quotes: the engine's readers extract
#: sentences with a `"message": "…"` pattern.
_RAISE = "Raise the cap in the workflow's settings, then Replay."


def credits_sentence(cap: float, spend: Spend) -> str:
    """Why a run stopped at its credit cap."""
    used = spend.credits if spend.credits is not None else 0.0
    head = f"Stopped: this run reached its cap of {credits_words(cap)}."
    if spend.step_credits and used < cap:
        # Its next step is what would take it past.
        return (f"{head} It had used about {credits_words(used)}, and its next step costs "
                f"{_number(spend.step_credits)}. {_RAISE}")
    return f"{head} It had used about {credits_words(used)}. {_RAISE}"


def usd_sentence(cap: float, spent: float) -> str:
    """Why a run stopped at its dollar cap."""
    return (f"Stopped: this run reached its cap of {usd_words(cap)} in model costs. "
            f"It had spent about {usd_words(spent)}. {_RAISE}")


def _mc(credits: float) -> int:
    """Compared in millicredits, the ledger's unit, so 4.9 + 0.1 is 5."""
    return round(credits * 1000)


async def stop_reason(run_id: str, step_type: str, cap: RunCap | None) -> str | None:
    """Why this run may not start a step of `step_type` under `cap`, or None."""
    if cap is None:
        return None
    try:
        spend = await _reader(run_id, step_type, cap)
    except Exception:  # noqa: BLE001 — a lookup error must not end a run
        _log.warning("run cap could not be read for %s; the step goes ahead", run_id, exc_info=True)
        return None
    if cap.credits is not None and spend.credits_after is not None:
        if _mc(spend.credits_after) > _mc(cap.credits):
            return credits_sentence(cap.credits, spend)
    if cap.usd is not None and spend.usd is not None and spend.usd >= cap.usd:
        return usd_sentence(cap.usd, spend.usd)
    return None


@dataclass(frozen=True)
class Room:
    """What a run's cap leaves one step's own work, read as the step starts or resumes (TEAM1e).

    A Team step spends inside itself — model calls, members, helpers — where `stop_reason`, read between steps,
    never looks: its team checks these before every call and every task (20 Q8). None: not capped, or not counted.
    """

    #: Model dollars left: the cap less what the run has spent so far.
    usd: float | None = None
    #: Tasks left, each priced as the step itself (1 + tasks, 20 Q7): the credits the cap leaves after this step.
    tasks: int | None = None
    cap: RunCap | None = None


async def room(run_id: str, step_type: str, cap: RunCap | None, *, resumed: bool = False) -> Room:
    """What `cap` leaves the step of `step_type` about to run. `resumed`: the step is re-entered after a person's
    answer, so its paused entry is already in the run's spend and its own price is not counted again."""
    if cap is None:
        return Room()
    try:
        spend = await _reader(run_id, step_type, cap)
    except Exception:  # noqa: BLE001 — a lookup error must not end a run
        _log.warning("run cap could not be read for %s; the step's team goes on unchecked", run_id, exc_info=True)
        return Room(cap=cap)
    usd = max(0.0, cap.usd - spend.usd) if cap.usd is not None and spend.usd is not None else None
    tasks = None
    if cap.credits is not None and spend.credits_after is not None and spend.step_credits:
        after = spend.credits_after - (spend.step_credits if resumed else 0.0)
        tasks = max(0, (_mc(cap.credits) - _mc(after)) // _mc(spend.step_credits))
    return Room(usd=usd, tasks=tasks, cap=cap)
