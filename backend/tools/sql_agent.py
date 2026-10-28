
from mcp.types import ToolAnnotations, Tool, TextContent, ImageContent, EmbeddedResource
import mcp.types as types
from mcp.server import Server
import asyncio
import json
import os
import re
import sys
from sqlalchemy import create_engine, inspect, text
from core.config import load_settings

# Initialize MCP Server
app = Server("sql-mcp-server")

# Engine cache: db_id (or a handed connection's id and hash) -> (engine, inspector)
_engines: dict[str, tuple] = {}


def _handover_required() -> bool:
    """On a multi-tenant worker this server is handed its connection with each call and never opens
    the store (F1118): `core/scale/worker_server_module.py` spawns it with this set."""
    return os.getenv("SYNAPSE_SQL_HANDOVER", "").strip() == "required"

# Keywords that can modify something. Checked anywhere in the statement, not
# only at the front — see _is_write_query.
_WRITE_KEYWORDS = {
    "insert", "update", "delete", "drop", "create", "alter",
    "truncate", "replace", "merge", "upsert", "grant", "revoke",
    # Statements that mutate without being obviously a write. `copy ... from`
    # loads a file into a table; `do` runs a procedural block; `call` runs a
    # procedure; `set` changes session state a later statement can rely on.
    "copy", "do", "call", "set", "lock", "vacuum", "reindex", "refresh",
    "comment", "rename", "attach", "detach", "pragma",
}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _strip_comments_and_literals(sql: str) -> str:
    """Blank out comments and quoted text so keyword scanning is not fooled.

    Without this, `WHERE status = 'delete me'` looks like a delete, and
    `-- drop table x` in a comment does too. Replaced with spaces rather than
    removed so offsets and word boundaries survive.
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i:i + 2]
        if two == "--":
            end = sql.find("\n", i)
            i = n if end == -1 else end
        elif two == "/*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in "'\"":
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    # Doubled quote is an escaped quote, not the end.
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _statement_count(query: str) -> int:
    """How many statements this is, ignoring a trailing semicolon."""
    cleaned = _strip_comments_and_literals(query)
    return len([part for part in cleaned.split(";") if part.strip()])


def _is_write_query(query: str) -> bool:
    """True if the query could modify anything.

    **Scans the whole statement, not just its first word.** The first-word
    version passed `WITH x AS (SELECT 1) DELETE FROM t` as a read, because the
    first word is `with` — so a read-only connection would happily run the
    delete. It also passed `SELECT 1; DROP TABLE t` on any driver that allows
    multiple statements.

    Deliberately conservative: a keyword anywhere in the cleaned statement
    counts. That can refuse an exotic read whose identifier happens to be a bare
    keyword, which is the right direction for a gate whose other failure mode is
    an unauthorised write. Comments and string literals are blanked first, so
    ordinary data containing the word "delete" does not trip it.
    """
    cleaned = _strip_comments_and_literals(query).lower()
    return any(word in _WRITE_KEYWORDS for word in _WORD_RE.findall(cleaned))


async def _load_db_configs() -> list[dict]:
    """Load the tenant's database configs from the store.

    This process is a stdio MCP server rather than the backend, so it opens the
    store itself through SYNAPSE_DB_URL — the same way the engine finds it.
    """
    from core.store import collections

    try:
        return await collections.load("db_configs")
    except Exception as exc:
        # Loud, because the caller turns an empty list into "no databases are
        # configured" — which is indistinguishable from a workspace that has
        # configured none, and was exactly the wrong diagnosis when a store read
        # failed. `collections.load` already skips individual documents it
        # cannot resolve, so reaching here means the read itself failed.
        print(f"[sql_agent] could not load db_configs: {exc!r}", file=sys.stderr, flush=True)
        return []


# Every call below that reaches the database runs in a thread (F299). This is
# one stdio server for the whole workspace, and the MCP server runs each call in
# a task of its own: a query run inline held the whole process, so every other
# SQL call waited behind it — and so did the ping each caller sends first, which
# after 5 s skips the call as "session unresponsive". F246 was the same thing for
# bash. SQLAlchemy's engine and its pool are safe to share across threads.


#: Each backend's default port, for a URL that names none.
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mssql": 1433}


def _through_egress(conn_str: str):
    """The URL the driver dials: through the host's egress proxy when it names one (F1117).

    `core.egress_tunnel.forwarder` gives a loopback address that reaches the database
    by `CONNECT` through the proxy. Postgres dials it as `hostaddr` and keeps `host`,
    so TLS is still checked against the database's own name. With no proxy, or a host
    `NO_PROXY` covers, the URL is unchanged.
    """
    from sqlalchemy.engine import make_url

    from core.egress_tunnel import forwarder

    url = make_url(conn_str)
    backend = url.get_backend_name()
    if not url.host or backend not in _DEFAULT_PORTS:
        return conn_str
    local = forwarder(url.host, url.port or _DEFAULT_PORTS[backend])
    if local is None:
        return conn_str
    if backend == "postgresql":
        return url.set(port=local[1]).update_query_dict({"hostaddr": local[0]})
    return url.set(host=local[0], port=local[1])


def _connect(conn_str: str):
    """A new engine and its inspector. `inspect()` connects to the database."""
    engine = create_engine(_through_egress(conn_str), echo=False)
    return engine, inspect(engine)


def _table_schemas(inspector, table_names: list[str]) -> str:
    schemas = []
    for table in table_names:
        try:
            columns = inspector.get_columns(table)
            fks = inspector.get_foreign_keys(table)
            pk = inspector.get_pk_constraint(table)
            col_strings = []
            for col in columns:
                nullable = "NULL" if col['nullable'] else "NOT NULL"
                is_pk = " (PK)" if col['name'] in pk.get('constrained_columns', []) else ""
                col_strings.append(f"- {col['name']} ({col['type']}) {nullable}{is_pk}")
            fk_strings = [
                f"-> FK to {fk['referred_table']}.{fk['referred_columns'][0]} on {fk['constrained_columns'][0]}"
                for fk in fks
            ]
            schema_text = (
                f"**Table: {table}**\nColumns:\n" + "\n".join(col_strings) +
                ("\nForeign Keys:\n" + "\n".join(fk_strings) if fk_strings else "")
            )
            schemas.append(schema_text)
        except Exception as e:
            schemas.append(f"Error inspecting table {table}: {str(e)}")
    return "\n\n".join(schemas)


def _run_query(engine, query: str, is_write: bool) -> str:
    with engine.connect() as connection:
        result = connection.execute(text(query))
        # Commit for write operations so changes persist
        if is_write:
            connection.commit()
            return f"Query executed successfully. Rows affected: {result.rowcount}"
        keys = list(result.keys())
        rows = [dict(zip(keys, row)) for row in result.fetchall()]
        if len(rows) > 50:
            rows = rows[:50]
            suffix = "\n...(Truncated to 50 rows)"
        else:
            suffix = ""
        return json.dumps(rows, default=str, indent=2) + suffix


async def get_db_engine(db_id: str | None = None, *, handed: dict | None = None):
    """Return (engine, inspector) for the given db_id, or fall back to global setting.

    `handed` is the connection the caller sent with the call (`core.tool_scope.handed_database`).
    When it is there it is the only one used; when a handover is required and it is not, nothing is
    opened.
    """
    global _engines

    if handed is not None:
        import hashlib

        digest = hashlib.sha256(handed["connection_string"].encode()).hexdigest()[:16]
        cache_key = f"handed:{handed.get('id') or '__global__'}:{digest}"
        if cache_key not in _engines:
            _engines[cache_key] = await asyncio.to_thread(_connect, handed["connection_string"])
        return _engines[cache_key]

    if _handover_required():
        if db_id:
            raise ValueError(f"No database config found for db_id='{db_id}'.")
        raise ValueError("No db_id provided and no global SQL connection string found in settings.")

    if db_id:
        if db_id in _engines:
            return _engines[db_id]
        configs = await _load_db_configs()
        config = next((c for c in configs if c.get("id") == db_id), None)
        if not config:
            raise ValueError(f"No database config found for db_id='{db_id}'.")
        conn_str = config.get("connection_string", "")
        if not conn_str:
            raise ValueError(f"Database config '{db_id}' has no connection string.")
        engine, inspector = await asyncio.to_thread(_connect, conn_str)
        _engines[db_id] = (engine, inspector)
        return engine, inspector

    # Fallback: global sql_connection_string from settings
    settings = load_settings()
    db_url = settings.get("sql_connection_string", "")
    if not db_url:
        raise ValueError("No db_id provided and no global SQL connection string found in settings.")
    cache_key = "__global__"
    if cache_key in _engines:
        return _engines[cache_key]
    engine, inspector = await asyncio.to_thread(_connect, db_url)
    _engines[cache_key] = (engine, inspector)
    return engine, inspector


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    db_id_prop = {
        "db_id": {
            "type": "string",
            "description": "The ID of the database config to use (from LINKED DATABASES in your system prompt). Required when multiple databases are configured."
        }
    }
    return [
        types.Tool(
            name="list_tables",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description="List all tables in a database. Provide db_id when multiple databases are linked.",
            inputSchema={
                "type": "object",
                "properties": db_id_prop,
            }
        ),
        types.Tool(
            name="get_table_schema",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
            description="Get the detailed schema (columns, types, foreign keys) for specific table(s). Provide db_id when multiple databases are linked.",
            inputSchema={
                "type": "object",
                "properties": {
                    **db_id_prop,
                    "table_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of table names to inspect."
                    }
                },
                "required": ["table_names"]
            }
        ),
        types.Tool(
            name="run_sql_query",
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
            description=(
                "Execute a SQL query against a linked database. "
                "Provide db_id when multiple databases are linked. "
                "Write queries (INSERT/UPDATE/DELETE/DROP/etc.) require allow_db_write to be enabled in settings "
                "AND explicit user confirmation BEFORE calling this tool."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **db_id_prop,
                    "query": {
                        "type": "string",
                        "description": "The SQL query to execute."
                    }
                },
                "required": ["query"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    from core.tool_scope import handed_database

    try:
        meta = app.request_context.meta
    except LookupError:
        meta = None
    return await _call(name, arguments, handed=handed_database(meta))


async def _call(name: str, arguments: dict, *, handed: dict | None) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    # The tenant, the store and this tenant's settings are established by
    # `bootstrap()`, which now runs alongside the handshake rather than ahead
    # of it. A handler is the first thing that actually needs any of them.
    # Not when the connection is handed over: then there is no store to open.
    if not _handover_required():
        from core.tool_server import ready

        await ready()

    try:
        db_id = arguments.get("db_id") or None
        engine, inspector = await (get_db_engine(db_id, handed=handed) if handed is not None else get_db_engine(db_id))

        if name == "list_tables":
            tables = await asyncio.to_thread(inspector.get_table_names)
            return [types.TextContent(
                type="text",
                text=f"Found {len(tables)} tables:\n" + "\n".join([f"- {t}" for t in tables])
            )]

        elif name == "get_table_schema":
            table_names = arguments.get("table_names", [])
            schemas = await asyncio.to_thread(_table_schemas, inspector, table_names)
            return [types.TextContent(type="text", text=schemas)]

        elif name == "run_sql_query":
            query = arguments.get("query", "").strip()

            # Refused whatever the write setting says. One statement per call
            # is all any caller needs, and a second one is the shape an
            # injection takes — `SELECT 1; DROP TABLE t` reads as a select to
            # anything that only inspects the start.
            if _statement_count(query) > 1:
                return [types.TextContent(
                    type="text",
                    text=(
                        "BLOCKED: send one statement per call. "
                        "Multiple statements separated by ';' are not accepted."
                    )
                )]

            if _is_write_query(query):
                if handed is not None:
                    allow_db_write = bool(handed.get("allow_write", False))
                else:
                    allow_db_write = load_settings().get("allow_db_write", False)
                if not allow_db_write:
                    refusal = (handed or {}).get("write_refusal")
                    return [types.TextContent(
                        type="text",
                        text=(
                            f"BLOCKED: This query modifies the database. {refusal}" if refusal else
                            "BLOCKED: This query modifies the database and DB write access is currently disabled. "
                            "A user with admin access must enable 'Allow agents to modify database' in General Settings."
                        )
                    )]

            out = await asyncio.to_thread(_run_query, engine, query, _is_write_query(query))
            return [types.TextContent(type="text", text=out)]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    # This process serves exactly one tenant, and it has to be told which —
    # see core/tool_server.py.
    # Serve first, bootstrap alongside. `bootstrap()` reads Postgres, and
    # putting that ahead of `stdio_server()` meant the MCP handshake waited
    # on a database — a cold serverless resume expired the 60s bound and
    # took the whole chat turn with it. Handlers wait via `ready()`.
    from core.tool_server import serve

    await serve(app)

if __name__ == "__main__":
    asyncio.run(main())
