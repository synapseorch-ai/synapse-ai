"""
One set of live MCP sessions per tenant, bounded, with idle eviction.

The worker used to call ``WorkerServerModule.build()`` once in ``on_startup`` and
hand the result to every job. One set of MCP sessions, opened from whatever
configuration existed at boot, served every tenant the process ran — which was
correct while a process belonged to one org, and is the last shared-process
assumption in the engine.

The unit is the module, not the session, because ``tool_router`` and
``agent_sessions`` are already per-instance. What was missing is a keyed cache
with a bound and a teardown.

Why bounded
-----------
A fleet sized for a thousand mostly-idle orgs must not hold a thousand live
session sets. Entries are evicted least-recently-used past ``POOL_SIZE``, and on
idle past ``IDLE_TTL`` regardless of size. What a tenant actually costs here is
small — stdio MCP is force-disabled in scale mode, so a tenant's own servers are
remote SSE connections, and the only subprocess is the Filesystem server rooted
at its vault.

**An entry in use is never evicted.** ``max_jobs`` defaults to 10, so several
jobs hold modules concurrently; closing one mid-call would kill the MCP sessions
under a running step.

Why each entry owns a task
--------------------------
anyio requires a cancel scope to be exited by the task that entered it. The
sessions are opened inside an ``AsyncExitStack``, so whichever task ran the build
must be the one to close it. Eviction is triggered by whoever happens to acquire
next — a different task — so an entry keeps a supervisor task that builds,
signals ready, waits, and closes in its own context.

`core/server.py`'s ``_filesystem_mcp_manager`` is the same pattern and carries
the scar that produced it: closing an MCP stack from an HTTP handler propagated a
CancelledError into the lifespan and tore down *every* session in the process.
"""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict

from core.tenancy import get_tenant

#: How many tenants' session sets may be live at once.
_DEFAULT_POOL_SIZE = 32

#: How long an unused entry is kept before its sessions are closed.
_DEFAULT_IDLE_TTL = 900.0

#: How long a caller waits for a tenant's session set to be built.
#:
#: Deliberately generous, because it is a **ceiling on the pathological case**
#: rather than a service-level target. A legitimate build is already bounded per
#: server by `REMOTE_MCP_CONNECT_TIMEOUT` (30s by default), and the tenant's
#: servers are connected in sequence — so an org with eight unreachable servers
#: legitimately spends four minutes here and must not be failed for it.
#:
#: What this catches is the class the per-server bound cannot: a vault hydration
#: that never returns, a resolver query that hangs, anything a future step adds
#: to the build without a bound of its own. Before it, such a build held the job
#: until `job_timeout` — an hour — with a `chat_sessions` row that never left
#: `running`. Five minutes turns that into a diagnosable failure with the
#: tenant's name in it.
_DEFAULT_BUILD_TIMEOUT = 300.0


def _pool_size() -> int:
    try:
        return max(1, int(os.getenv("SYNAPSE_MCP_POOL_SIZE", "") or _DEFAULT_POOL_SIZE))
    except (TypeError, ValueError):
        return _DEFAULT_POOL_SIZE


def _idle_ttl() -> float:
    try:
        return float(os.getenv("SYNAPSE_MCP_IDLE_TTL", "") or _DEFAULT_IDLE_TTL)
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_TTL


def _build_timeout() -> float:
    try:
        return float(
            os.getenv("SYNAPSE_MCP_BUILD_TIMEOUT", "") or _DEFAULT_BUILD_TIMEOUT
        )
    except (TypeError, ValueError):
        return _DEFAULT_BUILD_TIMEOUT


class _Entry:
    """One tenant's module, and the task that owns its lifetime."""

    __slots__ = ("tenant", "module", "refs", "idle_since", "task", "ready", "closing", "error")

    def __init__(self, tenant: str):
        self.tenant = tenant
        self.module = None
        self.refs = 0
        self.idle_since: float | None = None
        self.task: asyncio.Task | None = None
        self.ready = asyncio.Event()
        self.closing = asyncio.Event()
        self.error: BaseException | None = None


#: tenant -> entry, in least-recently-acquired order.
_entries: "OrderedDict[str, _Entry]" = OrderedDict()

#: Guards `_entries`. Held only across bookkeeping, never across a build.
_lock: asyncio.Lock | None = None

#: The process-wide native tool servers every tenant shares.
_shared = None


def _get_lock() -> asyncio.Lock:
    # Created lazily: a Lock binds to the running loop, and this module is
    # imported long before the worker's loop exists.
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def set_shared(shared) -> None:
    """Install the process-wide native session set, from worker startup."""
    global _shared
    _shared = shared


def get_shared():
    return _shared


async def _build(tenant: str):
    """Build one tenant's module, with the tenant established in this task."""
    from core import tenancy
    from core.scale.worker_server_module import WorkerServerModule

    if tenancy.is_multi_tenant() and tenancy.get_tenant() != tenant:
        with tenancy.tenant_scope(tenant):
            return await WorkerServerModule.build_for_tenant(_shared)
    return await WorkerServerModule.build_for_tenant(_shared)


async def _own(entry: _Entry) -> None:
    """Build, stay alive, then close — all in one task. See the module docstring."""
    try:
        entry.module = await _build(entry.tenant)
    except BaseException as exc:  # noqa: BLE001 — reported to the acquirer
        entry.error = exc
        entry.ready.set()
        return

    entry.ready.set()
    try:
        await entry.closing.wait()
    finally:
        # Unregister *before* tearing down, and unconditionally.
        #
        # The invariant this restores: an entry reachable from `_entries` is one
        # `acquire()` can use. Teardown used to rely entirely on whoever set
        # `closing` having popped the entry first, which is true for `_reap` and
        # `drain` and false for the case that actually happened — this task
        # being cancelled by an MCP client's task group. The entry stayed cached
        # with `ready` set, no error and `module` None, and every subsequent job
        # for that tenant died instantly on `'NoneType' object has no attribute
        # 'agent_sessions'`. Permanently: nothing rebuilds an entry that is
        # still in the map.
        #
        # `is entry` rather than a bare delete: a fresh entry may already have
        # taken this tenant's slot, and it must not be evicted by its
        # predecessor's teardown.
        async with _get_lock():
            if _entries.get(entry.tenant) is entry:
                del _entries[entry.tenant]

        module = entry.module
        entry.module = None
        if module is not None:
            await module.close()


async def acquire(tenant: str | None = None):
    """This tenant's server module, building it if the pool does not hold one.

    Every call must be paired with `release()`, or the entry is pinned forever
    and the pool stops being bounded.
    """
    tenant = tenant or get_tenant()
    lock = _get_lock()
    stale: _Entry | None = None

    async with lock:
        entry = _entries.get(tenant)

        # An entry past its idle TTL is rebuilt here rather than left to `_reap`.
        #
        # `_reap` runs at the *end* of this function, after `refs` has been
        # incremented, and only ever dooms entries with `refs == 0` — so it can
        # never evict the entry it is being called from. A tenant that is the
        # only active one on a worker therefore keeps its module **forever**:
        # observed serving MCP sessions built twenty-two minutes into a
        # fifteen-minute TTL, because nobody else's acquire ever came along to
        # reap it.
        #
        # That is not merely a stale-cache curiosity. It is why connecting an
        # MCP server did nothing: the customer authorised it, the row said
        # `connected`, and the fleet kept handing out a module built before it
        # existed. `invalidate()` below is the fast path for that; this is the
        # bound underneath it.
        if entry is not None and _is_stale(entry):
            del _entries[tenant]
            stale = entry
            entry = None

        if entry is None:
            entry = _Entry(tenant)
            _entries[tenant] = entry
            # Detached from the spawning job's trace and log ids: the
            # connection outlives it and serves the tenant's later jobs.
            from core.telemetry import detached_context
            entry.task = asyncio.create_task(
                _own(entry), name=f"mcp-pool[{tenant}]", context=detached_context()
            )
        entry.refs += 1
        entry.idle_since = None
        _entries.move_to_end(tenant)

    if stale is not None:
        # Outside the lock: teardown latency belongs to the tenant being
        # replaced, not to the lock every other tenant is waiting on.
        _close(stale)

    try:
        await asyncio.wait_for(entry.ready.wait(), timeout=_build_timeout())
    except asyncio.TimeoutError:
        # The build is wedged. Three things have to happen, in this order.
        #
        # Drop the entry, so the tenant's *next* job builds a fresh one instead
        # of queueing behind this one and inheriting the same wait. Cancel the
        # owner task, because nothing else will — it is stuck inside `_build`,
        # not waiting on `closing`, so setting `closing` would be ignored;
        # `_own` catches the cancellation, records it and wakes anyone else
        # already waiting on `ready`, and `build_for_tenant` closes whatever it
        # had opened. Then raise, rather than return a module the caller would
        # immediately dereference.
        async with lock:
            entry.refs = max(0, entry.refs - 1)
            if _entries.get(tenant) is entry:
                del _entries[tenant]
        if entry.task is not None:
            entry.task.cancel()
        raise TimeoutError(
            f"MCP session set for tenant '{tenant}' was not built within "
            f"{_build_timeout():.0f}s — a server is accepting connections "
            f"without answering, or a step of the build has no bound of its own"
        )

    if entry.error is not None:
        async with lock:
            entry.refs -= 1
            # A build that failed is not cached: the next job for this tenant
            # should try again rather than inherit a permanent failure, which is
            # what a transient MCP outage would otherwise become.
            if _entries.get(tenant) is entry:
                del _entries[tenant]
            entry.closing.set()
        raise entry.error

    if entry.module is None:
        # Belt and braces behind the invariant above: `ready` is set, no error
        # was recorded, and there is still nothing to hand back. Drop it and
        # build again rather than return `None` to a caller whose very next line
        # is `server_module.agent_sessions`.
        async with lock:
            entry.refs = max(0, entry.refs - 1)
            if _entries.get(tenant) is entry:
                del _entries[tenant]
            entry.closing.set()
        return await acquire(tenant)

    await _reap()
    return entry.module


def _is_stale(entry: _Entry) -> bool:
    """True when nobody holds this entry and it has sat past the idle TTL."""
    return (
        entry.refs == 0
        and entry.idle_since is not None
        and (time.monotonic() - entry.idle_since) > _idle_ttl()
    )


async def invalidate(tenant: str | None = None) -> None:
    """Drop a tenant's cached module so the next job rebuilds it.

    For when what the module was built *from* has changed: a server connected or
    authorised, a credential rotated, a server removed. Without it the customer
    waits out the idle TTL — and, before the staleness check in `acquire()`,
    could wait forever, because a tenant's own acquire never reaps its own
    entry.

    Safe while a job holds the entry. The map loses it immediately so the next
    acquire builds fresh, and the owner task closes the old sessions once the
    jobs still using them let go — which is the same reason eviction is a task's
    job rather than the caller's.
    """
    tenant = tenant or get_tenant()
    async with _get_lock():
        entry = _entries.pop(tenant, None)
    if entry is not None:
        _close(entry)


async def release(tenant: str | None = None) -> None:
    """Give back what `acquire()` handed out. Safe to call more than once."""
    tenant = tenant or get_tenant()
    async with _get_lock():
        entry = _entries.get(tenant)
        if entry is None:
            return
        entry.refs = max(0, entry.refs - 1)
        if entry.refs == 0:
            entry.idle_since = time.monotonic()


async def _reap() -> None:
    """Evict idle entries past the TTL, then the oldest unused past the bound."""
    now = time.monotonic()
    ttl = _idle_ttl()
    size = _pool_size()
    doomed: list[_Entry] = []

    async with _get_lock():
        for tenant, entry in list(_entries.items()):
            if entry.refs == 0 and entry.idle_since is not None and (now - entry.idle_since) > ttl:
                doomed.append(_entries.pop(tenant))

        while len(_entries) > size:
            for tenant, entry in _entries.items():
                if entry.refs == 0:
                    doomed.append(_entries.pop(tenant))
                    break
            else:
                # Every entry is in use. Over the bound and nothing to give up —
                # the alternative is closing MCP sessions under a running step.
                break

    for entry in doomed:
        _close(entry)


def _close(entry: _Entry) -> None:
    """Ask an entry's owner task to tear itself down. Does not wait."""
    entry.closing.set()
    if entry.task is not None:
        # Teardown latency belongs to the evicted tenant, not to the job that
        # happened to trigger the reap. Consume the result so a failure is not
        # reported as an un-retrieved task exception.
        entry.task.add_done_callback(lambda t: t.cancelled() or t.exception())


async def drain() -> None:
    """Close every entry and wait for it. From worker shutdown, and from tests."""
    async with _get_lock():
        entries = list(_entries.values())
        _entries.clear()

    for entry in entries:
        entry.closing.set()

    for entry in entries:
        if entry.task is not None:
            try:
                await entry.task
            except Exception:
                pass


def stats() -> dict:
    """What the pool is holding. For tests and for a future health endpoint."""
    return {
        "size": len(_entries),
        "limit": _pool_size(),
        "idle_ttl": _idle_ttl(),
        "tenants": {
            tenant: {"refs": entry.refs, "idle": entry.idle_since is not None}
            for tenant, entry in _entries.items()
        },
    }


def reset_state() -> None:
    """Forget everything without closing anything. For tests only."""
    global _shared, _lock
    _entries.clear()
    _shared = None
    _lock = None
