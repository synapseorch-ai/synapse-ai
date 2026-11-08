"""
The verification bar: two tenants, one worker process, jobs interleaved.

This is the claim the whole D29 refactor exists to make — that a single shared
fleet can run any tenant's job in any process, concurrently — and until now
nothing asserted it end to end. Three of the five axes were pinned in isolation
(`test_storage.py` for the cache, `test_no_env_secrets.py` for the environment,
`test_tenancy.py` for the ContextVar itself); the three that needed a running
worker were not pinned anywhere.

It runs in the fast suite, which was not obvious. `arq` and `fakeredis` are both
already installed by the deploy gate, and arq uses no Lua — only sorted-set
operations and WATCH/MULTI, all of which fakeredis supports. The single
incompatibility is `arq.worker.log_redis_info`, which issues `INFO Server`;
fakeredis does not implement it, so it is stubbed. Everything else is the real
`arq.worker.Worker`, the real job functions, and the real engine.

What is deliberately *not* faked: the store (SQLite in tmp_path), the
orchestration engine, the tenant ContextVar, the MCP pool, and the settings
provider. What is faked: Redis, the LLM (as everywhere in this suite), and MCP
sessions — spawning real stdio servers for two tenants would make this a
minute-long test to assert something `test_mcp_pool.py` already covers directly.
"""
import asyncio
import os
import subprocess
import sys

import pytest
from arq import ArqRedis
from arq.worker import Worker

import arq.worker as arq_worker
from core import settings_runtime, tenancy
from core.scale import context as scale_context
from core.scale import mcp_pool, worker as worker_mod
from core.store import resources
from core.store.settings import save_setting
from core.tenancy import tenant_scope
from core.tool_router import ToolRouter
from tests._fakes import fake_redis_stream as _frs
from tests._fakes import seed as _seed

QUEUE = "synapse:orchestrations"

A, B = "acme", "globex"

#: Shaped like a real key so a leak is unmistakable in a diff.
KEYS = {
    A: "-".join(["sk", "test", "acme", "0" * 24]),
    B: "-".join(["sk", "test", "globex", "1" * 24]),
}


class _StoreProvider:
    """A provider that answers from the store, which is already tenant-scoped.

    Every `multi_tenant` fixture in this suite uses a provider whose methods
    return `[]`, which is fine when the test is about the boundary itself. Here
    the tenants must genuinely resolve *different* agents, tools and MCP servers,
    and `core/scale/context.py` fails closed with no fallback to the store — so
    this delegates back to it explicitly.
    """

    async def resolve_agent(self, agent_id):
        return await resources.get_agent(agent_id)

    async def resolve_orchestration(self, orch_id):
        return await resources.get_orchestration(orch_id)

    async def resolve_custom_tools(self):
        return await resources.load_tools()

    async def resolve_mcp_servers(self):
        return await resources.load_mcp_servers()


class _TenantModule:
    """A server module carrying one tenant's MCP, without spawning anything."""

    def __init__(self, tenant):
        self.tenant = tenant
        self.agent_sessions = {}
        self.tool_router = ToolRouter()
        self.memory_store = None
        self.mcp_disabled = []
        self._session_tools = {}
        self.tool_router.register(f"{tenant}-server", "private_tool")
        self.tool_router.register("filesystem", "read_file")

    async def close(self):
        pass


@pytest.fixture
def fleet(monkeypatch):
    """A worker process serving two tenants, wired to nothing real but the store."""
    monkeypatch.setattr(arq_worker, "log_redis_info", _no_redis_info)

    async def _build(tenant):
        # The pool's own build is covered by test_mcp_pool.py; spawning eleven
        # stdio servers twice here would buy nothing and cost a minute.
        return _TenantModule(tenant)

    monkeypatch.setattr(mcp_pool, "_build", _build)
    mcp_pool.reset_state()

    settings_runtime.reset_state()
    settings_runtime.install_provider()
    scale_context.set_resource_provider(_StoreProvider())

    async def resolver(ctx, job_name, args, kwargs):
        # The tenant comes from the job id, not from a payload field: this is a
        # stand-in for an embedder reading the run row it minted under RLS, and
        # keeps the property that a caller cannot label its own run.
        job_id = ctx.get("job_id", "")
        return job_id.split("--", 1)[0] or None

    scale_context.set_job_tenant_resolver(resolver)

    yield

    scale_context.set_resource_provider(None)   # drops the resolver too
    settings_runtime.reset_state()
    from core.config import set_settings_provider

    set_settings_provider(None)


async def _no_redis_info(redis, log_func):
    """fakeredis has no INFO command; arq only uses it to log a banner."""


async def _seed_tenant(tenant: str, orch_id: str) -> None:
    """One tenant's world: its key, its custom tool, its orchestration."""
    with tenant_scope(tenant):
        await save_setting("openai_key", KEYS[tenant])
        await settings_runtime.refresh()
        await resources.save_tools([
            {
                "id": f"{tenant}_tool",
                "name": f"{tenant}_only",
                "description": f"belongs to {tenant}",
                "inputSchema": {"type": "object"},
            }
        ])
        await _seed.seed_orchestrations([
            _seed.make_orchestration(id=orch_id, name=f"{tenant} workflow")
        ])


async def _drain(ctx: dict, jobs: list[tuple[str, dict]], redis) -> None:
    """Enqueue and run to completion in one process, jobs interleaved."""
    pool = ArqRedis(connection_pool=redis.connection_pool, default_queue_name=QUEUE)
    for tenant, kwargs in jobs:
        await pool.enqueue_job(
            "run_orchestration_job",
            _queue_name=QUEUE,
            _job_id=f"{tenant}--{kwargs['run_id']}",
            **kwargs,
        )

    worker = Worker(
        functions=[worker_mod.run_orchestration_job],
        redis_pool=pool,
        queue_name=QUEUE,
        burst=True,
        handle_signals=False,
        max_jobs=4,
        poll_delay=0.01,
        ctx=ctx,
    )
    await worker.async_run()


class TestTwoTenantsOneProcess:
    async def test_each_run_carries_its_own_tenant(self, fleet, fake_redis, tmp_path):
        """max_jobs > 1, two tenants, one process: the D29 claim itself."""
        from sqlalchemy import select
        from core.store import get_store
        from core.store.models import OrchestrationRunDB

        session_factory = await get_store()
        await _seed_tenant(A, "orch_a")
        await _seed_tenant(B, "orch_b")

        ctx = {"redis": fake_redis, "session_factory": session_factory}
        await _drain(
            ctx,
            [
                (A, {"run_id": "run_a", "orch_id": "orch_a", "initial_input": "go"}),
                (B, {"run_id": "run_b", "orch_id": "orch_b", "initial_input": "go"}),
            ],
            fake_redis,
        )

        async with session_factory() as session:
            rows = {
                r.run_id: r
                for r in (await session.execute(select(OrchestrationRunDB))).scalars()
            }

        assert set(rows) == {"run_a", "run_b"}, f"both runs should exist: {sorted(rows)}"
        assert rows["run_a"].tenant_id == A
        assert rows["run_b"].tenant_id == B
        assert rows["run_a"].orchestration_id == "orch_a"
        assert rows["run_b"].orchestration_id == "orch_b"

        # And the print step actually ran, for each tenant's own orchestration.
        for run_id in ("run_a", "run_b"):
            assert rows[run_id].status == "completed", rows[run_id].status
            assert "greeting" in (rows[run_id].shared_state or {})

    async def test_neither_tenant_can_see_the_others_run(self, fleet, fake_redis):
        """`SharedState.restore` filters on the tenant, so this is free."""
        from core.orchestration.state import SharedState
        from core.store import get_store

        session_factory = await get_store()
        await _seed_tenant(A, "orch_a")

        ctx = {"redis": fake_redis, "session_factory": session_factory}
        await _drain(
            ctx, [(A, {"run_id": "run_a", "orch_id": "orch_a", "initial_input": "go"})], fake_redis
        )

        with tenant_scope(A):
            assert (await SharedState.restore("run_a")).run.run_id == "run_a"

        with tenant_scope(B):
            with pytest.raises(FileNotFoundError):
                await SharedState.restore("run_a")


class TestTheFiveAxes:
    """One job per tenant, asserting from inside the run while both are live."""

    async def _run_both(self, fake_redis, observe):
        from core.store import get_store

        session_factory = await get_store()
        await _seed_tenant(A, "orch_a")
        await _seed_tenant(B, "orch_b")

        ctx = {"redis": fake_redis, "session_factory": session_factory}
        await _drain(
            ctx,
            [
                (A, {"run_id": "run_a", "orch_id": "orch_a", "initial_input": "go"}),
                (B, {"run_id": "run_b", "orch_id": "orch_b", "initial_input": "go"}),
            ],
            fake_redis,
        )
        return observe

    async def test_axis_1_each_tenant_sees_its_own_model_key(self, fleet, fake_redis, monkeypatch):
        from core.config import load_settings

        seen = {}
        original = worker_mod._load_job_settings

        async def _spy():
            await original()
            # Read at three points around a yield, so a module global serving
            # both jobs would be caught rather than merely being unlikely.
            tenant = tenancy.get_tenant()
            first = load_settings().get("openai_key")
            await asyncio.sleep(0)
            second = load_settings().get("openai_key")
            seen[tenant] = (first, second)

        monkeypatch.setattr(worker_mod, "_load_job_settings", _spy)
        await self._run_both(fake_redis, seen)

        assert seen == {A: (KEYS[A], KEYS[A]), B: (KEYS[B], KEYS[B])}, seen

    async def test_axis_2_each_tenant_sees_only_its_own_custom_tools(self, fleet, fake_redis, monkeypatch):
        """`resolve_custom_tools()` is what an agent step calls before every
        model request. A print-only orchestration never reaches one, so this
        makes the same call from inside the job — same tenant context, same
        resolver, same fail-closed provider — rather than pretending a workflow
        with no agent step exercised it."""
        from core.scale.context import resolve_custom_tools

        seen = {}
        original = worker_mod._load_job_settings

        async def _spy():
            await original()
            tools = await resolve_custom_tools()
            await asyncio.sleep(0)
            again = await resolve_custom_tools()
            seen[tenancy.get_tenant()] = (
                sorted(t["name"] for t in tools),
                sorted(t["name"] for t in again),
            )

        monkeypatch.setattr(worker_mod, "_load_job_settings", _spy)
        await self._run_both(fake_redis, seen)

        assert seen == {
            A: ([f"{A}_only"], [f"{A}_only"]),
            B: ([f"{B}_only"], [f"{B}_only"]),
        }, seen

    async def test_axis_3_each_tenant_gets_its_own_tool_router(self, fleet, fake_redis, monkeypatch):
        """Phase 7's payoff. A hit for one tenant is a miss for the other."""
        seen = {}
        original = mcp_pool.acquire

        async def _spy(tenant=None):
            module = await original(tenant)
            await asyncio.sleep(0)
            seen[tenancy.get_tenant()] = (id(module), sorted(module.tool_router))
            return module

        monkeypatch.setattr(mcp_pool, "acquire", _spy)
        await self._run_both(fake_redis, seen)

        assert set(seen) == {A, B}
        assert seen[A][0] != seen[B][0], "both tenants were handed the same module"
        assert f"{A}-server__private_tool" in seen[A][1]
        assert f"{A}-server__private_tool" not in seen[B][1], "tenant A's MCP tool leaked to B"
        assert f"{B}-server__private_tool" in seen[B][1]
        # The shared native tool is present for both, which is the point of the split.
        assert "filesystem__read_file" in seen[A][1]
        assert "filesystem__read_file" in seen[B][1]

    async def test_axis_4_a_cache_hit_for_one_is_a_miss_for_the_other(self, fleet, fake_redis):
        """Already pinned by test_storage.py; re-asserted with runs in flight."""
        from core.cache import store as cache_store

        await self._run_both(fake_redis, None)

        key = cache_store.make_key("resp", "model-x", "what is our revenue?")
        with tenant_scope(A):
            cache_store.set("responses", key, "41")
        with tenant_scope(B):
            assert cache_store.get("responses", key) is None
        with tenant_scope(A):
            assert cache_store.get("responses", key)["value"] == "41"

    async def test_axis_5_no_key_reaches_the_environment_or_a_child(self, fleet, fake_redis, tmp_path):
        """`os.environ` is the real assertion; a child process is the proof.

        /proc/self/environ is a *weaker* check than os.environ — Linux fills it
        at execve and never updates it for a later setenv — so what actually
        matters is that a subprocess, which is what a stdio MCP server and a
        sandbox are, does not inherit either tenant's key.
        """
        await self._run_both(fake_redis, None)

        leaked = [k for k, v in os.environ.items() if v in KEYS.values()]
        assert not leaked, f"a provider key reached the environment: {leaked}"

        result = subprocess.run(
            [sys.executable, "-c", "import os; print('\\n'.join(os.environ.values()))"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for tenant, key in KEYS.items():
            assert key not in result.stdout, f"{tenant}'s key was inherited by a child process"
