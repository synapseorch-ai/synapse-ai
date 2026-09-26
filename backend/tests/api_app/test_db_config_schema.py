"""
A SQLite database connection can fetch its schema (F208).

Refresh schema passed `connect_args={"connect_timeout": 10}` to every engine.
SQLite's driver has no such argument, so a SQLite config answered 400
"Connection() got an unexpected keyword argument 'connect_timeout'" and agents
never got its schema. The timeout is a Postgres and MySQL argument, and goes
to those only.
"""
import sqlite3

import pytest


@pytest.fixture
def sqlite_db(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)")
    return path


async def test_a_sqlite_config_fetches_its_schema(client, sqlite_db):
    config = {"id": "db_f208", "name": "Shop", "db_type": "sqlite",
              "connection_string": f"sqlite:///{sqlite_db}"}
    assert (await client.post("/api/db-configs", json=config)).status_code == 200

    resp = await client.post("/api/db-configs/db_f208/refresh-schema")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "connected"
    assert "orders(id (INTEGER), total (REAL))" in resp.json()["schema_info"]

    (stored,) = (await client.get("/api/db-configs")).json()
    assert (stored["status"], stored["error_message"]) == ("connected", None)


@pytest.mark.parametrize("url,args", [
    ("postgresql://u:p@db.example:5432/app", {"connect_timeout": 10}),
    ("postgresql+psycopg://u:p@db.example/app", {"connect_timeout": 10}),
    ("mysql+pymysql://u:p@db.example/app", {"connect_timeout": 10}),
    ("mariadb+pymysql://u:p@db.example/app", {"connect_timeout": 10}),
    ("sqlite:////tmp/x.db", {}),
    ("mssql+pyodbc://u:p@dsn", {}),
])
def test_the_timeout_goes_only_where_the_driver_takes_it(url, args):
    from core.routes.db_configs import _connect_args
    assert _connect_args(url) == args


async def test_a_slow_database_does_not_freeze_the_backend(client, monkeypatch):
    """F234: the connect ran on the event loop, so every other request waited
    for as long as the database did (a GET took 9.1 s behind an unreachable
    Postgres). A connect that takes a second must leave the loop turning."""
    import asyncio
    import time

    import sqlalchemy

    def slow_engine(*args, **kwargs):
        time.sleep(1.0)
        raise sqlalchemy.exc.OperationalError("connect", {}, Exception("timeout expired"))

    monkeypatch.setattr(sqlalchemy, "create_engine", slow_engine)
    config = {"id": "db_f234", "name": "Far", "db_type": "postgres",
              "connection_string": "postgresql://u:p@10.255.255.1:5432/app"}
    assert (await client.post("/api/db-configs", json=config)).status_code == 200

    ticks = 0
    done = asyncio.Event()

    async def tick():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    ticker = asyncio.create_task(tick())
    resp = await client.post("/api/db-configs/db_f234/refresh-schema")
    done.set()
    await ticker
    assert resp.status_code == 400 and "timeout expired" in resp.json()["detail"]
    assert ticks >= 20, f"the loop turned {ticks} times in a 1 s connect"
