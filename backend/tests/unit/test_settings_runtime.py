"""
Serving `load_settings()` from the store while it stays synchronous.

`load_settings()` has ~16 synchronous callers on the execution path, so the
async store read happens at a boundary — a job entry, or an HTTP request — and
the provider reads what the boundary left behind. These tests pin the three
things that arrangement has to get right: it must fail closed on a miss, a
write must be visible to the very next synchronous read, and two tenants in one
process must not see each other's keys.
"""
import pytest

from core import settings_runtime
from core.config import default_settings, load_settings, set_settings_provider
from core.scale.context import set_resource_provider
from core.store.settings import load_settings_for_tenant, save_many, save_setting
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


@pytest.fixture(autouse=True)
def _clean_runtime():
    """Every test starts with nothing installed and nothing cached."""
    settings_runtime.reset_state()
    set_settings_provider(None)
    yield
    settings_runtime.reset_state()
    set_settings_provider(None)


@pytest.fixture
def installed():
    settings_runtime.install_provider()
    yield


@pytest.fixture
def multi_tenant():
    set_resource_provider(_Provider())
    yield
    set_resource_provider(None)


# ── failing closed ───────────────────────────────────────────────────────────

def test_a_read_before_any_load_returns_the_shipped_defaults(installed):
    assert load_settings() == default_settings()


def test_a_miss_is_not_mistaken_for_consent(installed):
    """`allow_stdio_mcp` defaults to True — see GHSA-3j67-x3j8-r32x."""
    from core.mcp_client import stdio_mcp_allowed

    assert load_settings()["allow_stdio_mcp"] is True
    assert settings_runtime.is_loaded() is False
    assert stdio_mcp_allowed() is False


def test_the_gate_defers_when_this_module_is_not_answering():
    """Without our provider installed, someone else owns the answer."""
    assert settings_runtime.is_loaded() is True


@pytest.mark.asyncio
async def test_the_gate_opens_once_settings_are_really_loaded(installed):
    from core.mcp_client import stdio_mcp_allowed

    await settings_runtime.refresh()

    assert settings_runtime.is_loaded() is True
    assert stdio_mcp_allowed() is True


# ── writes are visible to the next synchronous read ──────────────────────────

@pytest.mark.asyncio
async def test_a_saved_value_reaches_the_sync_reader(installed):
    await save_setting("agent_name", "Renamed")
    await settings_runtime.refresh()

    assert load_settings()["agent_name"] == "Renamed"


@pytest.mark.asyncio
async def test_bind_serves_the_cached_snapshot(installed):
    await save_setting("agent_name", "Bound")

    token = await settings_runtime.bind(force=True)
    try:
        assert load_settings()["agent_name"] == "Bound"
    finally:
        settings_runtime.reset(token)


# ── the diff against defaults ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_defaults_are_not_written_as_rows():
    """Storing every default would pin the install to today's defaults forever."""
    from sqlalchemy import func, select

    from core.store import session
    from core.store.models import SettingDB

    await save_many(default_settings())

    async with session() as s:
        rows = (await s.execute(select(func.count()).select_from(SettingDB))).scalar()
    assert rows == 0


@pytest.mark.asyncio
async def test_returning_a_value_to_its_default_deletes_its_row():
    settings = default_settings()

    settings["agent_name"] = "Custom"
    await save_many(settings)
    assert (await load_settings_for_tenant())["agent_name"] == "Custom"

    settings["agent_name"] = default_settings()["agent_name"]
    await save_many(settings)

    from sqlalchemy import select

    from core.store import session
    from core.store.models import SettingDB

    async with session() as s:
        keys = (
            await s.execute(select(SettingDB.key).where(SettingDB.key == "agent_name"))
        ).scalars().all()
    assert keys == []


@pytest.mark.asyncio
async def test_a_non_default_value_survives_the_round_trip():
    settings = default_settings()
    settings["worker_concurrency"] = 3
    settings["bash_allowed_dirs"] = ["/srv/work"]
    await save_many(settings)

    loaded = await load_settings_for_tenant()
    assert loaded["worker_concurrency"] == 3
    assert loaded["bash_allowed_dirs"] == ["/srv/work"]


# ── two tenants in one process ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_each_tenant_sees_its_own_keys(installed, multi_tenant):
    with tenant_scope("acme"):
        await save_setting("openai_key", "sk-acme")
        await settings_runtime.refresh()
    with tenant_scope("globex"):
        await save_setting("openai_key", "sk-globex")
        await settings_runtime.refresh()

    with tenant_scope("acme"):
        assert load_settings()["openai_key"] == "sk-acme"
    with tenant_scope("globex"):
        assert load_settings()["openai_key"] == "sk-globex"


@pytest.mark.asyncio
async def test_a_tenant_with_nothing_stored_gets_defaults_not_a_neighbour(
    installed, multi_tenant
):
    with tenant_scope("acme"):
        await save_setting("openai_key", "sk-acme")
        await settings_runtime.refresh()

    with tenant_scope("newcomer"):
        assert load_settings()["openai_key"] == ""
