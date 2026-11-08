"""
Resolving an execution's agents, custom tools and MCP servers.

These three lookups are the engine's per-run resource resolution. They used to
be "Postgres first, local JSON if that returns nothing", gated on a
process-global ``IS_SCALE_WORKER`` flag — and the Postgres half carried **no
tenant predicate at all**: ``select(ToolDB)`` returned every tenant's custom
tools, on every worker, for every run.

Now there is one path. The store is the source of truth, scoped to
``core.tenancy.get_tenant()``. An embedder that needs to resolve resources some
other way — from its own schema, with its own row-level security, decrypting
credentials on the way — registers a provider and answers all three itself.

Fail closed
-----------
When a provider is registered it is the *only* source. There is deliberately no
fallback to the local store if the provider returns nothing: a fallback is how
an authorization failure turns into another tenant's data. An empty answer is
an answer.
"""
from __future__ import annotations

from typing import Protocol

from core.tenancy import disable_multi_tenancy, enable_multi_tenancy


class ResourceProvider(Protocol):
    """How an embedder answers the engine's resource lookups.

    Every method is called with the tenant already established in the context;
    implementations read ``core.tenancy.get_tenant()``.

    ``resolve_orchestration`` is a lookup by id, not a listing. Listing every
    orchestration is a UI concern, and requiring an embedder to expose one to
    run a workflow would be the wrong default; the store answers those directly.
    """

    async def resolve_agent(self, agent_id: str) -> dict | None: ...
    async def resolve_orchestration(self, orch_id: str) -> dict | None: ...
    async def resolve_custom_tools(self) -> list[dict]: ...
    async def resolve_mcp_servers(self) -> list[dict]: ...

    #: Optional. The counterpart to ``resolve_mcp_servers``: the engine says
    #: what happened when it tried to reach one. Optional because it is the only
    #: method here that *writes*, and an embedder with a read-only projection is
    #: a legitimate thing to be — a provider without it is called for everything
    #: else and simply not told. Checked with ``getattr`` rather than declared
    #: on the Protocol, which cannot express "may be absent".
    #:
    #: async def report_mcp_status(self, server_name: str, status: str) -> None
    #:
    #: Optional too, for the same reason: the tools a connect listed, live, as
    #: ``{"name", "description", "inputSchema"}``, for a host that stores a
    #: server's tool list and would otherwise count a stale one (F524).
    #:
    #: async def report_mcp_tools(self, server_name: str, tools: list[dict]) -> None


_provider: ResourceProvider | None = None


def set_resource_provider(provider: ResourceProvider | None) -> None:
    """Install a resource provider, and with it, multi-tenancy.

    Registering a provider is what unlocks ``core.tenancy.tenant_scope()``.
    The two are deliberately coupled: switching tenants without a tenant-aware
    way to resolve resources would hand every tenant the same agents and tools,
    which is worse than refusing to switch at all.
    """
    global _provider, _job_tenant_resolver
    _provider = provider
    if provider is None:
        # The job resolver goes with it. Leaving one installed would have it
        # naming tenants that tenant_scope() then refuses to enter.
        _job_tenant_resolver = None
        disable_multi_tenancy()
    else:
        enable_multi_tenancy()


def get_resource_provider() -> ResourceProvider | None:
    return _provider


# ---------------------------------------------------------------------------
# Whose job is this?
# ---------------------------------------------------------------------------

#: How an embedder says which tenant a queued job belongs to.
#:
#: The job functions in ``core/scale/worker.py`` deliberately take no
#: ``tenant_id`` parameter — it used to come from the job payload, which meant
#: any caller that could enqueue could label its run with any tenant it liked.
#: But something has to establish the tenant before the job body runs, and in a
#: shared fleet that is per job rather than per process.
#:
#: The resolver is given the ARQ context and the job's own arguments and answers
#: from whatever it considers authoritative — for a service that mints run rows
#: under row-level security, reading the row is authoritative and the payload is
#: not. Returning ``None`` runs the job as the default tenant.
_job_tenant_resolver = None


def set_job_tenant_resolver(resolver) -> None:
    """Install the hook that names the tenant for each job.

    **This is not a second way to obtain multi-tenancy**, and it deliberately
    refuses to pretend otherwise: without a resource provider it raises, because
    the tenant it named would be unusable — ``tenant_scope()`` is shut until
    ``set_resource_provider()`` opens it. So the resolver adds no capability. It
    only says *where* an embedder plugs in, which is the whole point of the OSS
    engine being the core and a service being the wrapper.

    `resolver(ctx, job_name, args, kwargs) -> str | None`, awaitable.
    """
    global _job_tenant_resolver

    if resolver is not None and _provider is None:
        from core.tenancy import SingleTenantError

        raise SingleTenantError(
            "set_job_tenant_resolver() requires a resource provider. A tenant "
            "named without one cannot be entered — tenant_scope() refuses to "
            "open — so this would resolve a tenant the engine then ignores. "
            "Call set_resource_provider() first; see core/tenancy.py."
        )
    _job_tenant_resolver = resolver


def get_job_tenant_resolver():
    return _job_tenant_resolver


async def resolve_job_tenant(ctx: dict, job_name: str, args: tuple, kwargs: dict) -> str | None:
    """The tenant this job belongs to, or None for the default tenant."""
    if _job_tenant_resolver is None:
        return None
    return await _job_tenant_resolver(ctx, job_name, args, kwargs)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

async def resolve_agent(agent_id: str) -> dict | None:
    """The agent definition for `agent_id`, within the current tenant."""
    if not agent_id:
        return None
    if _provider is not None:
        return await _provider.resolve_agent(agent_id)

    from core.store.resources import get_agent

    return await get_agent(agent_id)


async def resolve_orchestration(orch_id: str) -> dict | None:
    """The orchestration definition for `orch_id`, within the current tenant.

    Routed through the provider like the others, because an orchestration is the
    resource that *names* every other one — its steps carry agent ids, forced
    tools and sub-orchestration ids. Resolving those through an embedder's
    row-level security while loading the orchestration itself straight from the
    store would bypass the boundary at the one place it matters most.
    """
    if not orch_id:
        return None
    if _provider is not None:
        return await _provider.resolve_orchestration(orch_id)

    from core.store.resources import get_orchestration

    return await get_orchestration(orch_id)


async def resolve_custom_tools() -> list[dict]:
    """Every custom tool belonging to the current tenant."""
    if _provider is not None:
        return await _provider.resolve_custom_tools()

    from core.store.resources import load_tools

    return await load_tools()


async def resolve_mcp_servers() -> list[dict]:
    """Every MCP server registered by the current tenant."""
    if _provider is not None:
        return await _provider.resolve_mcp_servers()

    from core.store.resources import load_mcp_servers

    return await load_mcp_servers()


async def report_mcp_status(server_name: str, status: str) -> None:
    """Record how the last attempt to reach an MCP server went.

    The engine has always *known* which servers needed re-authorising and has
    never had anywhere to say so. `reauth_needed` has been in the vocabulary of
    both UIs since their MCP screens were written, with nothing able to set it
    from a worker — so a server whose token had expired went on displaying
    `connected` while every agent using it reported, accurately, that it had no
    such tool.

    Never raises. A status is a courtesy to whoever is looking at the screen
    later; failing a tenant's whole MCP build because a status write did not
    land would trade a cosmetic problem for a real one.
    """
    try:
        if _provider is not None:
            report = getattr(_provider, "report_mcp_status", None)
            if report is not None:
                await report(server_name, status)
            return

        # No provider: the single-tenant store is the record. Read first so an
        # unchanged status is not a write — a pool build touches every server a
        # tenant has, and most of the time nothing has moved.
        from core.store.resources import get_mcp_server, save_mcp_server

        server = await get_mcp_server(server_name)
        if server is None or server.get("status") == status:
            return
        server["status"] = status
        await save_mcp_server(server)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        print(
            f"[context] could not record MCP status '{status}' for "
            f"'{server_name}': {exc}",
            flush=True,
        )


async def report_mcp_tools(server_name: str, tools: list[dict]) -> None:
    """Hand the host the tools a connect listed from one MCP server, live (F524).

    A host that stores a server's tool list (cloud's, from discovery) counted and offered the list it saw when
    the server was added, while every run was offered what the server lists now: acme's was 37 tools stored and
    236 live. Self-hosted lists live everywhere and stores none, so without a provider this does nothing.

    Never raises, as `report_mcp_status` does not: a stale count is a courtesy problem, a failed build a real one.
    """
    try:
        report = getattr(_provider, "report_mcp_tools", None) if _provider is not None else None
        if report is not None:
            await report(server_name, tools)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        print(f"[context] could not record MCP tools for '{server_name}': {exc}", flush=True)
