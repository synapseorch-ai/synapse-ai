"""
Resource CRUD in the store: tenant scoping, ordering, and the write guard.

Three of the four tables (`orchestrations`, `agents`, `tools`) are keyed on a
bare `id`, with the tenant only an index. That makes two things the database
will not do for us, and both are tested here: a read by id alone crosses the
tenant boundary, and an upsert on a conflicting id *rewrites the other tenant's
row*. The second is the sharper one — it is a cross-tenant write reachable by
guessing an id.

The ordering tests exist because `JsonStore` preserved insertion order and
`agents[0]` is the shipped fallback for "the user's default agent".
"""
import pytest

from core.scale.context import set_resource_provider
from core.store import resources
from core.store.resources import CrossTenantWrite
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


# ── round-trip ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_whole_document_round_trips():
    """`definition` holds the original item, not a projection of known fields."""
    item = {
        "id": "orch-1",
        "name": "Nightly",
        "description": "runs at 2am",
        "steps": [{"id": "s1", "type": "agent", "agent_id": "a1"}],
        "a_field_the_engine_has_never_heard_of": {"nested": [1, 2, 3]},
    }
    await resources.save_orchestration(item)

    assert await resources.get_orchestration("orch-1") == item


@pytest.mark.asyncio
async def test_save_is_an_upsert_not_an_append():
    await resources.save_agent({"id": "a1", "name": "First"})
    await resources.save_agent({"id": "a1", "name": "Renamed"})

    agents = await resources.load_agents()
    assert len(agents) == 1
    assert agents[0]["name"] == "Renamed"


@pytest.mark.asyncio
async def test_delete_reports_whether_it_deleted():
    await resources.save_tool({"id": "t1", "name": "grep"})

    assert await resources.delete_tool("t1") is True
    assert await resources.delete_tool("t1") is False
    assert await resources.load_tools() == []


@pytest.mark.asyncio
async def test_mcp_servers_are_keyed_by_name():
    await resources.save_mcp_server({"name": "github", "label": "GitHub"})
    await resources.save_mcp_server({"name": "github", "label": "GitHub (edited)"})

    servers = await resources.load_mcp_servers()
    assert len(servers) == 1
    assert servers[0]["label"] == "GitHub (edited)"


# ── ordering ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_order_is_deterministic():
    """Ids chosen to sort against insertion order, so a bare PK sort would show.

    The guarantee is `(created_at, id)` — stable across restarts — rather than
    strict insertion order, which two inserts landing in the same microsecond
    could not provide anyway.
    """
    for agent_id in ("zeta", "mu", "alpha"):
        await resources.save_agent({"id": agent_id, "name": agent_id})

    first = [a["id"] for a in await resources.load_agents()]
    second = [a["id"] for a in await resources.load_agents()]

    assert first == second
    assert sorted(first) != first, "ids must not merely be sorting alphabetically"


@pytest.mark.asyncio
async def test_adding_an_agent_does_not_change_which_one_is_default():
    """`agents[0]` is the shipped fallback, so a new agent must not take it."""
    await resources.save_agent({"id": "zeta", "name": "The default"})
    await resources.save_agent({"id": "alpha", "name": "Added later"})

    assert (await resources.load_agents())[0]["id"] == "zeta"


@pytest.mark.asyncio
async def test_editing_an_agent_does_not_move_it():
    for n in range(3):
        await resources.save_agent({"id": f"a{n}", "name": f"Agent {n}"})

    await resources.save_agent({"id": "a0", "name": "Still first"})

    agents = await resources.load_agents()
    assert [a["id"] for a in agents] == ["a0", "a1", "a2"]
    assert agents[0]["name"] == "Still first"


# ── tenant scoping ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_tenants_rows_are_invisible_to_another(multi_tenant):
    with tenant_scope("acme"):
        await resources.save_orchestration({"id": "shared-id", "name": "Acme's"})

    with tenant_scope("globex"):
        assert await resources.load_orchestrations() == []
        assert await resources.get_orchestration("shared-id") is None


@pytest.mark.asyncio
async def test_a_write_cannot_take_over_another_tenants_row(multi_tenant):
    """The id is the whole conflict target, so this would otherwise UPDATE."""
    with tenant_scope("acme"):
        await resources.save_orchestration({"id": "orch-1", "name": "Acme's"})

    with tenant_scope("globex"):
        with pytest.raises(CrossTenantWrite):
            await resources.save_orchestration({"id": "orch-1", "name": "Stolen"})

    with tenant_scope("acme"):
        assert (await resources.get_orchestration("orch-1"))["name"] == "Acme's"


@pytest.mark.asyncio
async def test_delete_cannot_reach_another_tenants_row(multi_tenant):
    with tenant_scope("acme"):
        await resources.save_agent({"id": "a1", "name": "Acme's"})

    with tenant_scope("globex"):
        assert await resources.delete_agent("a1") is False

    with tenant_scope("acme"):
        assert await resources.get_agent("a1") is not None


@pytest.mark.asyncio
async def test_two_tenants_can_register_the_same_mcp_server_name(multi_tenant):
    with tenant_scope("acme"):
        await resources.save_mcp_server({"name": "github", "label": "Acme"})
    with tenant_scope("globex"):
        await resources.save_mcp_server({"name": "github", "label": "Globex"})

    with tenant_scope("acme"):
        assert (await resources.load_mcp_servers())[0]["label"] == "Acme"
    with tenant_scope("globex"):
        assert (await resources.load_mcp_servers())[0]["label"] == "Globex"


# ── replace ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_replace_deletes_what_is_missing():
    await resources.replace_agents([{"id": "a1"}, {"id": "a2"}])
    await resources.replace_agents([{"id": "a2"}, {"id": "a3"}])

    assert sorted(a["id"] for a in await resources.load_agents()) == ["a2", "a3"]


@pytest.mark.asyncio
async def test_replace_with_nothing_clears_only_this_tenant(multi_tenant):
    with tenant_scope("acme"):
        await resources.save_agent({"id": "a1", "name": "Acme's"})
    with tenant_scope("globex"):
        await resources.save_agent({"id": "b1", "name": "Globex's"})
        await resources.replace_agents([])
        assert await resources.load_agents() == []

    with tenant_scope("acme"):
        assert len(await resources.load_agents()) == 1


# ── the importer writes the same rows ────────────────────────────────────────

@pytest.mark.asyncio
async def test_imported_rows_match_natively_written_ones(tmp_path):
    """The importer and the CRUD path share their row builders for this reason."""
    import json

    from core.store.importer import import_data_dir

    item = {"id": "orch-1", "name": "Nightly", "description": "d", "is_active": True}
    (tmp_path / "orchestrations.json").write_text(json.dumps([item]), encoding="utf-8")
    await import_data_dir(tmp_path)

    imported = await resources.get_orchestration("orch-1")
    await resources.delete_orchestration("orch-1")
    await resources.save_orchestration(item)

    assert imported == await resources.get_orchestration("orch-1")


# ── the row's key is the id ──────────────────────────────────────────────────

async def _insert_raw_agent(agent_id: str, name: str, definition: dict) -> None:
    """A row written the way another writer stores it — the cloud control plane
    keeps the id in the column and not inside `definition`."""
    from core.store import cache, session
    from core.store.models import AgentDB
    from core.tenancy import get_tenant

    async with session() as s:
        s.add(AgentDB(id=agent_id, name=name, definition=definition, tenant_id=get_tenant()))
        await s.commit()
    cache.invalidate(AgentDB.__tablename__)


@pytest.mark.asyncio
async def test_a_listed_agent_carries_its_row_id_even_when_the_document_does_not():
    """Every cloud agent created from a screen, a template or an import was listed
    without an id, so each collapsed to `""`: a delegate offered the model an
    `agent_id` enum containing an empty string (Gemini refuses the whole request),
    the builder's `list_agents` answered `id: None`, and channel lookups keyed on
    `a["id"]` raised. The row's key is the identity; the document is its payload."""
    await _insert_raw_agent("agent_cloud_1", "Researcher", {"name": "Researcher", "type": "conversational"})

    listed = {a["name"]: a for a in await resources.load_agents()}
    assert listed["Researcher"]["id"] == "agent_cloud_1"
    assert (await resources.get_agent("agent_cloud_1"))["id"] == "agent_cloud_1"


@pytest.mark.asyncio
async def test_a_document_without_a_name_is_listed_under_the_rows():
    await _insert_raw_agent("agent_cloud_2", "Named in the column", {"type": "conversational"})

    (agent,) = [a for a in await resources.load_agents() if a["id"] == "agent_cloud_2"]
    assert agent["name"] == "Named in the column"
