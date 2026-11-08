"""
The per-tenant MCP pool: bounded, evicted, and never closed under a live job.

The worker built one `WorkerServerModule` at startup and handed it to every job.
This is what replaces that, and the properties below are the ones a shared fleet
actually depends on:

* two tenants get two modules, and a hit for one is a miss for the other;
* an entry someone is holding is never evicted, whatever the pressure — closing
  it would kill the MCP sessions under a running step;
* teardown happens on the task that ran the build, because anyio requires a
  cancel scope to be exited by the task that entered it, and `core/server.py`'s
  `_filesystem_mcp_manager` documents what happens when it is not.

Real MCP sessions are not involved: `build_for_tenant` is replaced with a fake
that records which task built it and which closed it, which is the only way to
observe the property in the third bullet.
"""
import asyncio

import pytest

from core.scale import mcp_pool
from core.scale.context import set_resource_provider
from core.tenancy import tenant_scope
from core.tool_router import ToolRouter


class _Provider:
    async def resolve_agent(self, agent_id):
        return None

    async def resolve_orchestration(self, orch_id):
        return None

    async def resolve_custom_tools(self):
        return []

    async def resolve_mcp_servers(self):
        return []


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


class _FakeModule:
    """Stands in for a WorkerServerModule, recording its own lifecycle."""

    def __init__(self, tenant):
        self.tenant = tenant
        self.agent_sessions = {}
        self.tool_router = ToolRouter()
        self.memory_store = None
        self.mcp_disabled = []
        self._session_tools = {}
        self.built_on = asyncio.current_task()
        self.closed_on = None
        self.tool_router.register(tenant, "only_mine")

    async def close(self):
        self.closed_on = asyncio.current_task()


@pytest.fixture
async def pool(monkeypatch):
    """A clean pool whose builds are instant and observable."""
    built = []

    async def _fake_build(tenant):
        from core import tenancy

        # Mirrors the real _build: the tenant must be established in this task,
        # because that is what the Filesystem server's vault root is read from.
        assert tenancy.get_tenant() == tenant, "the owner task built under the wrong tenant"
        await asyncio.sleep(0)
        module = _FakeModule(tenant)
        built.append(module)
        return module

    mcp_pool.reset_state()
    monkeypatch.setattr(mcp_pool, "_build", _fake_build)
    yield built
    await mcp_pool.drain()
    mcp_pool.reset_state()


class TestOneSetPerTenant:
    async def test_two_tenants_get_two_modules(self, pool, multi_tenant):
        with tenant_scope("acme"):
            acme = await mcp_pool.acquire()
        with tenant_scope("globex"):
            globex = await mcp_pool.acquire()

        assert acme is not globex
        assert acme.tenant == "acme"
        assert globex.tenant == "globex"

    async def test_a_hit_for_one_tenant_is_a_miss_for_another(self, pool, multi_tenant):
        """Axis 3 of the verification bar, at the pool's own level."""
        with tenant_scope("acme"):
            first = await mcp_pool.acquire()
            await mcp_pool.release()
            again = await mcp_pool.acquire()
            await mcp_pool.release()

        assert again is first, "the second acquire should have been a cache hit"
        assert len(pool) == 1

        with tenant_scope("globex"):
            other = await mcp_pool.acquire()
            await mcp_pool.release()

        assert other is not first
        assert len(pool) == 2, "the other tenant should have forced a build"

    async def test_each_tenants_router_holds_only_its_own(self, pool, multi_tenant):
        with tenant_scope("acme"):
            acme = await mcp_pool.acquire()
        with tenant_scope("globex"):
            globex = await mcp_pool.acquire()

        assert "acme__only_mine" in acme.tool_router
        assert "acme__only_mine" not in globex.tool_router
        assert "globex__only_mine" in globex.tool_router

    async def test_concurrent_acquires_for_one_tenant_build_once(self, pool, multi_tenant):
        """Two jobs for the same tenant must not each spawn a session set."""

        async def job():
            with tenant_scope("acme"):
                module = await mcp_pool.acquire()
                await asyncio.sleep(0)
                await mcp_pool.release()
                return module

        a, b = await asyncio.gather(job(), job())

        assert a is b
        assert len(pool) == 1


class TestEviction:
    async def test_an_entry_in_use_is_never_evicted(self, pool, multi_tenant, monkeypatch):
        """The property that makes `max_jobs > 1` safe.

        Evicting a module a running job holds would close its MCP sessions
        mid-call.
        """
        monkeypatch.setenv("SYNAPSE_MCP_POOL_SIZE", "1")

        with tenant_scope("acme"):
            held = await mcp_pool.acquire()  # deliberately not released

        for tenant in ("globex", "initech", "umbrella"):
            with tenant_scope(tenant):
                await mcp_pool.acquire()

        assert held.closed_on is None, "an entry with a live refcount was torn down"
        assert mcp_pool.stats()["size"] == 4, "nothing was evictable, so nothing should go"

        with tenant_scope("acme"):
            await mcp_pool.release()
        for tenant in ("globex", "initech", "umbrella"):
            with tenant_scope(tenant):
                await mcp_pool.release()

    async def test_the_oldest_idle_entry_goes_first(self, pool, multi_tenant, monkeypatch):
        monkeypatch.setenv("SYNAPSE_MCP_POOL_SIZE", "2")

        modules = {}
        for tenant in ("acme", "globex"):
            with tenant_scope(tenant):
                modules[tenant] = await mcp_pool.acquire()
                await mcp_pool.release()

        with tenant_scope("initech"):
            await mcp_pool.acquire()
            await mcp_pool.release()

        await mcp_pool.drain()

        assert modules["acme"].closed_on is not None, "the least recent should have gone"
        assert "acme" not in mcp_pool.stats()["tenants"]

    async def test_touching_an_entry_makes_it_recent(self, pool, multi_tenant, monkeypatch):
        monkeypatch.setenv("SYNAPSE_MCP_POOL_SIZE", "2")

        for tenant in ("acme", "globex"):
            with tenant_scope(tenant):
                await mcp_pool.acquire()
                await mcp_pool.release()

        with tenant_scope("acme"):  # acme is now the most recently used
            await mcp_pool.acquire()
            await mcp_pool.release()

        with tenant_scope("initech"):
            await mcp_pool.acquire()
            await mcp_pool.release()

        assert set(mcp_pool.stats()["tenants"]) == {"acme", "initech"}

    async def test_an_idle_entry_goes_when_its_ttl_expires(self, pool, multi_tenant, monkeypatch):
        monkeypatch.setenv("SYNAPSE_MCP_IDLE_TTL", "0")

        with tenant_scope("acme"):
            acme = await mcp_pool.acquire()
            await mcp_pool.release()

        # Any later acquire reaps; a fleet of mostly-idle orgs should not hold
        # session sets for orgs that stopped sending work hours ago.
        with tenant_scope("globex"):
            await mcp_pool.acquire()
            await mcp_pool.release()

        await asyncio.sleep(0)
        assert acme.closed_on is not None
        assert "acme" not in mcp_pool.stats()["tenants"]

    async def test_an_evicted_tenant_gets_a_fresh_module(self, pool, multi_tenant, monkeypatch):
        monkeypatch.setenv("SYNAPSE_MCP_POOL_SIZE", "1")

        with tenant_scope("acme"):
            first = await mcp_pool.acquire()
            await mcp_pool.release()
        with tenant_scope("globex"):
            await mcp_pool.acquire()
            await mcp_pool.release()
        with tenant_scope("acme"):
            second = await mcp_pool.acquire()
            await mcp_pool.release()

        assert second is not first


class TestTeardownOwnership:
    async def test_close_runs_on_the_task_that_built_it(self, pool, multi_tenant):
        """anyio requires the entering task to exit the cancel scope.

        `core/server.py:310-321` records what happened when this was violated:
        closing an MCP stack from an HTTP handler propagated a CancelledError
        into the lifespan and tore down every session in the process.
        """
        with tenant_scope("acme"):
            module = await mcp_pool.acquire()
            await mcp_pool.release()

        acquiring_task = asyncio.current_task()
        await mcp_pool.drain()

        assert module.closed_on is not None, "it was never closed"
        assert module.closed_on is module.built_on, "closed on a different task than it was built on"
        assert module.built_on is not acquiring_task, "the build should own its own task"

    async def test_drain_waits_for_teardown(self, pool, multi_tenant):
        for tenant in ("acme", "globex"):
            with tenant_scope(tenant):
                await mcp_pool.acquire()
                await mcp_pool.release()

        await mcp_pool.drain()

        assert all(m.closed_on is not None for m in pool)
        assert mcp_pool.stats()["size"] == 0


class TestFailures:
    async def test_a_failed_build_is_not_cached(self, multi_tenant, monkeypatch):
        """A transient MCP outage must not become permanent for that tenant."""
        mcp_pool.reset_state()
        attempts = []

        async def _flaky(tenant):
            attempts.append(tenant)
            if len(attempts) == 1:
                raise RuntimeError("mcp is having a moment")
            return _FakeModule(tenant)

        monkeypatch.setattr(mcp_pool, "_build", _flaky)
        try:
            with tenant_scope("acme"):
                with pytest.raises(RuntimeError):
                    await mcp_pool.acquire()

                assert mcp_pool.stats()["size"] == 0, "a failed build was left in the pool"

                module = await mcp_pool.acquire()
                assert module.tenant == "acme"
                await mcp_pool.release()
        finally:
            await mcp_pool.drain()
            mcp_pool.reset_state()

    async def test_releasing_an_unknown_tenant_is_harmless(self, pool, multi_tenant):
        with tenant_scope("nobody"):
            await mcp_pool.release()


class TestSingleTenantDefault:
    async def test_the_shipped_product_holds_exactly_one_entry(self, pool):
        """No provider registered: `get_tenant()` is a constant, so is the pool."""
        first = await mcp_pool.acquire()
        await mcp_pool.release()
        second = await mcp_pool.acquire()
        await mcp_pool.release()

        assert first is second
        assert mcp_pool.stats()["size"] == 1


class TestATornDownEntryIsNeverHandedOut:
    """The invariant: an entry reachable from `_entries` is one you can use.

    Teardown used to rely entirely on whoever set `closing` having popped the
    entry first. That holds for `_reap` and for `drain`, and it did not hold for
    the case that actually happened: the owner task cancelled from underneath,
    by an MCP client's `anyio` task group losing a child. The `finally` nulled
    `module` and left the entry cached with `ready` set and no error, so
    `acquire()` handed back `None` and every later job for that tenant died on
    `'NoneType' object has no attribute 'agent_sessions'` — permanently, because
    nothing rebuilds an entry that is still in the map.
    """

    async def test_a_cancelled_owner_task_does_not_poison_the_tenant(
        self, pool, multi_tenant
    ):
        with tenant_scope("t1"):
            first = await mcp_pool.acquire()
            assert first is not None
            await mcp_pool.release()

            # What an MCP client's task group does to its host, reproduced.
            entry = mcp_pool._entries["t1"]  # noqa: SLF001
            entry.task.cancel()
            await asyncio.sleep(0.05)

            # The entry unregistered itself on the way down...
            assert "t1" not in mcp_pool._entries, (  # noqa: SLF001
                "a torn-down entry stayed reachable and will be handed out"
            )

            # ...so the next acquire builds a fresh one rather than returning None.
            second = await mcp_pool.acquire()
            assert second is not None, "acquire() returned a module-less entry"
            assert second is not first
            assert second.agent_sessions is not None
            await mcp_pool.release()

    async def test_acquire_rebuilds_rather_than_return_none(self, pool, multi_tenant):
        """Belt and braces behind the invariant, forced directly.

        If some future path ever leaves a `ready`, no-error, no-module entry in
        the map, the caller must not be the one to discover it — its very next
        line is `server_module.agent_sessions`.
        """
        with tenant_scope("t2"):
            first = await mcp_pool.acquire()
            await mcp_pool.release()

            entry = mcp_pool._entries["t2"]  # noqa: SLF001
            entry.module = None  # the impossible state, made real

            second = await mcp_pool.acquire()
            assert second is not None
            assert second is not first
            await mcp_pool.release()


class TestStaleness:
    """A module is only as good as what it was built from.

    Both of these were reported as "I connected an MCP server and the agent says
    it does not have that tool" — accurately, of the module it had been handed.
    """

    async def test_a_lone_tenants_entry_is_not_immortal(self, pool, multi_tenant, monkeypatch):
        """`_reap` cannot evict the entry it is called from.

        It runs at the end of `acquire`, after `refs` has been incremented, and
        only dooms entries with `refs == 0`. So a tenant that is the only active
        one on a worker kept its module forever — observed still serving
        twenty-two minutes into a fifteen-minute TTL, because nobody else's
        acquire ever came along to reap it.
        """
        monkeypatch.setattr(mcp_pool, "_idle_ttl", lambda: 0.01)

        with tenant_scope("lonely"):
            first = await mcp_pool.acquire()
            await mcp_pool.release()
            await asyncio.sleep(0.05)  # past the TTL

            second = await mcp_pool.acquire()
            await mcp_pool.release()

        assert second is not first, (
            "the same tenant acquiring again kept a module past its idle TTL"
        )

    async def test_a_held_entry_is_never_replaced_underneath_a_job(
        self, pool, multi_tenant, monkeypatch
    ):
        """Staleness must not become a way to close sessions under a running step."""
        monkeypatch.setattr(mcp_pool, "_idle_ttl", lambda: 0.01)

        with tenant_scope("busy"):
            held = await mcp_pool.acquire()          # refs == 1, never released
            await asyncio.sleep(0.05)
            again = await mcp_pool.acquire()          # refs == 2
            assert again is held, "an entry in use was rebuilt under its holder"
            await mcp_pool.release()
            await mcp_pool.release()

    async def test_invalidate_forces_a_rebuild(self, pool, multi_tenant):
        """The fast path for "this tenant's configuration just moved"."""
        with tenant_scope("changed"):
            first = await mcp_pool.acquire()
            await mcp_pool.release()

            await mcp_pool.invalidate("changed")

            second = await mcp_pool.acquire()
            await mcp_pool.release()

        assert second is not first
        assert first.closed_on is not None, "the replaced module was never closed"

    async def test_invalidate_is_safe_while_a_job_holds_the_entry(self, pool, multi_tenant):
        """The map loses it immediately so the next job builds fresh; the owner
        task closes the old sessions once the holders let go."""
        with tenant_scope("midflight"):
            held = await mcp_pool.acquire()
            await mcp_pool.invalidate("midflight")

            # The holder keeps a working module.
            assert held.agent_sessions is not None

            fresh = await mcp_pool.acquire()
            assert fresh is not held, "the next acquire reused an invalidated entry"
            await mcp_pool.release()
            await mcp_pool.release()

    async def test_invalidating_a_tenant_with_nothing_cached_is_a_no_op(
        self, pool, multi_tenant
    ):
        await mcp_pool.invalidate("never-seen")


class TestABuildThatNeverFinishes:
    """`acquire()` used to wait on `entry.ready` with no bound at all.

    The per-server `REMOTE_MCP_CONNECT_TIMEOUT` bounds each connect, which is
    the case that was actually seen. It does not bound the rest of the build:
    vault hydration, the resolver query, or anything a later step adds without a
    bound of its own. One of those hanging held the job until `job_timeout` —
    an hour — against a `chat_sessions` row that never left `running`.

    The timeout is the ceiling on that class, not a service-level target, so
    these drive it through the environment variable rather than waiting on the
    real five minutes.
    """

    async def test_a_wedged_build_raises_instead_of_waiting(
        self, monkeypatch, multi_tenant
    ):
        monkeypatch.setenv("SYNAPSE_MCP_BUILD_TIMEOUT", "0.2")

        async def never_finishes(tenant):
            await asyncio.sleep(3600)

        monkeypatch.setattr(mcp_pool, "_build", never_finishes)

        with tenant_scope("t-wedged"):
            with pytest.raises(TimeoutError, match="t-wedged"):
                await mcp_pool.acquire()

    async def test_the_wedged_entry_is_dropped_so_the_next_job_starts_fresh(
        self, monkeypatch, multi_tenant
    ):
        """Otherwise every later job queues behind the same wedged build and
        inherits the same wait — the failure would spread rather than stay put."""
        monkeypatch.setenv("SYNAPSE_MCP_BUILD_TIMEOUT", "0.2")

        async def never_finishes(tenant):
            await asyncio.sleep(3600)

        monkeypatch.setattr(mcp_pool, "_build", never_finishes)

        with tenant_scope("t-wedged"):
            with pytest.raises(TimeoutError):
                await mcp_pool.acquire()

        assert "t-wedged" not in mcp_pool._entries  # noqa: SLF001

    async def test_the_owner_task_is_stopped_rather_than_left_running(
        self, monkeypatch, multi_tenant
    ):
        """Nothing else would stop it: it is inside `_build`, not waiting on
        `closing`, so setting `closing` would never be seen and the task would
        hold its half-built sessions until the process exited.

        It finishes rather than ending up `cancelled()`, and that is the design:
        `_own` catches the cancellation, records it on the entry and sets
        `ready`, so anyone *already* waiting on this entry is woken with the
        reason instead of waiting out their own timeout too.
        """
        monkeypatch.setenv("SYNAPSE_MCP_BUILD_TIMEOUT", "0.2")
        started: list[asyncio.Task] = []
        entries: list = []

        async def never_finishes(tenant):
            started.append(asyncio.current_task())
            entries.append(mcp_pool._entries[tenant])  # noqa: SLF001
            await asyncio.sleep(3600)

        monkeypatch.setattr(mcp_pool, "_build", never_finishes)

        with tenant_scope("t-wedged"):
            with pytest.raises(TimeoutError):
                await mcp_pool.acquire()

        await asyncio.sleep(0.05)
        assert started[0].done(), "the wedged owner task is still running"
        assert entries[0].ready.is_set(), "other waiters were left hanging"
        assert isinstance(entries[0].error, asyncio.CancelledError)

    async def test_a_build_inside_the_bound_is_untouched(
        self, monkeypatch, multi_tenant, pool
    ):
        """The bound must not fail a slow-but-legitimate build. An org with
        several unreachable servers spends real minutes here by design."""
        monkeypatch.setenv("SYNAPSE_MCP_BUILD_TIMEOUT", "5")

        with tenant_scope("t-slow"):
            module = await mcp_pool.acquire()
            assert module is not None
            await mcp_pool.release()
