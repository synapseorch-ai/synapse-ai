"""
Tenant-scoped CRUD for the four resource collections.

Orchestrations, agents, custom tools and MCP servers used to be four JSON files
under ``DATA_DIR``, read through ``JsonStore`` singletons bound to a path at
import time. This module is what replaces them.

Two rules run through everything here, and both exist because of the shape of
the tables rather than as general good practice:

**Every read carries an explicit tenant predicate.** ``orchestrations``,
``agents`` and ``tools`` are keyed on a bare ``id``; the tenant is an index, not
part of the key. So a lookup by id alone is a cross-tenant read, and the
database will not stop one.

**No write may move a row between tenants.** Same cause: because ``id`` alone is
the conflict target, an upsert of an id that already belongs to someone else
would rewrite *their* row. That is a cross-tenant write reachable by guessing an
id, so writes check ownership first and raise rather than silently reassigning.

Ordering
--------
``JsonStore`` preserved insertion order, and two shipped behaviours depend on
it: ``load_user_agents()[0]`` is the documented fallback for "the user's default
agent" in both the UI route and the v1 API. SQL has no inherent order, so lists
come back sorted by ``(created_at, id)``.
"""
from __future__ import annotations

from sqlalchemy import delete, select

from core.tenancy import get_tenant


class CrossTenantWrite(RuntimeError):
    """A write addressed a row belonging to another tenant."""


# ---------------------------------------------------------------------------
# Row shapes
#
# Shared with core/store/importer.py so that an install imported from JSON and
# one created natively produce identical rows. When these drifted apart the
# importer became a second, undocumented schema.
# ---------------------------------------------------------------------------

def orchestration_values(item: dict, tenant_id: str) -> dict:
    return {
        "id": item["id"],
        "tenant_id": tenant_id,
        "name": item.get("name") or item["id"],
        "description": item.get("description") or "",
        "definition": item,
        # The engine treats NULL is_active as "not active", so an orchestration
        # written without it would exist, list fine, and be invisible on any
        # screen that counts `WHERE is_active`.
        "is_active": bool(item.get("is_active", True)),
    }


def agent_values(item: dict, tenant_id: str) -> dict:
    return {
        "id": item["id"],
        "tenant_id": tenant_id,
        "name": item.get("name") or item["id"],
        "definition": item,
    }


def tool_values(item: dict, tenant_id: str) -> dict:
    tool_id = item.get("id") or item.get("name")
    return {
        "id": tool_id,
        "tenant_id": tenant_id,
        "name": item.get("name") or tool_id,
        "definition": item,
    }


def mcp_server_values(item: dict, tenant_id: str) -> dict:
    name = item["name"]
    return {
        "tenant_id": tenant_id,
        "name": name,
        "label": item.get("label") or name,
        "definition": item,
    }


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

async def _list(model) -> list[dict]:
    """Every item in this tenant's collection, cached.

    Read on the per-turn path — `resolve_custom_tools()` runs before each model
    call — so this is one of the queries the cache exists for. Writes below
    invalidate it, so a cached list is never stale for the process that changed
    it. See core/store/cache.py.
    """
    from core.store import cache

    return await cache.get_or_load(model.__tablename__, "*", lambda: _list_uncached(model))


async def _list_uncached(model) -> list[dict]:
    from core.store import session

    async with session() as s:
        rows = (
            await s.execute(
                select(model)
                .where(model.tenant_id == get_tenant())
                .order_by(model.created_at, _natural_key(model))
            )
        ).scalars().all()
    return [_document(model, r) for r in rows]


#: Tables whose row key IS the item's id. The document usually repeats it —
#: every row this engine writes does — but another writer need not: the cloud
#: control plane keeps an agent's id in the column only, and every agent it
#: created was listed without one. So the column wins, and a document with no
#: name takes the row's. Tools and MCP servers are keyed on something else and
#: are returned untouched.
_KEYED_BY_ID = frozenset({"agents", "orchestrations"})


def _document(model, row) -> dict:
    if model.__tablename__ not in _KEYED_BY_ID:
        return row.definition
    return {**row.definition, "id": row.id, "name": row.definition.get("name") or row.name}


def _natural_key(model):
    """The tiebreaker for rows sharing a `created_at`."""
    return model.name if model.__tablename__ == "mcp_servers" else model.id


async def _get(model, key_column, key_value) -> dict | None:
    from core.store import cache

    if not key_value:
        return None
    return await cache.get_or_load(
        model.__tablename__, f"id:{key_value}",
        lambda: _get_uncached(model, key_column, key_value),
    )


async def _get_uncached(model, key_column, key_value) -> dict | None:
    from core.store import session

    async with session() as s:
        row = (
            await s.execute(
                select(model).where(
                    key_column == key_value,
                    model.tenant_id == get_tenant(),
                )
            )
        ).scalar_one_or_none()
    return _document(model, row) if row is not None else None


async def _owner(s, model, key_column, key_value) -> str | None:
    """The tenant owning this row, or None when there is no such row."""
    return (
        await s.execute(
            select(model.tenant_id).where(key_column == key_value)
        )
    ).scalar_one_or_none()


async def _save(s, model, values: dict, index_elements: list[str], key_column) -> None:
    from core.store import upsert

    tenant = values["tenant_id"]
    # `mcp_servers` carries the tenant in its primary key, so a conflict there
    # can only ever be with a row this tenant already owns. The other three are
    # keyed on `id` alone and need the check.
    if "tenant_id" not in index_elements:
        owner = await _owner(s, model, key_column, values[index_elements[0]])
        if owner is not None and owner != tenant:
            raise CrossTenantWrite(
                f"{model.__tablename__} row {values[index_elements[0]]!r} belongs to "
                f"another tenant"
            )

    # Never rewrite identity or the conflict key. `created_at` is absent from
    # `values` and so is never updated either — the list order depends on it,
    # and editing an item must not move it to the end of the list.
    protected = {"id", "tenant_id", *index_elements}

    await upsert(
        s,
        model,
        values=values,
        index_elements=index_elements,
        update=[c for c in values if c not in protected],
    )

    from core.store import cache
    cache.invalidate(model.__tablename__)


async def _delete(model, key_column, key_value) -> bool:
    from core.store import cache, session

    if not key_value:
        return False
    async with session() as s:
        result = await s.execute(
            delete(model).where(
                key_column == key_value,
                model.tenant_id == get_tenant(),
            )
        )
    cache.invalidate(model.__tablename__)
    return bool(result.rowcount)


async def _replace(model, items: list[dict], build, index_elements, key_column, key_of) -> None:
    """Make this tenant's rows exactly `items`.

    Deletes anything not present, so it is for seeding and bulk import only —
    never a CRUD route, where `_replace([])` on an empty request body would wipe
    the tenant.
    """
    from core.store import session

    tenant = get_tenant()
    keys = []
    async with session() as s:
        for item in items:
            values = build(item, tenant)
            await _save(s, model, values, index_elements, key_column)
            keys.append(values[key_of])

        stmt = delete(model).where(model.tenant_id == tenant)
        if keys:
            stmt = stmt.where(key_column.notin_(keys))
        await s.execute(stmt)

    from core.store import cache
    cache.invalidate(model.__tablename__)


# ---------------------------------------------------------------------------
# Orchestrations
# ---------------------------------------------------------------------------

async def load_orchestrations() -> list[dict]:
    from core.store.models import OrchestrationDB
    return await _list(OrchestrationDB)


async def get_orchestration(orch_id: str) -> dict | None:
    from core.store.models import OrchestrationDB
    return await _get(OrchestrationDB, OrchestrationDB.id, orch_id)


async def save_orchestration(item: dict) -> None:
    from core.store import session
    from core.store.models import OrchestrationDB

    async with session() as s:
        await _save(
            s, OrchestrationDB, orchestration_values(item, get_tenant()),
            ["id"], OrchestrationDB.id,
        )


async def delete_orchestration(orch_id: str) -> bool:
    from core.store.models import OrchestrationDB
    return await _delete(OrchestrationDB, OrchestrationDB.id, orch_id)


async def replace_orchestrations(items: list[dict]) -> None:
    from core.store.models import OrchestrationDB
    await _replace(
        OrchestrationDB, [i for i in items if i.get("id")],
        orchestration_values, ["id"], OrchestrationDB.id, "id",
    )


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def load_agents() -> list[dict]:
    from core.store.models import AgentDB
    return await _list(AgentDB)


async def get_agent(agent_id: str) -> dict | None:
    from core.store.models import AgentDB
    return await _get(AgentDB, AgentDB.id, agent_id)


async def save_agent(item: dict) -> None:
    from core.store import session
    from core.store.models import AgentDB

    async with session() as s:
        await _save(s, AgentDB, agent_values(item, get_tenant()), ["id"], AgentDB.id)


async def delete_agent(agent_id: str) -> bool:
    from core.store.models import AgentDB
    return await _delete(AgentDB, AgentDB.id, agent_id)


async def replace_agents(items: list[dict]) -> None:
    from core.store.models import AgentDB
    await _replace(
        AgentDB, [i for i in items if i.get("id")],
        agent_values, ["id"], AgentDB.id, "id",
    )


# ---------------------------------------------------------------------------
# Custom tools
# ---------------------------------------------------------------------------

async def load_tools() -> list[dict]:
    from core.store.models import ToolDB
    return await _list(ToolDB)


async def get_tool(tool_id: str) -> dict | None:
    from core.store.models import ToolDB
    return await _get(ToolDB, ToolDB.id, tool_id)


async def save_tool(item: dict) -> None:
    await save_tools([item])


async def save_tools(items: list[dict]) -> None:
    """Upsert several tools without deleting anything else.

    This is what the create/import routes want. They previously did
    load-list → mutate → save-list, which loses a concurrent write.
    """
    from core.store import session
    from core.store.models import ToolDB

    tenant = get_tenant()
    async with session() as s:
        for item in items:
            if not (item.get("id") or item.get("name")):
                continue
            await _save(s, ToolDB, tool_values(item, tenant), ["id"], ToolDB.id)


async def delete_tool(tool_id: str) -> bool:
    from core.store.models import ToolDB
    return await _delete(ToolDB, ToolDB.id, tool_id)


async def replace_tools(items: list[dict]) -> None:
    from core.store.models import ToolDB
    await _replace(
        ToolDB, [i for i in items if i.get("id") or i.get("name")],
        tool_values, ["id"], ToolDB.id, "id",
    )


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

async def load_mcp_servers() -> list[dict]:
    from core.store.models import MCPServerDB
    return await _list(MCPServerDB)


async def get_mcp_server(name: str) -> dict | None:
    from core.store.models import MCPServerDB
    return await _get(MCPServerDB, MCPServerDB.name, name)


async def save_mcp_server(item: dict) -> None:
    from core.store import session
    from core.store.models import MCPServerDB

    if not item.get("name"):
        return
    async with session() as s:
        await _save(
            s, MCPServerDB, mcp_server_values(item, get_tenant()),
            ["tenant_id", "name"], MCPServerDB.name,
        )


async def delete_mcp_server(name: str) -> bool:
    from core.store.models import MCPServerDB
    return await _delete(MCPServerDB, MCPServerDB.name, name)


async def replace_mcp_servers(items: list[dict]) -> None:
    from core.store.models import MCPServerDB
    await _replace(
        MCPServerDB, [i for i in items if i.get("name")],
        mcp_server_values, ["tenant_id", "name"], MCPServerDB.name, "name",
    )
