"""What each step of a run spent, attributed by time (CE1, D261; F454).

`usage_logs` has no step id: a row is one model call, with the run's id and the
moment it was logged. So a step's calls are the rows inside the window its
history entry records, and this is where that rule lives, for both products'
run pages: OSS sweeps its own calls, cloud adds the wallet's settles.

## Exact windows, and the old ones

A history entry's `started_at` and `ended_at` are whole seconds, and the
engine's other readers parse them that way, so they stay. Beside them the
engine now writes `started_ms` and `ended_ms` (`exact_window`), and a window is
read from those when they are there.

With whole seconds only, the run page gave a step every call up to 2 s past its
cut-off end, which is how the next step's first call went to the wrong step:
Editing pass's "Line edit" (1.4 s) showed no call and "Structure" showed two
(CE1, measured on the dev stack). An entry written before the exact stamps keeps
that old rule, so an old run reads as it always has. An exact window allows
0.5 s, for a wallet's settle written a moment after the call returns.

A human step's entry claims no call: a person's wait calls no model, and its
window (the whole wait) holds the calls of whatever runs beside it or after the
answer.

## Every pass counts (F454)

Steps are swept in history order and each entry claims the calls inside its
window, so no call is counted twice. A step that ran three times (a Loop body, a
retry) sums its three passes; it used to report only the last. Each entry's own
figure is kept as well, for a timeline that shows each attempt.

`cost_usd` is None, never 0, when a step's only calls are on a model nothing
prices: "free" would be a claim (F63).

## A call says which step it ran for (F461)

Windows cannot tell steps apart that run at once: a Parallel's branches start
together, and each branch's entry is written when it ends, so the first branch
to finish held a window around every branch's calls. C9 measured four branches'
four calls all under `w0`. So the engine stamps a call with the step it ran for
(`step_scope`, set where every executor runs a step), `usage_tracker` writes the
stamp in the row's `details`, and `sweep` gives a stamped call of this run to its
step before any window is read. A wallet's settle (cloud) has no step of its
own; it follows the stamped call it paid for, matched by its tokens, since the
gateway settles as the call returns and the engine logs it a moment later. What
carries no stamp — a row from before, a nested run's call (its own run's step,
F284) — is swept by time, as before.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: How far past an exact end a call may still be the step's.
EXACT_SLACK = timedelta(seconds=0.5)

#: How far past a whole-second end it may be: the second the stamp dropped, and a
#: little more (the rule the run page has always used for these entries).
LEGACY_SLACK = timedelta(seconds=2)

#: How far apart a settle and the call it paid for may be logged (measured: 10–50 ms).
PAIR_SLACK = timedelta(seconds=2)

#: The run and step a model call is made for, while a step runs (F461).
_STEP: ContextVar[dict | None] = ContextVar("synapse_current_step", default=None)


@contextmanager
def step_scope(run_id: str | None, step_id: str | None) -> Iterator[None]:
    """Stamp the model calls made inside it with `run_id`'s step `step_id`.

    A task made inside it (a Parallel's branch, a helper, a delegate) inherits the
    stamp and may set its own. The previous value is put back rather than reset by
    token: an abandoned generator is closed from another task, where a reset fails.
    """
    previous = _STEP.get()
    _STEP.set({"run_id": run_id, "step_id": step_id} if run_id and step_id else previous)
    try:
        yield
    finally:
        _STEP.set(previous)


def current_step() -> dict | None:
    """`{"run_id", "step_id"}` while a step runs; None otherwise."""
    return _STEP.get()


def exact_window(started: float, ended: float | None = None) -> dict[str, int]:
    """The exact window to write on a history entry, in epoch milliseconds."""
    return {"started_ms": int(started * 1000), "ended_ms": int((time.time() if ended is None else ended) * 1000)}


@dataclass(frozen=True)
class Call:
    """One model call, as `usage_logs` records it."""

    at: datetime
    model: str | None = None
    tool: str | None = None
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: The rate card's estimate. 0 for a call the wallet paid (`Payment`).
    cost: float = 0.0
    context_chars: int = 0
    latency: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: (run_id, step_id) the call was made for, when the engine stamped it (F461).
    step: tuple[str, str] | None = None


@dataclass(frozen=True)
class Payment:
    """What a wallet paid for a call, in USD (cloud's settles)."""

    at: datetime
    usd: float
    #: The call's tokens as the settle records them, to find the stamped call it paid for (F461).
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


def calls_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Call]:
    """`usage_logs` records, as the store returns them, as calls."""
    calls = []
    for row in rows:
        at = parse_utc(row.get("timestamp"))
        if at is None:
            continue
        calls.append(Call(
            at=at,
            model=row.get("model") or None,
            tool=row.get("tool_name") or None,
            tokens=int(row.get("total_tokens") or 0),
            input_tokens=int(row.get("input_tokens") or 0),
            output_tokens=int(row.get("output_tokens") or 0),
            cost=float(row.get("estimated_cost") or 0.0),
            context_chars=int(row.get("context_chars") or 0),
            latency=float(row.get("latency_seconds") or 0.0),
            cache_read_tokens=int(row.get("cache_read_tokens") or 0),
            cache_write_tokens=int(row.get("cache_write_tokens") or 0),
            step=stamp_of(row.get("step")),
        ))
    return calls


def stamp_of(value: Any) -> tuple[str, str] | None:
    """A row's step stamp (`{"run_id", "step_id"}`) as (run_id, step_id); None when it has none."""
    if isinstance(value, Mapping) and value.get("run_id") and value.get("step_id"):
        return str(value["run_id"]), str(value["step_id"])
    return None


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _from_ms(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def window(entry: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    """An entry's start and the last moment a call can be its own; None for an open end.

    Exact only when its end is exact too, or it has not ended: an entry some
    other writer closed with a whole-second `ended_at` alone (a human step's
    answer, a nested run's end, a cancel) reads by the old rule throughout, never
    as an exact start with no end, which would claim every call after it.
    """
    started = _from_ms(entry.get("started_ms"))
    ended_exact = _from_ms(entry.get("ended_ms"))
    if started is not None and (ended_exact is not None or not entry.get("ended_at")):
        return started, None if ended_exact is None else ended_exact + EXACT_SLACK
    started = parse_utc(entry.get("started_at"))
    ended = parse_utc(entry.get("ended_at"))
    return started, None if ended is None else ended + LEGACY_SLACK


def figure(calls: list[Call], payments: list[Payment], *, unpriced: Callable[[str], bool]) -> dict[str, Any]:
    """What a set of calls spent, in the fields the run page shows."""
    cost: float | None = round(sum(c.cost for c in calls) + sum(p.usd for p in payments), 6)
    if cost == 0 and any(c.model and unpriced(c.model) for c in calls):
        cost = None
    return {
        # One row is one model call, which is what a "turn" means here.
        "turns": len(calls),
        "models": sorted({c.model for c in calls if c.model}),
        "tools": sorted({c.tool for c in calls if c.tool}),
        "tokens": sum(c.tokens for c in calls),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cost_usd": cost,
        # A high-water mark, never a sum: context is the size of one prompt.
        "context_peak_chars": max((c.context_chars for c in calls), default=0),
        "latency_seconds": round(sum(c.latency for c in calls), 2),
    }


def _entry_for(call: Call, indexes: list[int], history: list[Any]) -> int:
    """Which pass of its step a stamped call belongs to: the entry whose window holds it, else the last to start."""
    started_before = None
    for i in indexes:
        started, limit = window(history[i])
        if started is not None and call.at >= started:
            if limit is None or call.at <= limit:
                return i
            started_before = i
    return started_before if started_before is not None else indexes[0]


def _pays_for(payment: Payment, call: Call) -> bool:
    if payment.input_tokens is None or payment.output_tokens is None:
        return False
    return (payment.input_tokens == call.input_tokens and payment.output_tokens == call.output_tokens
            and (payment.cache_read_tokens or 0) == call.cache_read_tokens
            and (payment.cache_write_tokens or 0) == call.cache_write_tokens
            and abs(call.at - payment.at) <= PAIR_SLACK)


def sweep(
    history: Iterable[Any],
    calls: Iterable[Call],
    payments: Iterable[Payment] = (),
    *,
    unpriced: Callable[[str], bool] = lambda model: False,
    run_id: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any] | None]]:
    """Each step's spend (passes summed), and each history entry's own.

    The second list is aligned with `history`: an entry that claimed no call is
    None. A call inside no window is left out rather than forced into the
    nearest step; the run's own total stays the authority. With `run_id`, a call
    stamped with one of this run's steps is that step's first (F461).
    """
    history = list(history)
    calls = [c for c in calls if c.at is not None]
    payments = [p for p in payments if p.at is not None]
    entry_calls: list[list[Call]] = [[] for _ in history]
    entry_paid: list[list[Payment]] = [[] for _ in history]

    def step_of(entry: Any) -> str:
        step_id = str(entry.get("step_id") or "") if isinstance(entry, Mapping) else ""
        # A person's wait calls no model, so it claims nothing: its window can hold
        # the calls of a step running beside it, or of the step after the answer.
        return "" if step_id and entry.get("step_type") == "human" else step_id

    # A stamped call is its step's, whatever the windows say (F461); a settle follows the call it paid for.
    if run_id:
        entries_of: dict[str, list[int]] = {}
        for i, entry in enumerate(history):
            if step_of(entry):
                entries_of.setdefault(step_of(entry), []).append(i)
        stamped: list[tuple[Call, int]] = []
        rest: list[Call] = []
        for c in calls:
            if c.step and c.step[0] == run_id and c.step[1] in entries_of:
                i = _entry_for(c, entries_of[c.step[1]], history)
                entry_calls[i].append(c)
                stamped.append((c, i))
            else:
                rest.append(c)
        calls = rest
        unpaired: list[Payment] = []
        for p in sorted(payments, key=lambda p: p.at):
            match = min((s for s in stamped if _pays_for(p, s[0])), key=lambda s: abs(s[0].at - p.at), default=None)
            if match is None:
                unpaired.append(p)
                continue
            stamped.remove(match)
            entry_paid[match[1]].append(p)
        payments = unpaired

    # Everything else by time, steps in history order, each claiming what its window holds.
    unclaimed = sorted(calls, key=lambda c: c.at)
    unpaid = sorted(payments, key=lambda p: p.at)
    for i, entry in enumerate(history):
        step_id = step_of(entry)
        started, limit = window(entry) if step_id else (None, None)
        if started is None:
            continue

        def inside(at: datetime, started: datetime = started, limit: datetime | None = limit) -> bool:
            return at >= started and (limit is None or at <= limit)

        mine = [c for c in unclaimed if inside(c.at)]
        if not mine and not entry_calls[i]:
            continue
        unclaimed = [c for c in unclaimed if not inside(c.at)]
        entry_calls[i].extend(mine)
        entry_paid[i].extend(p for p in unpaid if inside(p.at))
        unpaid = [p for p in unpaid if not inside(p.at)]

    per_entry: list[dict[str, Any] | None] = []
    per_step: dict[str, tuple[list[Call], list[Payment]]] = {}
    for i, entry in enumerate(history):
        if not entry_calls[i]:
            per_entry.append(None)
            continue
        per_entry.append(figure(entry_calls[i], entry_paid[i], unpriced=unpriced))
        held = per_step.setdefault(step_of(entry), ([], []))
        held[0].extend(entry_calls[i])
        held[1].extend(entry_paid[i])

    by_step = {sid: figure(c, p, unpriced=unpriced) for sid, (c, p) in per_step.items()}
    return by_step, per_entry
