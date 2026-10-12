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

## Every pass counts (F454)

Steps are swept in history order and each entry claims the calls inside its
window, so no call is counted twice. A step that ran three times (a Loop body, a
retry) sums its three passes; it used to report only the last. Each entry's own
figure is kept as well, for a timeline that shows each attempt.

`cost_usd` is None, never 0, when a step's only calls are on a model nothing
prices: "free" would be a claim (F63).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: How far past an exact end a call may still be the step's.
EXACT_SLACK = timedelta(seconds=0.5)

#: How far past a whole-second end it may be: the second the stamp dropped, and a
#: little more (the rule the run page has always used for these entries).
LEGACY_SLACK = timedelta(seconds=2)


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


@dataclass(frozen=True)
class Payment:
    """What a wallet paid for a call, in USD (cloud's settles)."""

    at: datetime
    usd: float


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
        ))
    return calls


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
    """An entry's start and the last moment a call can be its own; None for an open end."""
    started = _from_ms(entry.get("started_ms"))
    if started is not None:
        ended = _from_ms(entry.get("ended_ms"))
        return started, None if ended is None else ended + EXACT_SLACK
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


def sweep(
    history: Iterable[Any],
    calls: Iterable[Call],
    payments: Iterable[Payment] = (),
    *,
    unpriced: Callable[[str], bool] = lambda model: False,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any] | None]]:
    """Each step's spend (passes summed), and each history entry's own.

    The second list is aligned with `history`: an entry that claimed no call is
    None. A call inside no window is left out rather than forced into the
    nearest step; the run's own total stays the authority.
    """
    unclaimed = sorted((c for c in calls if c.at is not None), key=lambda c: c.at)
    unpaid = sorted((p for p in payments if p.at is not None), key=lambda p: p.at)
    per_entry: list[dict[str, Any] | None] = []
    per_step: dict[str, tuple[list[Call], list[Payment]]] = {}

    for entry in history:
        step_id = str(entry.get("step_id") or "") if isinstance(entry, Mapping) else ""
        started, limit = window(entry) if step_id else (None, None)
        if started is None:
            per_entry.append(None)
            continue

        def inside(at: datetime, started: datetime = started, limit: datetime | None = limit) -> bool:
            return at >= started and (limit is None or at <= limit)

        mine = [c for c in unclaimed if inside(c.at)]
        if not mine:
            per_entry.append(None)
            continue
        unclaimed = [c for c in unclaimed if not inside(c.at)]
        paid = [p for p in unpaid if inside(p.at)]
        unpaid = [p for p in unpaid if not inside(p.at)]
        per_entry.append(figure(mine, paid, unpriced=unpriced))
        held = per_step.setdefault(step_id, ([], []))
        held[0].extend(mine)
        held[1].extend(paid)

    by_step = {sid: figure(c, p, unpriced=unpriced) for sid, (c, p) in per_step.items()}
    return by_step, per_entry
