"""
The vault is a directory, or it is an object store, and the code says which.

Open source installs on a laptop: the vault is a folder, every filesystem
operation works because there is a filesystem, and nothing is uploaded anywhere.
Cloud runs on pods that disappear: the object store is the vault and the folder
is a working copy, so a pod dying costs a re-download rather than the data.

Both have to work, and the choice must not be a mode flag anyone can get wrong —
it is asked of the blob store. A store that can give a path gets `LocalVault`; a
store that cannot gets `S3Vault`. `S3BlobStore.path_for()` returns None for
exactly this reason.

What this fixes, concretely: `_vault_root()` used to fall back to a plain
`LocalBlobStore()` whenever the store had no path, so a deployment on object
storage wrote its vault to the pod's own disk — durably, silently, and only
until the pod was replaced. `maybe_vault` did upload a copy to S3, but through
`core.s3_storage.get_s3()` rather than the blob store (a second answer to "is
this cloud"), and under `except Exception: pass`, so a failed upload still told
the model its output was saved.
"""
import pytest

from core.storage import LocalBlobStore, set_blob_store
from core.storage.base import tenant_key
from core.tenancy import tenant_scope
from core.vault_backend import LocalVault, S3Vault, get_vault


class _Pathless:
    """A blob store with no filesystem, like S3. Records what it is asked.

    Applies `tenant_key` inside every method and strips it back off in `list`,
    exactly as `core/storage/s3.py` does — that is the contract callers rely on,
    and a fake that prefixes differently would let a tenancy bug pass.
    """

    def __init__(self):
        self.blobs: dict[str, str] = {}
        self.puts = 0
        self.fail_put = False

    def put(self, key, content):
        self.puts += 1
        if self.fail_put:
            raise RuntimeError("the object store said no")
        self.blobs[tenant_key(key)] = content

    def get(self, key):
        return self.blobs.get(tenant_key(key))

    def exists(self, key):
        return tenant_key(key) in self.blobs

    def delete(self, key):
        self.blobs.pop(tenant_key(key), None)

    def list(self, prefix=""):
        scoped = tenant_key(prefix)
        cut = tenant_key("")
        return [k[len(cut):] for k in self.blobs if k.startswith(scoped)]

    def path_for(self, key):
        return None


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
    from core.scale.context import set_resource_provider

    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


@pytest.fixture
def object_store(monkeypatch, tmp_path):
    """Swap the vault onto a pathless store, as cloud would."""
    store = _Pathless()
    monkeypatch.setenv("SYNAPSE_SCRATCH_DIR", str(tmp_path / "scratch"))
    set_blob_store(store)
    yield store
    set_blob_store(None)


@pytest.fixture
def local_store(tmp_path):
    set_blob_store(LocalBlobStore(tmp_path / "blobs"))
    yield
    set_blob_store(None)


class TestTheFactoryPicksFromTheStore:
    def test_a_store_with_a_path_gets_the_directory_vault(self, local_store):
        vault = get_vault()

        assert isinstance(vault, LocalVault)
        assert vault.materialises is False

    def test_a_store_without_one_gets_the_materialising_vault(self, object_store):
        vault = get_vault()

        assert isinstance(vault, S3Vault)
        assert vault.materialises is True

    def test_the_root_is_never_the_pods_disk_by_accident(self, object_store, tmp_path):
        """The bug: a pathless store used to fall through to LocalBlobStore()."""
        root = get_vault().root

        assert str(root).startswith(str(tmp_path / "scratch")), (
            f"an object-store vault materialised outside the scratch root: {root}"
        )

    def test_each_tenant_gets_its_own_working_copy(self, object_store, multi_tenant):
        with tenant_scope("acme"):
            acme = get_vault().root
        with tenant_scope("globex"):
            globex = get_vault().root

        assert acme != globex
        assert acme.parts[-2] == "acme" and globex.parts[-2] == "globex"


class TestLocalVaultIsUnchanged:
    """OSS must keep working exactly as it did: a folder, and nothing else."""

    def test_a_write_is_just_a_file(self, local_store):
        vault = get_vault()
        path = vault.root / "tool_outputs" / "out.json"

        vault.write(path, '{"a": 1}')

        assert path.read_text() == '{"a": 1}'

    def test_ensure_local_is_a_no_op(self, local_store):
        vault = get_vault()
        path = vault.root / "nothing-here.txt"

        assert vault.ensure_local(path) == path
        assert not path.exists(), "it must not invent a file that was never written"

    def test_hydrate_does_nothing(self, local_store):
        assert get_vault().hydrate() == 0


class TestS3VaultWritesThrough:
    def test_a_write_reaches_the_object_store(self, object_store):
        vault = get_vault()
        path = vault.root / "tool_outputs" / "out.json"

        vault.write(path, "payload")

        assert path.read_text() == "payload", "the working copy is still a real file"
        assert object_store.blobs[tenant_key("vault/tool_outputs/out.json")] == "payload"

    def test_a_failed_write_raises_instead_of_lying(self, object_store):
        """The reference handed to the model says the output is saved.

        Under `except Exception: pass` that sentence could be false, and nothing
        would find out until the pod was replaced.
        """
        vault = get_vault()
        object_store.fail_put = True

        with pytest.raises(RuntimeError):
            vault.write(vault.root / "tool_outputs" / "doomed.txt", "payload")

    def test_a_write_outside_the_vault_is_refused(self, object_store, tmp_path):
        vault = get_vault()

        with pytest.raises(ValueError):
            vault.write(tmp_path / "elsewhere.txt", "nope")


class TestS3VaultReadsBack:
    def test_a_file_this_pod_never_wrote_is_materialised(self, object_store):
        """The pod-replacement case: the run continues on a machine that has
        never seen this tenant's vault."""
        vault = get_vault()
        object_store.blobs[tenant_key("vault/tool_outputs/earlier.json")] = "from a dead pod"
        path = vault.root / "tool_outputs" / "earlier.json"
        assert not path.exists()

        returned = vault.ensure_local(path)

        assert returned == path, "it must come back at its real path, not a temp name"
        assert path.read_text() == "from a dead pod"

    def test_a_file_that_is_already_here_is_not_refetched(self, object_store):
        vault = get_vault()
        path = vault.root / "tool_outputs" / "here.json"
        vault.write(path, "local")
        object_store.blobs[tenant_key("vault/tool_outputs/here.json")] = "remote"

        assert vault.ensure_local(path).read_text() == "local"

    def test_a_missing_file_is_left_alone(self, object_store):
        vault = get_vault()
        path = vault.root / "tool_outputs" / "never-existed.json"

        assert vault.ensure_local(path) == path
        assert not path.exists()

    def test_hydrate_pulls_the_whole_directory(self, object_store):
        """What a recursive search needs, and what one file at a time cannot give.

        The Filesystem MCP server runs `search_files` in a subprocess against
        the working copy, so a partly-materialised vault reports a smaller one
        than the tenant has.
        """
        vault = get_vault()
        for name in ("a.json", "b.json", "c.json"):
            object_store.blobs[tenant_key(f"vault/tool_outputs/{name}")] = name

        pulled = vault.hydrate()

        assert pulled == 3
        assert sorted(p.name for p in (vault.root / "tool_outputs").iterdir()) == [
            "a.json", "b.json", "c.json"
        ]
        assert vault.hydrate() == 0, "a second pass should find nothing left to pull"

    def test_hydrate_can_be_scoped_to_a_subdirectory(self, object_store):
        vault = get_vault()
        object_store.blobs[tenant_key("vault/tool_outputs/a.json")] = "a"
        object_store.blobs[tenant_key("vault/compaction_archives/b.json")] = "b"

        assert vault.hydrate("tool_outputs") == 1
        assert (vault.root / "tool_outputs" / "a.json").exists()
        assert not (vault.root / "compaction_archives").exists()

    def test_one_tenants_hydration_does_not_pull_anothers(self, object_store, multi_tenant):
        with tenant_scope("acme"):
            object_store.blobs[tenant_key("vault/tool_outputs/secret.json")] = "acme's"
        with tenant_scope("globex"):
            vault = get_vault()
            assert vault.hydrate() == 0
            assert not (vault.root / "tool_outputs").exists()


class TestMaybeVaultUsesTheBackend:
    def test_a_large_output_lands_in_the_object_store(self, object_store, monkeypatch):
        from core.config import set_settings_provider, default_settings
        from core.vault import maybe_vault

        settings = default_settings()
        settings["vault_enabled"] = True
        settings["vault_threshold"] = 10
        set_settings_provider(lambda: settings)
        try:
            result = maybe_vault("web_scraper", "x" * 100)
        finally:
            set_settings_provider(None)

        assert "vault_file" in result
        assert object_store.puts == 1, "the durable copy was not written"
        assert any(k.endswith(".txt") for k in object_store.blobs)


class TestAColdPoolBuildDoesNotDownloadTheVault:
    async def test_build_for_tenant_never_lists_the_vault(self, object_store, monkeypatch):
        """`hydrate()` pulled a tenant's whole vault onto the replica at every
        cold pool build — on the event loop, per replica, every idle TTL — for
        a Filesystem MCP that no longer exists. `tools/vault_fs.py` and the
        mention reader read the store, so nothing needs the working copy."""
        from core.scale import worker_server_module as wsm

        lists: list[str] = []
        original_list = object_store.list
        object_store.list = lambda prefix="": (lists.append(prefix), original_list(prefix))[1]
        object_store.put("vault/notes.md", "x")

        monkeypatch.setattr(wsm, "_get_native_mcp_servers", lambda *a, **k: {})

        async def _nothing(self, *a, **k):
            return None

        monkeypatch.setattr(wsm.WorkerServerModule, "_connect_user_mcp", _nothing)
        monkeypatch.setattr(wsm.WorkerServerModule, "_attach_memory_store", lambda self: None)

        module = await wsm.WorkerServerModule.build_for_tenant()

        assert module is not None
        assert lists == [], f"the build listed the vault: {lists}"
        assert not (get_vault().root / "notes.md").exists(), "a working copy was materialised"
