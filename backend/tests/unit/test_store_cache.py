"""
The store's read-through cache.

Every ReAct turn resolves the tenant's custom tools before it can build the
model request, and every authenticated request looks up an API key. On a shared
fleet those are network round trips inside the latency a user feels.

Caching them is only safe if three things hold, which is what this pins: the
cache is keyed by tenant, a write is visible immediately to the process that
made it, and nothing lives past its TTL.
"""
import pytest

from core import api_keys
from core.scale.context import set_resource_provider
from core.store import cache, resources
from core.tenancy import tenant_scope


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


@pytest.mark.asyncio
async def test_a_repeat_read_does_not_hit_the_database():
    await resources.save_tool({"id": "t1", "name": "grep"})
    await resources.load_tools()

    calls = 0
    original = resources._list_uncached

    async def _counting(model):
        nonlocal calls
        calls += 1
        return await original(model)

    resources._list_uncached = _counting
    try:
        await resources.load_tools()
        await resources.load_tools()
    finally:
        resources._list_uncached = original

    assert calls == 0


@pytest.mark.asyncio
async def test_a_write_is_visible_to_the_very_next_read():
    """In-process invalidation, so a user's edit is never one turn behind."""
    await resources.save_tool({"id": "t1", "name": "grep"})
    assert len(await resources.load_tools()) == 1

    await resources.save_tool({"id": "t2", "name": "find"})
    assert len(await resources.load_tools()) == 2

    await resources.delete_tool("t1")
    assert [t["id"] for t in await resources.load_tools()] == ["t2"]


@pytest.mark.asyncio
async def test_the_cache_is_keyed_by_tenant(multi_tenant):
    """A cache that forgot the tenant would be a cross-tenant read."""
    with tenant_scope("acme"):
        await resources.save_tool({"id": "t1", "name": "acme's"})
        assert len(await resources.load_tools()) == 1

    with tenant_scope("globex"):
        assert await resources.load_tools() == []


@pytest.mark.asyncio
async def test_disabling_the_cache_is_honoured(monkeypatch):
    monkeypatch.setenv("SYNAPSE_STORE_CACHE_TTL", "0")
    cache.invalidate_all()

    await resources.save_tool({"id": "t1", "name": "grep"})
    await resources.load_tools()

    calls = 0
    original = resources._list_uncached

    async def _counting(model):
        nonlocal calls
        calls += 1
        return await original(model)

    resources._list_uncached = _counting
    try:
        await resources.load_tools()
    finally:
        resources._list_uncached = original

    assert calls == 1


@pytest.mark.asyncio
async def test_revoking_a_key_beats_the_cache(multi_tenant):
    """Revocation runs in the owning tenant's scope; the lookup does not."""
    with tenant_scope("acme"):
        raw, record = await api_keys.generate_api_key("acme's")

    assert await api_keys.validate_api_key(raw) is not None  # now cached

    with tenant_scope("acme"):
        await api_keys.revoke_api_key(record["id"])

    assert await api_keys.validate_api_key(raw) is None
