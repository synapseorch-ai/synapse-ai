"""
What stops a scraped key from spending the deployment's model budget.

The origin allow-list is the front door, and it is the strong control. This is
the floor underneath it, for the cases the door cannot cover: a forged `Referer`,
a browser driven by a script on an allowed page, or simply a popular site where
the traffic is legitimate and still more than the owner meant to pay for.

Three counters, each answering a different question:

    per visitor IP     one person hammering the widget
    per widget, daily  the bill the owner agreed to
    concurrent turns   a Free-tier install's only run slot   (cloud enforces this
                       through its own admission; here it is advisory)

**Fixed windows, not sliding.** The same choice `api_v2._check_rate_limit` and
the cloud's `api/control/ratelimit.py` make: a burst at a window boundary can
briefly double the rate, and that is an acceptable price for one `INCR` instead
of a sorted set per caller.

**Redis when there is one, memory when there is not.** Single-process open source
has no Redis, and a limiter that silently did nothing there would be worse than
no limiter: the settings screen would show a cap that was never enforced. The
in-process fallback is correct for one process, which is exactly what that
deployment is. Scale mode gets the shared counter.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

#: A visitor's own budget. Generous enough that a real conversation never trips
#: it — a person cannot type twenty messages in five minutes and mean them.
PER_IP = ("ip", 20, 300)

#: How often a page may ask for a token. Higher than PER_IP because an ordinary
#: visitor mints once, and a reload mints again.
SESSION_PER_IP = ("sess", 30, 300)


@dataclass
class _Window:
    count: int
    expires_at: float


#: Fallback counters for a deployment with no Redis. Bounded by `_sweep`, which
#: runs on write — an unbounded dict keyed on visitor IP is a memory leak with a
#: public trigger, which is the kind of bug this module exists to prevent.
_MEMORY: dict[str, _Window] = {}
_MAX_TRACKED = 50_000


def _sweep(now: float) -> None:
    if len(_MEMORY) < _MAX_TRACKED:
        return
    for key in [k for k, w in _MEMORY.items() if w.expires_at <= now]:
        _MEMORY.pop(key, None)
    if len(_MEMORY) >= _MAX_TRACKED:
        # Still full of live windows: a flood from many addresses. Drop the lot
        # rather than grow without bound — the next request re-counts from zero,
        # which is a worse limit for one window and not an outage.
        _MEMORY.clear()


async def _bump(redis, key: str, window_seconds: int) -> int:
    """The count in the current window, after counting this call."""
    now = time.time()
    bucket = f"{key}:{int(now // window_seconds)}"

    if redis is not None:
        try:
            count = await redis.incr(f"synapse:widget:rl:{bucket}")
            if count == 1:
                await redis.expire(f"synapse:widget:rl:{bucket}", window_seconds + 2)
            return int(count)
        except Exception:  # noqa: BLE001 — a limiter must not take the site down
            pass

    _sweep(now)
    window = _MEMORY.get(bucket)
    if window is None or window.expires_at <= now:
        window = _Window(count=0, expires_at=now + window_seconds + 2)
        _MEMORY[bucket] = window
    window.count += 1
    return window.count


async def check(redis, bucket: str, key: str, limit: int, window_seconds: int) -> bool:
    """True if this call is within the limit."""
    if limit <= 0:
        return True
    count = await _bump(redis, f"{bucket}:{key}", window_seconds)
    return count <= limit


async def check_ip(redis, ip: str, spec: tuple[str, int, int] = PER_IP) -> bool:
    bucket, limit, window = spec
    return await check(redis, bucket, ip or "unknown", limit, window)


async def check_daily(redis, widget_id: str, cap: int) -> bool:
    """The widget's own daily message cap.

    Keyed on the UTC day rather than a rolling 24 hours, so "500 a day" means a
    number the owner can reconcile against a calendar.
    """
    if not cap or cap <= 0:
        return True
    day = time.strftime("%Y%m%d", time.gmtime())
    count = await _bump(redis, f"day:{widget_id}:{day}", 86_400)
    return count <= cap


def reset() -> None:
    """Drop the in-process counters. For tests."""
    _MEMORY.clear()
