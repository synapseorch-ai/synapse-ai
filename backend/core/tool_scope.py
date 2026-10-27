"""Who a call to one of Synapse's own tool servers is for, sent with the call (F1067).

A native tool server is a subprocess, and one serves every chat and run of a backend (on a worker, every tenant of
its task): a ContextVar does not reach it, so nothing it keeps between calls knew whose it was. The scraper's
browser sessions and cookie jar were everybody's. The call now carries the tenant and the owner, the chat or run
it is made for, in MCP's request `_meta`, and a server that keeps state keys it by them (`tools/web_scraper.py`).

Sent only to Synapse's own servers that keep state by it (`SCOPED_SERVERS`): a tenant id and a chat id are not
something a third party's MCP server is told, and every other call is made exactly as before.
"""
from __future__ import annotations

#: The `_meta` key, in MCP's reverse-DNS form for a vendor's own keys.
SCOPE_KEY = "ai.synapse/scope"

#: The native servers (`core.tools_registry`) that read it.
SCOPED_SERVERS = frozenset({"web_scraper"})


def call_meta(server_name: str, owner: str | None) -> dict | None:
    """The `_meta` for a call to `server_name` made for `owner`, or None: not one of ours, or no owner to say."""
    if server_name not in SCOPED_SERVERS or not owner:
        return None
    from core.tenancy import get_tenant

    return {SCOPE_KEY: {"tenant": get_tenant(), "owner": str(owner)}}


def call_kwargs(server_name: str, owner: str | None) -> dict:
    """`session.call_tool`'s keyword for it: `meta=`, or nothing at all, so a call to any other server is made
    exactly as before."""
    meta = call_meta(server_name, owner)
    return {"meta": meta} if meta else {}


def scope_from(meta) -> tuple[str, str] | None:
    """`(tenant, owner)` from a request's `_meta`, or None when it names neither fully. For the server's side."""
    if meta is None:
        return None
    extra = meta if isinstance(meta, dict) else (getattr(meta, "model_extra", None) or {})
    scope = extra.get(SCOPE_KEY)
    if not isinstance(scope, dict):
        return None
    tenant, owner = scope.get("tenant"), scope.get("owner")
    if not (isinstance(tenant, str) and tenant and isinstance(owner, str) and owner):
        return None
    return tenant, owner
