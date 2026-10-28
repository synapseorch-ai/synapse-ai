"""A database URL saved without a driver opens with one that is installed (F1174).

SQLAlchemy 2.1 opens a bare `postgresql://` with psycopg (version 3), where 2.0 used
psycopg2, so a string saved under one release stopped opening under the next. Every
place that makes an engine from a saved string names an installed driver first.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from core import db_drivers
from core.db_drivers import with_installed_driver

SAVED = "postgresql://reporting:not-a-real-password@db.example.com:5432/analytics"


def _only(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    monkeypatch.setattr(db_drivers, "_installed", lambda module: module in modules)


class TestABareSchemeGetsAnInstalledDriver:
    def test_postgres_prefers_psycopg2(self, monkeypatch):
        _only(monkeypatch, "psycopg2", "psycopg")

        assert make_url(with_installed_driver(SAVED)).drivername == "postgresql+psycopg2"

    def test_postgres_takes_psycopg_when_it_is_the_one_installed(self, monkeypatch):
        _only(monkeypatch, "psycopg")

        assert make_url(with_installed_driver(SAVED)).drivername == "postgresql+psycopg"

    def test_mysql_takes_pymysql_when_mysqlclient_is_absent(self, monkeypatch):
        _only(monkeypatch, "pymysql")

        url = with_installed_driver("mysql://u:p@db.example.com/x")

        assert make_url(url).drivername == "mysql+pymysql"

    def test_the_rest_of_the_url_is_kept(self, monkeypatch):
        _only(monkeypatch, "psycopg2")

        url = make_url(with_installed_driver(f"{SAVED}?sslmode=require"))

        assert (url.username, url.password, url.host, url.port, url.database, dict(url.query)) == (
            "reporting", "not-a-real-password", "db.example.com", 5432, "analytics", {"sslmode": "require"},
        )

    @pytest.mark.parametrize(
        "saved",
        ["postgresql+psycopg://u:p@h/x", "mysql+pymysql://u:p@h/x", "sqlite:///local.db", "mssql://u:p@h/x"],
    )
    def test_a_named_driver_or_another_backend_is_left_alone(self, monkeypatch, saved):
        _only(monkeypatch, "psycopg2", "psycopg", "pymysql")

        assert with_installed_driver(saved) == saved


class TestNoSiteHandsSqlalchemyABareScheme:
    class _Stop(Exception):
        pass

    def _capture(self, seen):
        def create_engine(url, **kwargs):
            seen.append(make_url(url).drivername)
            raise self._Stop

        return create_engine

    def test_the_sql_tool(self, monkeypatch):
        from tools import sql_agent

        seen: list[str] = []
        monkeypatch.setattr(sql_agent, "create_engine", self._capture(seen))
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            monkeypatch.delenv(name, raising=False)

        with pytest.raises(self._Stop):
            sql_agent._connect(SAVED)

        assert seen and "+" in seen[0], seen

    def test_the_schema_refresh(self, monkeypatch):
        import sqlalchemy

        from core.routes import db_configs

        seen: list[str] = []
        monkeypatch.setattr(sqlalchemy, "create_engine", self._capture(seen))

        with pytest.raises(self._Stop):
            db_configs._read_schema(SAVED)

        assert seen and "+" in seen[0], seen
