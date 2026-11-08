"""
A query no longer holds up the SQL tool server (F299).

`sql` is one stdio tool server for the whole workspace, like bash (F246). It ran
every query — and every schema read — synchronously inside its async handler, so
while one query ran the server answered nothing: not another call, and not the
5 s ping each caller sends first, after which a caller skips its call as
"session unresponsive". Two agent branches of one Parallel may now query at once
(F255), which makes that a common case.

The database work now runs in a thread. Its results and refusals are the ones
it always gave.
"""
import asyncio
import json
import time

import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def served(monkeypatch):
    """The handler as a running server has it: its bootstrap is over."""
    from core import tool_server

    async def started():
        return None

    monkeypatch.setattr(tool_server, "ready", started)


@pytest.fixture
def database(tmp_path, monkeypatch, served):
    """A real SQLite file with 60 rows, handed to the tool as its engine."""
    from tools import sql_agent

    engine = create_engine(f"sqlite:///{tmp_path / 'shop.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        for i in range(60):
            c.execute(text("INSERT INTO items (name) VALUES (:n)"), {"n": f"item {i}"})

    async def get_db_engine(db_id=None):
        return engine, inspect(engine)

    monkeypatch.setattr(sql_agent, "get_db_engine", get_db_engine)
    return engine


class _SlowEngine:
    """An engine whose every statement takes a second of wall-clock, blocking."""

    class _Result:
        def keys(self):
            return ["n"]

        def fetchall(self):
            return [(1,)]

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            time.sleep(1.0)
            return _SlowEngine._Result()

    def connect(self):
        return self._Connection()


async def _call(name, **arguments):
    from tools import sql_agent

    return (await sql_agent.call_tool(name, arguments))[0].text


async def test_the_server_keeps_answering_while_a_query_runs(served, monkeypatch):
    from tools import sql_agent

    async def get_db_engine(db_id=None):
        return _SlowEngine(), None

    monkeypatch.setattr(sql_agent, "get_db_engine", get_db_engine)

    t0 = time.monotonic()
    query = asyncio.create_task(_call("run_sql_query", query="SELECT 1 AS n"))
    await asyncio.sleep(0.05)  # the query starts, and runs for a second
    lag = time.monotonic() - t0
    assert json.loads(await query) == [{"n": 1}]
    assert lag < 0.5, f"a 50 ms timer fired after {lag:.2f}s — the query held the server"


async def test_a_read_returns_its_rows_capped_at_fifty(database):
    out = await _call("run_sql_query", query="SELECT id, name FROM items ORDER BY id")
    body, _, tail = out.partition("\n...")
    rows = json.loads(body)
    assert len(rows) == 50 and rows[0] == {"id": 1, "name": "item 0"}
    assert tail == "(Truncated to 50 rows)"


async def test_a_write_is_still_refused_when_writes_are_off(database):
    out = await _call("run_sql_query", query="DELETE FROM items")
    assert out.startswith("BLOCKED: This query modifies the database")
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM items")).scalar() == 60


async def test_a_write_commits_when_writes_are_on(database, monkeypatch):
    from tools import sql_agent

    monkeypatch.setattr(sql_agent, "load_settings", lambda: {"allow_db_write": True})
    out = await _call("run_sql_query", query="DELETE FROM items WHERE id > 10")
    assert out == "Query executed successfully. Rows affected: 50"
    with database.connect() as c:
        assert c.execute(text("SELECT count(*) FROM items")).scalar() == 10


async def test_the_tables_and_their_schema_are_listed(database):
    assert await _call("list_tables") == "Found 1 tables:\n- items"
    schema = await _call("get_table_schema", table_names=["items"])
    assert "**Table: items**" in schema
    assert "- id (INTEGER) NULL (PK)" in schema or "- id (INTEGER) NOT NULL (PK)" in schema
    assert "- name (TEXT) NOT NULL" in schema


async def test_a_database_error_is_still_the_error_text(database):
    out = await _call("run_sql_query", query="SELECT * FROM no_such_table")
    assert out.startswith("Error: ") and "no_such_table" in out
