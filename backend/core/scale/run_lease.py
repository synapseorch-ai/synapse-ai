"""A run pumped in a task of its own holds a lease while its task lives (F470).

A run job's run is the job's: ARQ hands the job out again after a crash, the
job writes its worker onto the row, and its adapter polls the cancel key. A run
`runner.spawn_engine_run` pumps — a workflow agent's turn and its resume, a
workflow an agent hands work to, a golden set's case, a scheduled or channel
run — had none of that. Only its own process's `_active_tasks` knew it was
alive, so a worker killed under it left the row `running` for good, and nothing
read `synapse:cancel:<run_id>` for it, so Cancel never reached it.

Where the host shares Redis (scale mode, cloud), each such run — and each golden
set, which runs its cases the same way — holds `synapse:run-live:<id>` while its
task lives: set on the way in, renewed every `BEAT_SECONDS`, deleted on the way
out. A `running` row that no live worker owns and no lease covers is one nothing
is running any more; the sweeps end it (`heartbeat.sweep_orphaned_runs`, and a
host's own, which `on_hold` lets it arm). The same beat reads the cancel key, so
the engine stops at its next step boundary, as a run job's does.

Nothing is installed standalone: every pumped run lives in the API process
there, and dies with it, and the boot sweep and the in-process Cancel cover it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

LEASE_KEY = "synapse:run-live:{id}"
#: How long a lease outlives the last beat that renewed it: three beats, so two
#: missed renewals (a slow Redis, a long GC) are not a death.
LEASE_SECONDS = 90
BEAT_SECONDS = 30
#: How often a run's cancel key is read: a run job's adapter reads it as often.
CANCEL_POLL_SECONDS = 2.0

_redis: Any = None
_holder = ""
_on_hold: Callable[[str, str], Awaitable[None] | None] | None = None


def install(redis: Any, *, holder: str = "", on_hold: Callable[[str, str], Any] | None = None) -> None:
    """Hold leases in `redis` from now on (None stops). `on_hold(id, kind)` is told each one taken."""
    global _redis, _holder, _on_hold
    _redis, _holder, _on_hold = redis, holder or "", on_hold


def installed() -> bool:
    return _redis is not None


def key(id_: str) -> str:
    return LEASE_KEY.format(id=id_)


async def held(id_: str, redis: Any = None) -> bool:
    """Whether a live task holds `id_`'s lease. A Redis that cannot answer says yes: never end on a guess."""
    client = redis if redis is not None else _redis
    if client is None:
        return False
    try:
        return bool(await client.exists(key(id_)))
    except Exception as exc:  # noqa: BLE001
        _log.warning("[run_lease] could not read %s's lease (%s); taken as held", id_, type(exc).__name__)
        return True


@contextlib.asynccontextmanager
async def hold(id_: str, *, kind: str = "run"):
    """Hold `id_`'s lease for the life of the block; a no-op where none is installed.

    `kind` is "run" (its cancel key is read, and the engine told) or "set" (a
    golden set, whose Stop is a status on its row). Anything raised in the block
    goes through untouched — a hand-back (`HandedBack`) included.
    """
    redis = _redis
    if redis is None:
        yield
        return
    lease = key(id_)

    async def renew() -> None:
        try:
            await redis.set(lease, _holder, ex=LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 — the run goes on; the next beat tries again
            _log.warning("[run_lease] could not renew %s's lease: %s", id_, type(exc).__name__)

    await renew()
    if _on_hold is not None:
        try:
            told = _on_hold(id_, kind)
            if asyncio.iscoroutine(told):
                await told
        except Exception:  # noqa: BLE001 — a watch not armed is not a reason to stop the run
            _log.warning("[run_lease] on_hold failed for %s", id_, exc_info=True)

    stop = asyncio.Event()

    async def beat() -> None:
        renewed = time.monotonic()
        cancelled = kind != "run"
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CANCEL_POLL_SECONDS if not cancelled
                                       else BEAT_SECONDS)
            if stop.is_set():
                return
            if not cancelled:
                from core.scale.pubsub import is_cancelled
                try:
                    if await is_cancelled(redis, id_):
                        # The engine's own check between steps ends it `cancelled`
                        # (`engine.py`), as the in-process Cancel's flag does.
                        from core.orchestration.state import _cancelled_run_ids
                        _cancelled_run_ids.add(id_)
                        cancelled = True
                except Exception:  # noqa: BLE001 — a Redis hiccup is not a cancel
                    pass
            if time.monotonic() - renewed >= BEAT_SECONDS:
                await renew()
                renewed = time.monotonic()

    beater = asyncio.create_task(beat(), name=f"run-lease:{id_}")
    try:
        yield
    finally:
        stop.set()
        beater.cancel()
        with contextlib.suppress(BaseException):
            await beater
        try:
            await redis.delete(lease)
        except Exception as exc:  # noqa: BLE001 — it runs out on its own
            _log.warning("[run_lease] could not drop %s's lease: %s", id_, type(exc).__name__)
