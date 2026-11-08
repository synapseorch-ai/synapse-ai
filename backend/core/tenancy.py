"""
Which tenant is this execution for?

The engine resolves settings, agents, tools, MCP servers and run state through
providers. Those providers need to know whose request they are serving, and
threading a tenant argument through every function in the orchestration engine
would touch thousands of lines and break every caller. So the tenant lives in a
context variable instead.

``ContextVar`` is the right primitive rather than a module global because a
worker runs many jobs concurrently in one process. Each ARQ job is its own
asyncio task, and a task gets a copy of the context at creation — so two jobs
interleaving on the event loop each read their own tenant, with no locking and
no possibility of one job's value being observed by another.

The shipped product is single-tenant
------------------------------------
Multi-tenancy is a capability of the engine, not a feature of the application.
There is deliberately **no public setter**. ``tenant_scope()`` refuses to open
unless an embedder has registered a resource provider, and nothing in the
shipped tree registers one — so ``get_tenant()`` is a constant, and no setting,
environment variable, CLI flag or API parameter can change it.

That is a product boundary, not a claim about what is possible: this is AGPL
source, and anyone may fork it and call ``enable_multi_tenancy()`` themselves.
At that point they are distributing a derivative work under the same licence,
which is the intended arrangement. What the boundary prevents is the shipped
application offering tenancy as something a user can switch on.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from core.store.models import DEFAULT_TENANT

#: The tenant the current execution belongs to. Read by every provider.
_current_tenant: ContextVar[str] = ContextVar("synapse_tenant", default=DEFAULT_TENANT)

#: Set by ``enable_multi_tenancy()`` when an embedder registers a resource
#: provider. Until then ``tenant_scope()`` refuses to open.
_multi_tenant: bool = False


class SingleTenantError(RuntimeError):
    """Raised when something tries to switch tenants in a single-tenant build."""


def get_tenant() -> str:
    """The tenant this execution belongs to.

    Always safe to call. In the shipped single-tenant product this is a
    constant.
    """
    return _current_tenant.get()


def is_multi_tenant() -> bool:
    return _multi_tenant


def enable_multi_tenancy() -> None:
    """Allow ``tenant_scope()`` to open.

    Called by ``core.scale.context.set_resource_provider()``, i.e. only when an
    embedder has supplied a tenant-aware way to resolve agents, tools and MCP
    servers. Enabling tenancy without that would give every tenant the same
    resources, which is worse than refusing.
    """
    global _multi_tenant
    _multi_tenant = True


def disable_multi_tenancy() -> None:
    """Return to single-tenant. For tests, and for provider teardown."""
    global _multi_tenant
    _multi_tenant = False


def adopt_process_tenant(tenant_id: str) -> None:
    """Make *this process* serve one tenant, for its whole life.

    A native tool server is a subprocess. A ``ContextVar`` does not cross a
    process boundary, so a spawned `bash` or `vault_sandbox` server resolved
    ``get_tenant()`` to the default and read the default tenant's vault, whoever
    had called it. The parent spawns one such server per tenant and tells it
    which one it is; this is how it is told.

    **This is not a way to serve two tenants**, and it does not weaken the
    single-tenant boundary D30 describes. It sets one value, once, in a process
    that exists to serve exactly one tenant. ``tenant_scope()`` is still shut
    unless an embedder registers a resource provider, so nothing here lets a
    process switch tenants, and no environment variable read by the *engine* can
    produce a second one. `tests/unit/test_tool_server.py` pins both halves.

    Called only from ``core/tool_server.py``, which is the one module allowed to
    read ``SYNAPSE_TENANT_ID`` — the same arrangement as ``core/store/importer.py``
    being the one module allowed to name ``SYNAPSE_DATA_DIR``.
    """
    if not tenant_id:
        return
    _current_tenant.set(tenant_id)


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """Run a block as `tenant_id`.

    The only way to change tenants, and it is unavailable unless a resource
    provider has been registered.

    Restores the previous value on exit via the token rather than by assigning
    the old value back: under concurrency, assigning back would clobber a value
    set by an interleaved task in the same context.
    """
    if not _multi_tenant:
        raise SingleTenantError(
            "This build is single-tenant. tenant_scope() requires a resource "
            "provider registered through core.scale.context.set_resource_provider(); "
            "see core/tenancy.py."
        )
    if not tenant_id:
        raise ValueError("tenant_scope() requires a non-empty tenant id")

    token = _current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _current_tenant.reset(token)
