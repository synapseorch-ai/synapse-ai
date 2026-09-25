"""
Serving `load_settings()` from the store without making it async.

`load_settings()` is called from roughly sixteen synchronous places on the
execution path — every vaulted tool result, the stdio MCP gate, notifications,
the scale config, and nine sites across tools/steps/react_engine. Making it
async would push `await` through all of them and out into their callers for no
gain, since none of those sites can usefully do anything else while waiting.

So the asynchronous read happens at a boundary, and the synchronous provider
reads what the boundary left behind:

    ContextVar  →  per-tenant process cache  →  default_settings()

The worker binds at job entry. The API server binds per request and refreshes
on a TTL, because in a multi-replica deployment another process may have
written the row. Both use this module; the worker's own copy of this logic is
gone.

Failing closed
--------------
A read with nothing bound returns the shipped defaults rather than whatever the
last execution happened to leave in the cache. Returning another tenant's
settings would leak their provider keys.

Note that "the defaults" is not automatically the safe answer: `allow_stdio_mcp`
defaults to True, so a miss re-opens the gate that GHSA-3j67-x3j8-r32x closed.
`is_loaded()` exists so a security check can tell "nothing is bound" apart from
"the tenant allows it" — see `core.mcp_client.stdio_mcp_allowed`.
"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar, Token

from core.tenancy import get_tenant

#: The current execution's settings. A ContextVar rather than a module global
#: for the same reason the tenant is one: with max_jobs > 1 many jobs share the
#: process, and each must see its own tenant's keys.
_current: ContextVar[dict | None] = ContextVar("synapse_settings", default=None)

#: tenant -> (loaded_at, settings). Behind the ContextVar, so a request that
#: never binds still avoids a database round trip per `load_settings()` call.
_cache: dict[str, tuple[float, dict]] = {}


def _ttl() -> float:
    """How long a cached snapshot is trusted before it is re-read.

    Only matters when something outside this process can write the settings
    row — i.e. another API replica in cloud. A single-process install
    invalidates its own cache on write, so the TTL never fires anything.
    """
    try:
        return float(os.getenv("SYNAPSE_SETTINGS_TTL", "") or 30.0)
    except (TypeError, ValueError):
        return 30.0


def install_provider() -> None:
    """Point `load_settings()` at this module."""
    from core.config import set_settings_provider

    set_settings_provider(_provider)


def _provider() -> dict:
    """The bound settings — as a copy, so no reader can change them (S10).

    It used to hand back the cached snapshot itself, and `GET /api/settings`
    popped the password hash off what it was handed: opening the Settings page
    deleted the hash from the live settings, and until the cache refreshed
    login read as not configured — the gate open to anyone. Writers call
    `save_settings()` with their own dict; nothing needs the shared one.
    Shallow: ~70 top-level keys, read a handful of times per step.
    """
    from core.config import default_settings

    settings = _current.get()
    if settings is not None:
        return dict(settings)

    entry = _cache.get(get_tenant())
    if entry is not None:
        return dict(entry[1])

    return default_settings()


def is_loaded() -> bool:
    """Whether `load_settings()` is serving a real read rather than defaults.

    A security gate must not read "nothing has been loaded yet" as consent:
    `allow_stdio_mcp` defaults to True, so a miss would re-open the capability
    that GHSA-3j67-x3j8-r32x turns into unauthenticated RCE.

    True when this module is not the one answering — another provider, or the
    on-disk fallback, is then responsible for its own answer and gets the
    benefit of the doubt.
    """
    from core.config import get_settings_provider

    if get_settings_provider() is not _provider:
        return True
    return _current.get() is not None or get_tenant() in _cache


async def refresh(tenant: str | None = None) -> dict:
    """Read the current tenant's settings from the store and cache them."""
    from core.store.settings import load_settings_for_tenant

    tenant = tenant or get_tenant()
    settings = await load_settings_for_tenant()
    _cache[tenant] = (time.monotonic(), settings)
    return settings


def invalidate(tenant: str | None = None) -> None:
    """Drop a cached snapshot. None drops every tenant's."""
    if tenant is None:
        _cache.clear()
    else:
        _cache.pop(tenant, None)


def _is_stale(tenant: str) -> bool:
    entry = _cache.get(tenant)
    return entry is None or (time.monotonic() - entry[0]) > _ttl()


async def bind(force: bool = False) -> Token:
    """Bind this execution's settings, refreshing only if the snapshot is stale.

    Returns a token for `reset()`. Deliberately not a context manager: the HTTP
    middleware and the worker's job entry both want to bind in one place and
    unbind in a `finally` somewhere else.
    """
    tenant = get_tenant()
    if force or _is_stale(tenant):
        try:
            await refresh(tenant)
        except Exception as exc:
            # A store hiccup must not fail every request. An existing snapshot
            # keeps serving; without one, the provider falls back to defaults
            # and `is_loaded()` stays False.
            print(f"[settings] could not load settings for {tenant}: {exc}", flush=True)

    entry = _cache.get(tenant)
    return _current.set(entry[1] if entry is not None else None)


def reset(token: Token) -> None:
    _current.reset(token)


def set_snapshot(settings: dict | None) -> Token:
    """Bind an explicit settings dict. For tests and for embedders."""
    return _current.set(settings)


def reset_state() -> None:
    """Forget everything. For tests."""
    _cache.clear()
    _current.set(None)
