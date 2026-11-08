"""
Blob storage: tenant scoping, and the cache leak it closes.

The response and tool caches key on a content hash — model plus messages, or
tool name plus arguments. Keyed that way and stored in one directory, two
tenants asking the same question share an answer: a cross-tenant read on an
ordinary request, with no race and nothing unusual in the logs. Routing the
cache through a tenant-prefixed blob store is what closes it.

The prefix is applied inside the store rather than by callers, so these tests
exercise the property that actually holds in production: a caller cannot ask
for an unprefixed key even by mistake.
"""
import pytest

from core.scale.context import set_resource_provider
from core.storage import LocalBlobStore, get_blob_store, set_blob_store
from core.storage.base import tenant_key
from core.tenancy import tenant_scope


class _Provider:
    """Minimal resource provider — registering one is what unlocks tenancy."""

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


# ── tenant scoping ───────────────────────────────────────────────────────────

def test_keys_carry_the_tenant(multi_tenant):
    with tenant_scope("acme"):
        assert tenant_key("vault/x.txt") == "acme/vault/x.txt"
    with tenant_scope("globex"):
        assert tenant_key("vault/x.txt") == "globex/vault/x.txt"


def test_two_tenants_do_not_share_a_blob(tmp_path, multi_tenant):
    store = LocalBlobStore(tmp_path)

    with tenant_scope("acme"):
        store.put("notes.txt", "acme secret")
    with tenant_scope("globex"):
        assert store.get("notes.txt") is None      # same key, different tenant
        store.put("notes.txt", "globex secret")

    with tenant_scope("acme"):
        assert store.get("notes.txt") == "acme secret"
    with tenant_scope("globex"):
        assert store.get("notes.txt") == "globex secret"


def test_listing_shows_only_the_callers_blobs(tmp_path, multi_tenant):
    store = LocalBlobStore(tmp_path)
    with tenant_scope("acme"):
        store.put("a/1.txt", "x")
        store.put("a/2.txt", "y")
    with tenant_scope("globex"):
        store.put("a/3.txt", "z")
        assert store.list("a") == ["a/3.txt"]
    with tenant_scope("acme"):
        assert store.list("a") == ["a/1.txt", "a/2.txt"]


def test_deleting_does_not_reach_another_tenant(tmp_path, multi_tenant):
    store = LocalBlobStore(tmp_path)
    with tenant_scope("acme"):
        store.put("shared.txt", "keep me")
    with tenant_scope("globex"):
        store.put("shared.txt", "delete me")
        store.delete("shared.txt")
    with tenant_scope("acme"):
        assert store.get("shared.txt") == "keep me"


@pytest.mark.parametrize("key", ["../escape.txt", "a/../../escape.txt", "../../etc/passwd"])
def test_a_key_cannot_escape_the_store_root(tmp_path, key):
    """Keys come from tool output names and cache namespaces.

    Escaping the root also means escaping the tenant prefix, so this is a
    tenancy control and not only a path-traversal one.
    """
    store = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.put(key, "should not be written")


def test_writes_are_atomic(tmp_path):
    """A reader never sees a half-written blob, and a failed write leaves the
    previous version rather than a truncated one."""
    store = LocalBlobStore(tmp_path)
    store.put("f.txt", "first")
    store.put("f.txt", "second")
    assert store.get("f.txt") == "second"
    # No temp files left behind.
    assert not list(store.root.rglob("*.tmp"))


# ── the leak this closes ─────────────────────────────────────────────────────

def test_one_tenants_cached_answer_is_a_miss_for_another(tmp_path, monkeypatch, multi_tenant):
    """Axis 4 of the verification bar.

    `make_key` hashes only the content, so both tenants compute an identical
    cache key. Before the blob store that meant one lookup, one file, and org B
    reading org A's model output.
    """
    from core.cache import store as cache_store

    set_blob_store(LocalBlobStore(tmp_path))
    try:
        key = cache_store.make_key("resp", "model-x", "what is our revenue?")

        with tenant_scope("acme"):
            cache_store.set("responses", key, "acme revenue is 41")
            assert cache_store.get("responses", key)["value"] == "acme revenue is 41"

        with tenant_scope("globex"):
            assert cache_store.get("responses", key) is None
            cache_store.set("responses", key, "globex revenue is 7")

        with tenant_scope("acme"):
            assert cache_store.get("responses", key)["value"] == "acme revenue is 41"
    finally:
        set_blob_store(None)


def test_clearing_a_namespace_leaves_other_tenants_alone(tmp_path, multi_tenant):
    from core.cache import store as cache_store

    set_blob_store(LocalBlobStore(tmp_path))
    try:
        key = cache_store.make_key("t", "same")
        with tenant_scope("acme"):
            cache_store.set("ns", key, "acme")
        with tenant_scope("globex"):
            cache_store.set("ns", key, "globex")
            assert cache_store.clear_namespace("ns") == 1     # only its own
        with tenant_scope("acme"):
            assert cache_store.get("ns", key)["value"] == "acme"
    finally:
        set_blob_store(None)


def test_the_default_store_is_local():
    assert isinstance(get_blob_store(), LocalBlobStore)
