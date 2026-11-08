"""
Run logs are tenant-scoped blobs, not a flat prefix on a shared bucket.

Finished logs used to be uploaded to `logs/orchestration/{run_id}.log` with no
tenant component. Two consequences, both live: two tenants whose runs shared an
id overwrote each other's log, and `list_logs()` returned every tenant's runs —
including `user_input`, which is whatever the user typed.

Routing them through the blob store fixes both at once, because the store
applies the tenant prefix inside `put`/`get`/`list` rather than trusting each
call site to remember.
"""
import pytest

from core.agent_logger import AgentLogger
from core.orchestration.logger import OrchestrationLogger
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


def _write_run(run_id: str, name: str, user_input: str) -> None:
    logger = OrchestrationLogger(
        run_id=run_id,
        orchestration_id="orch-1",
        orchestration_name=name,
        user_input=user_input,
    )
    logger.run_end("completed")
    logger.close()


def test_a_finished_log_is_readable_back():
    _write_run("run-1", "Nightly", "do the thing")

    text = OrchestrationLogger.get_log("run-1")
    assert text is not None
    assert "Nightly" in text


def test_listing_surfaces_the_summary_fields():
    _write_run("run-1", "Nightly", "do the thing")

    entry = next(e for e in OrchestrationLogger.list_logs() if e["run_id"] == "run-1")
    assert entry["orchestration_name"] == "Nightly"
    assert entry["user_input"] == "do the thing"


def test_one_tenants_logs_are_invisible_to_another(multi_tenant):
    """The regression test for the flat prefix."""
    with tenant_scope("acme"):
        _write_run("run-1", "Acme Nightly", "acme's private prompt")

    with tenant_scope("globex"):
        assert OrchestrationLogger.list_logs() == []
        assert OrchestrationLogger.get_log("run-1") is None


def test_two_tenants_can_share_a_run_id_without_collision(multi_tenant):
    with tenant_scope("acme"):
        _write_run("run-1", "Acme's", "a")
    with tenant_scope("globex"):
        _write_run("run-1", "Globex's", "b")

    with tenant_scope("acme"):
        assert "Acme's" in OrchestrationLogger.get_log("run-1")
    with tenant_scope("globex"):
        assert "Globex's" in OrchestrationLogger.get_log("run-1")


def test_delete_only_reaches_the_current_tenant(multi_tenant):
    with tenant_scope("acme"):
        _write_run("run-1", "Acme's", "a")
    with tenant_scope("globex"):
        _write_run("run-1", "Globex's", "b")
        assert OrchestrationLogger.delete_log("run-1") is True

    with tenant_scope("acme"):
        assert OrchestrationLogger.get_log("run-1") is not None


def test_agent_logs_are_scoped_the_same_way(multi_tenant):
    with tenant_scope("acme"):
        logger = AgentLogger(agent_id="ag1", agent_name="Researcher",
                             session_id="s1", source="chat", user_message="hello")
        logger.close()

    with tenant_scope("globex"):
        assert AgentLogger.list_logs() == []
