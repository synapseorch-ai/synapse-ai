"""
The two kinds of real directory left, and the difference between them.

`state_dir()` is rebuildable — losing it costs a re-index. `tenant_dir()` is
durable tenant state, and when there is nowhere durable to put it, it refuses.
That refusal is the point of the module: a logged-in WhatsApp Web session
written to ephemeral container storage looks like it worked, and costs the user
a QR scan on every deploy.
"""
import pytest

from core.runtime_dirs import NoDurableStorage, state_dir, tenant_dir
from core.scale.context import set_resource_provider
from core.storage import set_blob_store
from core.storage.base import LocalBlobStore
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


class _PathlessStore(LocalBlobStore):
    """A blob store with no filesystem, as S3 is."""

    def path_for(self, key):
        return None


def test_state_dir_is_created_and_per_tenant(multi_tenant):
    with tenant_scope("acme"):
        acme = state_dir("chroma_db")
    with tenant_scope("globex"):
        globex = state_dir("chroma_db")

    assert acme.is_dir() and globex.is_dir()
    assert acme != globex


def test_state_dir_honours_its_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNAPSE_STATE_DIR", str(tmp_path / "elsewhere"))
    assert str(state_dir("chroma_db")).startswith(str(tmp_path / "elsewhere"))


def test_tenant_dir_lands_beside_the_tenants_other_content():
    path = tenant_dir("whatsapp_sessions", "chan_1")
    assert path.is_dir()
    assert path.name == "chan_1"


def test_tenant_dir_refuses_when_there_is_nowhere_durable(tmp_path):
    """The whole reason this module exists.

    Writing a WhatsApp profile to container storage that the next deploy
    discards fails silently, and only at the worst moment.
    """
    previous = None
    try:
        set_blob_store(_PathlessStore(root=tmp_path))
        with pytest.raises(NoDurableStorage):
            tenant_dir("whatsapp_sessions", "chan_1")
    finally:
        set_blob_store(previous)
