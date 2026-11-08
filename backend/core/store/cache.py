"""
A small read-through cache for the store's hot paths.

Every ReAct turn resolves the tenant's custom tools and its agent before it can
build the request to the model, and every authenticated request looks up an API
key. Those are point queries against tiny tables, but on a shared fleet they
are also a network round trip to Postgres in the middle of the latency the user
actually feels — several per turn, on every turn.

So reads that repeat go through here. Three rules make it safe:

**Everything is keyed by tenant.** Entries are stored under
``(tenant, namespace, key)``. A cache that forgot the tenant would be a
cross-tenant read with no query behind it to audit, which is worse than the
round trip it saved.

**Writes invalidate in-process immediately.** The CRUD paths call
``invalidate`` on the namespace they touched, so a user who edits a tool sees
the change on their next turn rather than after a timeout.

**A TTL bounds everything else.** In-process invalidation cannot reach another
replica, so a short TTL is what makes a multi-replica deployment converge.
That is also why the API-key namespace has its own, much shorter TTL: a revoked
key must stop working quickly, and correctness there is worth more than the
saved lookup.

Set ``SYNAPSE_STORE_CACHE_TTL=0`` to disable it entirely.
"""
from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable

from core.tenancy import get_tenant

#: namespace -> seconds. Namespaces not listed here use the default TTL.
_TTL_OVERRIDES = {
    # Revocation has to take effect fast, so this buys much less than the
    # resource namespaces do — but an API key is read on every single request,
    # and even a few seconds removes that query from the common case.
    "api_keys": 5.0,
}

_DEFAULT_TTL = 30.0

#: (tenant, namespace, key) -> (expires_at, value)
_entries: dict[tuple[str, str, str], tuple[float, Any]] = {}


def _ttl(namespace: str) -> float:
    configured = os.getenv("SYNAPSE_STORE_CACHE_TTL")
    if configured is not None:
        try:
            base = float(configured)
        except (TypeError, ValueError):
            base = _DEFAULT_TTL
        # An explicit 0 disables caching everywhere, including the overrides.
        if base <= 0:
            return 0.0
        return min(base, _TTL_OVERRIDES.get(namespace, base))
    return _TTL_OVERRIDES.get(namespace, _DEFAULT_TTL)


async def get_or_load(
    namespace: str,
    key: str,
    loader: Callable[[], Awaitable[Any]],
) -> Any:
    """Return the cached value for `key`, or await `loader` and cache it."""
    ttl = _ttl(namespace)
    if ttl <= 0:
        return await loader()

    entry_key = (get_tenant(), namespace, key)
    hit = _entries.get(entry_key)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]

    value = await loader()
    _entries[entry_key] = (now + ttl, value)
    return value


def invalidate(namespace: str, tenant: str | None = "") -> None:
    """Drop everything cached for one namespace.

    Scoped to the current tenant by default. Pass ``tenant=None`` to clear the
    namespace for every tenant — which the API-key path needs: a key is looked
    up before any scope is established, so its entry is filed under whatever
    tenant happened to be current, while revocation runs inside the owning
    tenant's scope. Matching on tenant there would reliably miss.
    """
    if tenant == "":
        tenant = get_tenant()
    for entry_key in [
        k for k in _entries
        if k[1] == namespace and (tenant is None or k[0] == tenant)
    ]:
        del _entries[entry_key]


def invalidate_all() -> None:
    """Drop the whole cache. For tests, and for a store swap."""
    _entries.clear()
