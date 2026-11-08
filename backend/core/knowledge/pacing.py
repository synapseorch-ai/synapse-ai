"""A provider's rate limit is waited out, not failed — the same for every provider (F356 for contexts, PG1 for
embeddings).

Once a limit outlasts what the provider's own call already retried, the work waits `RATE_WAIT_S`, doubling to
`RATE_WAIT_MAX_S`, and asks again; it gives up only after `NO_PROGRESS_S` with nothing answered. Each wait is told
to the work's progress (`progress.current()`), so a screen says whose limit it is waiting on and until when.

* **Contexts** (`enrich._Pacer`) keep their own loop — one call at a time once limited — and read these values
  as their own module's names.
* **Embedding** waits inside the embedder's slice loop (`embedders.ProviderEmbedder`), and only where the
  indexing pipeline asked for it (`paced()`): a search's question is never kept waiting, and the reconcile
  backfill, which runs inside the reconcile loop itself, still stops at the first failure and tries again on
  the next pass.

A rate limit is the provider saying "slow down": `LLMError.code == rate_limited` (a 429 that is not an
exhausted credit — our gateway's `model_throttled` too), or a failure in words (Bedrock's `ThrottlingException`).
An out-of-credit answer is never one: it never clears.

**Giving the slot back (KL1, D191; F360).** Where one worker serves every org, a wait held in a job slot holds it
from everyone. Work run inside `parking()` (cloud's knowledge worker) waits in place only until its waits in this
run would pass `GIVE_BACK_AFTER_S` (15 + 30 + 60 s), then raises `Parked`: the document goes back to `queued`, saying
whose limit and until when, and the worker queues it again for then — its contexts and vectors kept (the context
cache, F345's vectors), so nothing is bought twice. The "nothing answered for `NO_PROGRESS_S`" failure carries
across parks (`stalled_since`). A rebuild step parks the same way. A busy document reader (Docling, one at a time)
gives the slot back too (`reader_busy`). Outside `parking()` — OSS — nothing changes: a wait is waited out in place.
`Parked` is a `BaseException`, as a cancel is, so every `except Exception` on the way lets it through.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass

#: The first wait, the longest, and how long a document waits with nothing answered before it fails.
RATE_WAIT_S = 15.0
RATE_WAIT_MAX_S = 60.0
NO_PROGRESS_S = 600.0
#: Inside `parking()`, how long one run of the work waits in place, all its waits together, before it gives its
#: slot back (Naveen's call 2 in D191: two minutes).
GIVE_BACK_AFTER_S = 120.0
#: A rate limit said in words, for a provider whose failure carries no status (Bedrock's `ThrottlingException`).
THROTTLED = re.compile(r"throttl|too ?many ?requests|rate.?limit|servicequotaexceeded", re.IGNORECASE)

_PACED: contextvars.ContextVar[bool] = contextvars.ContextVar("knowledge_paced", default=False)


class Parked(BaseException):
    """The work gives its slot back and asks to run again at `until` (epoch seconds) — see the module docstring.

    `reason`: `rate_limited` (a provider's limit, named by `provider_id` / `provider`) or `reader_busy` (the
    document reader is parsing another document). `stalled_since` (epoch seconds) is when the work was last
    answered, carried to its next run so the `NO_PROGRESS_S` failure still comes. A `BaseException`, as a cancel
    is: every `except Exception` on the way lets it through, and only the runner that entered `parking()`
    catches it."""

    def __init__(self, *, until: float, reason: str, provider_id: str = "", provider: str = "",
                 stalled_since: float | None = None) -> None:
        super().__init__(f"parked ({reason}) until {until:.0f}")
        self.until = until
        self.reason = reason
        self.provider_id = provider_id
        self.provider = provider
        self.stalled_since = stalled_since


@dataclass
class _Parking:
    stalled_since: float | None
    waited_s: float = 0.0


_PARKING: contextvars.ContextVar[_Parking | None] = contextvars.ContextVar("knowledge_parking", default=None)


@contextlib.contextmanager
def parking(*, stalled_since: float | None = None) -> Iterator[None]:
    """Inside the block, a long rate-limit wait (or a busy reader) raises `Parked` instead of holding the slot.
    `stalled_since`: when an earlier run of the same work was last answered (its `Parked.stalled_since`)."""
    token = _PARKING.set(_Parking(stalled_since=stalled_since))
    try:
        yield
    finally:
        _PARKING.reset(token)


def is_parking() -> bool:
    return _PARKING.get() is not None


def _carried_stall() -> float | None:
    state = _PARKING.get()
    return state.stalled_since if state is not None else None


def give_back(wait: float, *, model: str, answered_at: float) -> None:
    """Raise `Parked` when waiting `wait` more would take this run's waits past `GIVE_BACK_AFTER_S` — only inside
    `parking()`; otherwise count the wait and return. `answered_at` (epoch seconds) is when the work was last
    answered."""
    state = _PARKING.get()
    if state is None:
        return
    if state.waited_s + wait > GIVE_BACK_AFTER_S:
        provider_id, label = provider_of(model)
        raise Parked(until=time.time() + wait, reason="rate_limited", provider_id=provider_id, provider=label,
                     stalled_since=answered_at)
    state.waited_s += wait


def rate_limited(exc: BaseException) -> bool:
    """A provider saying "too many, slow down" — never an exhausted credit or a refused key."""
    from core.llm_providers import MODEL_ERROR, PROVIDER_UNAVAILABLE, RATE_LIMITED

    code = getattr(exc, "code", None)
    if code == RATE_LIMITED:
        return True
    return code in (MODEL_ERROR, PROVIDER_UNAVAILABLE) and bool(THROTTLED.search(str(exc)))


@contextlib.contextmanager
def paced() -> Iterator[None]:
    """Inside the block, an embedding rate limit is waited out instead of failing the call."""
    token = _PACED.set(True)
    try:
        yield
    finally:
        _PACED.reset(token)


def is_paced() -> bool:
    return _PACED.get()


def provider_of(model: str) -> tuple[str, str]:
    """The provider of `model` as (id, its own name), from the registry."""
    from core.providers.registry import provider_for

    spec = provider_for(model)
    return spec.id, spec.label


async def wait_out(seconds: float, *, model: str) -> None:
    """Sleep `seconds` for `model`'s provider's rate limit, telling the work's progress while it waits."""
    from core.knowledge import progress

    tracker = progress.current()
    token = None
    if tracker is not None:
        provider_id, label = provider_of(model)
        token = tracker.waiting(provider_id=provider_id, provider=label, seconds=seconds)
    try:
        await asyncio.sleep(seconds)
    finally:
        if tracker is not None and token is not None:
            tracker.resumed(token)


class Waiter:
    """One call's waits: each `RATE_WAIT_S` doubling to `RATE_WAIT_MAX_S`, until nothing has been answered for
    `NO_PROGRESS_S` — counted from an earlier run's last answer when the work was parked. Inside `parking()` a
    wait that would take the run past `GIVE_BACK_AFTER_S` raises `Parked`. Reads this module's values when it waits
    (a test changes them here)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.waits = 0
        self.waited_s = 0.0
        self._wait: float | None = None
        self._answered = time.monotonic()
        self._answered_at = time.time()
        carried = _carried_stall()
        if carried is not None:
            self._answered -= max(0.0, time.time() - carried)
            self._answered_at = carried

    def answered(self) -> None:
        self._answered = time.monotonic()
        self._answered_at = time.time()
        self._wait = None

    async def wait(self) -> bool:
        """Wait out a rate limit; False (give up) once nothing has been answered for `NO_PROGRESS_S`."""
        if time.monotonic() - self._answered >= NO_PROGRESS_S:
            return False
        wait = RATE_WAIT_S if self._wait is None else self._wait
        give_back(wait, model=self.model, answered_at=self._answered_at)
        self._wait = min(wait * 2, RATE_WAIT_MAX_S)
        self.waits += 1
        self.waited_s += wait
        await wait_out(wait, model=self.model)
        return True
