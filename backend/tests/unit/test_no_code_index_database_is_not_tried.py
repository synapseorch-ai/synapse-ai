"""With no code-index database set, starting up neither tries one nor says anything (F1494).

The Docker image started with "Warning: could not ensure database exists: missing "=" after "/postgres" in
connection info string": `_ensure_database_exists` built the admin address from an empty `sql_connection_string`
(nothing but "/postgres") and handed it to psycopg. Nothing was configured, so nothing was wrong; the line read as a
fault. A database that is set is still made if it is missing, and one that cannot be reached still says so.
"""
import os

import pytest

from services import code_indexer


@pytest.fixture
def connects(monkeypatch):
    """Every psycopg connection asked for, refused, as a server that isn't there would."""
    asked: list[str] = []

    def connect(url, **kwargs):
        asked.append(url)
        raise OSError("connection refused")

    monkeypatch.setattr(code_indexer, "COCOINDEX_AVAILABLE", True)
    monkeypatch.setattr(code_indexer.psycopg, "connect", connect)
    monkeypatch.delenv("COCOINDEX_DATABASE_URL", raising=False)
    return asked


def _set(monkeypatch, value):
    monkeypatch.setattr(code_indexer, "load_settings", lambda: {"sql_connection_string": value})


@pytest.mark.parametrize("value", ["", "   ", None])
def test_no_database_set_is_not_tried_and_says_nothing(monkeypatch, capsys, connects, value):
    _set(monkeypatch, value)
    code_indexer.init_cocoindex()
    assert connects == []
    assert capsys.readouterr().out == ""
    assert "COCOINDEX_DATABASE_URL" not in os.environ


def test_a_database_that_is_set_is_still_made_and_its_failure_said(monkeypatch, capsys, connects):
    _set(monkeypatch, "postgresql://synapse@db.internal:5432/codeindex")
    code_indexer.init_cocoindex()
    assert connects == ["postgresql://synapse@db.internal:5432/postgres"]
    out = capsys.readouterr().out
    assert "could not ensure database exists: connection refused" in out
    assert os.environ["COCOINDEX_DATABASE_URL"] == "postgresql://synapse@db.internal:5432/codeindex"
