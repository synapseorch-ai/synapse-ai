"""A repo's id reaches the code index's database as a quoted name, never as SQL (F1480, CodeQL py/sql-injection).

DELETE /api/repos/{repo_id} took the id from the URL (POST /api/repos takes one from its body), and `drop_index` ran
f'DROP TABLE IF EXISTS "ci_{repo_id}__emb" CASCADE;' against the Postgres in Settings' sql_connection_string:
`x", other; --` dropped the table `other`. Each name built from a repo id is now psycopg's `sql.Identifier`, and the
route removes only a repo that is there.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402

import services.code_indexer as code_indexer  # noqa: E402

HOSTILE = 'x", other; --'


class _Cursor:
    def __init__(self, seen, rows):
        self.seen, self.rows = seen, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.seen.append(query)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Conn:
    def __init__(self, seen, rows):
        self.seen, self.rows = seen, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cursor(self.seen, self.rows)

    def commit(self):
        pass


@pytest.fixture
def executed(monkeypatch):
    seen: list = []
    rows: list = [("public.t",), (3,)]
    monkeypatch.setattr(code_indexer, "COCOINDEX_AVAILABLE", True)
    monkeypatch.setattr(code_indexer, "_get_db_url", lambda: "postgresql://u@localhost/db")
    monkeypatch.setattr(code_indexer.psycopg, "connect", lambda *a, **k: _Conn(seen, rows))
    return seen


def _rendered(query) -> str:
    return query.as_string(None) if isinstance(query, sql.Composable) else query


def test_dropping_an_index_quotes_the_repo_id_as_a_name(executed):
    code_indexer.drop_index(HOSTILE)
    drops = [q for q in executed if "DROP" in _rendered(q)]
    assert len(drops) == 2
    for query in drops:
        assert isinstance(query, sql.Composed), "a DROP built as a string"
    assert [_rendered(q) for q in drops] == [
        'DROP TABLE IF EXISTS "ci_x"", other; --__emb" CASCADE',
        'DROP TABLE IF EXISTS "ci_x"", other; --__cocoindex_tracking" CASCADE',
    ]


def test_no_query_is_a_string_holding_the_id(executed):
    code_indexer.drop_index(HOSTILE)
    code_indexer.get_index_status(HOSTILE)
    assert executed
    assert not [q for q in executed if isinstance(q, str) and "other;" in q]


def test_counting_an_index_quotes_its_name(executed):
    assert code_indexer.get_index_status(HOSTILE) == {"status": "indexed", "count": 3}
    count = next(q for q in executed if "COUNT" in _rendered(q))
    assert isinstance(count, sql.Composed)
    assert _rendered(count) == 'SELECT COUNT(*) FROM "ci_x"", other; --__emb"'


async def test_an_unknown_repo_is_not_found_and_nothing_is_dropped(client, monkeypatch):
    dropped: list[str] = []
    monkeypatch.setattr(code_indexer, "drop_index", dropped.append)
    resp = await client.delete("/api/repos/x%22%2C%20other%3B%20--")
    assert resp.status_code == 404
    assert dropped == []


async def test_a_listed_repo_is_removed_and_its_index_dropped(client, monkeypatch, tmp_path):
    dropped: list[str] = []
    monkeypatch.setattr(code_indexer, "drop_index", dropped.append)
    body = {"id": "repo_f1480", "name": "F1480", "path": str(tmp_path)}
    assert (await client.post("/api/repos", json=body)).status_code == 200
    assert (await client.delete("/api/repos/repo_f1480")).status_code == 200
    assert dropped == ["repo_f1480"]
    assert "repo_f1480" not in [r["id"] for r in (await client.get("/api/repos")).json()]
