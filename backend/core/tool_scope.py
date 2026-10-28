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


#: The `_meta` key for the one database a call to the SQL tool may use (F1118).
DB_KEY = "ai.synapse/db"

#: The native servers handed their connection with each call, rather than opening the store themselves.
HANDOVER_SERVERS = frozenset({"sql"})


def _load_settings() -> dict:
    from core.config import load_settings

    return load_settings()


async def call_kwargs_for(server_name: str, owner: str | None, arguments: dict | None) -> dict:
    """`call_kwargs`, plus, for the SQL tool, the database the call names, resolved in this process.

    The caller reads the tenant's database configs here, where an embedder's resolver turns a stored
    reference into its value, so the SQL tool's server is handed the one connection a call uses and
    never opens the store. Every other server is called exactly as `call_kwargs` says.
    """
    kwargs = call_kwargs(server_name, owner)
    if server_name not in HANDOVER_SERVERS:
        return kwargs
    database = await _database_for(arguments or {})
    if database is None:
        return kwargs
    meta = dict(kwargs.get("meta") or {})
    meta[DB_KEY] = database
    return {"meta": meta}


async def _database_for(arguments: dict) -> dict | None:
    """`{id, connection_string, allow_write}` for the database a call names, or None when none is set."""
    settings = _load_settings()
    allow_write = bool(settings.get("allow_db_write", False))
    db_id = arguments.get("db_id") or None
    if db_id:
        from core.store import collections

        config = next((c for c in await collections.load("db_configs") if c.get("id") == db_id), None)
        if not config or not config.get("connection_string"):
            return None
        return {"id": db_id, "connection_string": config["connection_string"], "allow_write": allow_write}
    fallback = settings.get("sql_connection_string", "")
    if not fallback:
        return None
    return {"id": None, "connection_string": fallback, "allow_write": allow_write}


def handed_database(meta) -> dict | None:
    """The database a request's `_meta` hands the SQL tool's server, or None. For the server's side."""
    if meta is None:
        return None
    extra = meta if isinstance(meta, dict) else (getattr(meta, "model_extra", None) or {})
    database = extra.get(DB_KEY)
    if not isinstance(database, dict) or not isinstance(database.get("connection_string"), str):
        return None
    return database
