"""
API keys are a table, looked up by hash.

They were a JSON file read whole on every authenticated request, so the auth
path's cost grew with the number of keys a user had ever created. As a row with
an indexed hash it is a point lookup.

The lookup is deliberately not tenant-scoped — a request arrives carrying only
the key, so this call is what *establishes* the tenant. These tests pin that,
and the thing it must not become: a key must not authenticate as a tenant that
does not own it.
"""
import pytest

from core import api_keys
from core.scale.context import set_resource_provider
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
async def test_a_generated_key_validates():
    raw, record = await api_keys.generate_api_key("ci")

    validated = await api_keys.validate_api_key(raw)
    assert validated is not None
    assert validated["id"] == record["id"]


@pytest.mark.asyncio
async def test_the_raw_key_is_never_stored():
    raw, _ = await api_keys.generate_api_key("ci")

    listed = await api_keys.list_api_keys()
    assert raw not in str(listed)
    assert all("key_hash" not in k for k in listed)


@pytest.mark.asyncio
async def test_a_revoked_key_stops_validating():
    raw, record = await api_keys.generate_api_key("ci")

    assert await api_keys.revoke_api_key(record["id"]) is True
    assert await api_keys.validate_api_key(raw) is None


@pytest.mark.asyncio
async def test_a_deleted_key_stops_validating():
    raw, record = await api_keys.generate_api_key("ci")

    assert await api_keys.delete_api_key(record["id"]) is True
    assert await api_keys.delete_api_key(record["id"]) is False
    assert await api_keys.validate_api_key(raw) is None


@pytest.mark.asyncio
async def test_garbage_is_rejected_without_touching_the_store():
    assert await api_keys.validate_api_key("") is None
    assert await api_keys.validate_api_key("not-a-synapse-key") is None


@pytest.mark.asyncio
async def test_validation_reports_the_owning_tenant(multi_tenant):
    """The key is the only thing a request carries, so it carries the tenant."""
    with tenant_scope("acme"):
        raw, _ = await api_keys.generate_api_key("acme's key")

    # Validated from outside any scope — this call is what establishes it.
    validated = await api_keys.validate_api_key(raw)
    assert validated["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_listing_shows_only_the_current_tenants_keys(multi_tenant):
    with tenant_scope("acme"):
        await api_keys.generate_api_key("acme's")
    with tenant_scope("globex"):
        await api_keys.generate_api_key("globex's")
        assert [k["name"] for k in await api_keys.list_api_keys()] == ["globex's"]

    with tenant_scope("acme"):
        assert [k["name"] for k in await api_keys.list_api_keys()] == ["acme's"]


@pytest.mark.asyncio
async def test_a_tenant_cannot_revoke_another_tenants_key(multi_tenant):
    with tenant_scope("acme"):
        raw, record = await api_keys.generate_api_key("acme's")

    with tenant_scope("globex"):
        assert await api_keys.revoke_api_key(record["id"]) is False
        assert await api_keys.delete_api_key(record["id"]) is False

    assert await api_keys.validate_api_key(raw) is not None


# ---------------------------------------------------------------------------
# The embedder hook
# ---------------------------------------------------------------------------
#
# A service that already has an API key table — one with its own tenancy column
# and its own row-level security — answers these five calls itself rather than
# the engine keeping a second table for the same job.


class _RecordingProvider:
    """Answers from a dict, and remembers what it was asked."""

    def __init__(self, records=None):
        self.records = dict(records or {})
        self.validated: list[str] = []

    async def validate(self, key_hash):
        self.validated.append(key_hash)
        return self.records.get(key_hash)

    async def generate(self, name):
        return "sk-syn-" + "e" * 32, {"id": "embedder-1", "name": name}

    async def list(self):
        return [{"id": "embedder-1", "name": "from the embedder"}]

    async def revoke(self, key_id):
        return key_id == "embedder-1"

    async def delete(self, key_id):
        return key_id == "embedder-1"


@pytest.fixture
def embedder():
    """Install a provider and take it out again.

    Nothing global resets `core.api_keys._provider`, so a test that leaves one
    installed silently reroutes every later test's key lookups.
    """
    provider = _RecordingProvider()
    api_keys.set_api_key_provider(provider)
    yield provider
    api_keys.set_api_key_provider(None)


@pytest.mark.asyncio
async def test_the_provider_answers_every_call(embedder):
    raw, record = await api_keys.generate_api_key("ci")
    assert record["id"] == "embedder-1"

    assert [k["name"] for k in await api_keys.list_api_keys()] == ["from the embedder"]
    assert await api_keys.revoke_api_key("embedder-1") is True
    assert await api_keys.delete_api_key("embedder-1") is True
    assert await api_keys.revoke_api_key("not-theirs") is False

    # Nothing reached the engine's own table — the raw key it minted is not
    # one the store has ever seen, and neither is the record's id.
    api_keys.set_api_key_provider(None)
    assert await api_keys.validate_api_key(raw) is None


@pytest.mark.asyncio
async def test_the_provider_sees_the_hash_and_never_the_key(embedder):
    raw = "sk-syn-" + "a" * 32
    embedder.records[api_keys._hash_key(raw)] = {"id": "k1", "tenant_id": "acme"}

    record = await api_keys.validate_api_key(raw)

    assert record["tenant_id"] == "acme"
    assert embedder.validated == [api_keys._hash_key(raw)]
    assert raw not in embedder.validated


@pytest.mark.asyncio
async def test_a_provider_that_says_no_is_the_final_answer(embedder):
    """No fallback to the local table. A fallback in the call that decides who
    you are is how an authorization failure becomes another tenant's session."""
    raw, _ = await api_keys.generate_api_key("real key in the store")
    api_keys.set_api_key_provider(None)
    real_raw, _ = await api_keys.generate_api_key("really in the store")
    api_keys.set_api_key_provider(embedder)

    # The store would happily validate this one. The provider is not asked to
    # care, and its "no" stands.
    assert await api_keys.validate_api_key(real_raw) is None


@pytest.mark.asyncio
async def test_the_provider_answer_is_not_cached(embedder):
    """The engine caches its own lookup because it can invalidate on revoke.
    An embedder owns both sides, so guessing a staleness window for it would be
    the engine deciding how long someone else's revocation may take."""
    raw = "sk-syn-" + "b" * 32
    embedder.records[api_keys._hash_key(raw)] = {"id": "k1", "tenant_id": "acme"}

    assert await api_keys.validate_api_key(raw) is not None
    embedder.records.clear()
    assert await api_keys.validate_api_key(raw) is None
    assert len(embedder.validated) == 2


@pytest.mark.asyncio
async def test_removing_the_provider_restores_the_engines_table():
    raw, record = await api_keys.generate_api_key("ours")

    api_keys.set_api_key_provider(_RecordingProvider())
    try:
        assert await api_keys.validate_api_key(raw) is None
    finally:
        api_keys.set_api_key_provider(None)

    assert (await api_keys.validate_api_key(raw))["id"] == record["id"]


@pytest.mark.asyncio
async def test_the_hook_cannot_open_multi_tenancy(embedder):
    """It names a source of records; it does not unlock `tenant_scope()`.

    Only `set_resource_provider()` does that — so a record naming a tenant is
    still unusable in the shipped single-tenant product.
    """
    from core.tenancy import SingleTenantError, is_multi_tenant

    assert is_multi_tenant() is False
    with pytest.raises(SingleTenantError):
        with tenant_scope("acme"):
            pass
