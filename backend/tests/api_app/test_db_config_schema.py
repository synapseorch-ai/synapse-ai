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
