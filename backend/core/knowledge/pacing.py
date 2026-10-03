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
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import re
import time
from collections.abc import Iterator

#: The first wait, the longest, and how long a document waits with nothing answered before it fails.
RATE_WAIT_S = 15.0
RATE_WAIT_MAX_S = 60.0
NO_PROGRESS_S = 600.0
#: A rate limit said in words, for a provider whose failure carries no status (Bedrock's `ThrottlingException`).
THROTTLED = re.compile(r"throttl|too ?many ?requests|rate.?limit|servicequotaexceeded", re.IGNORECASE)

_PACED: contextvars.ContextVar[bool] = contextvars.ContextVar("knowledge_paced", default=False)


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
    `NO_PROGRESS_S`. Reads this module's values when it waits (a test changes them here)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.waits = 0
        self.waited_s = 0.0
        self._wait: float | None = None
        self._answered = time.monotonic()

    def answered(self) -> None:
        self._answered = time.monotonic()
        self._wait = None

    async def wait(self) -> bool:
        """Wait out a rate limit; False (give up) once nothing has been answered for `NO_PROGRESS_S`."""
        if time.monotonic() - self._answered >= NO_PROGRESS_S:
            return False
        wait = RATE_WAIT_S if self._wait is None else self._wait
        self._wait = min(wait * 2, RATE_WAIT_MAX_S)
        self.waits += 1
        self.waited_s += wait
        await wait_out(wait, model=self.model)
        return True
