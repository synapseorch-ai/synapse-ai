"""A saved database URL that names no driver gets one that is installed (F1174).

SQLAlchemy picks a default driver for a bare scheme, and the default moves between
releases: 2.0 opens `postgresql://` with psycopg2, 2.1 with psycopg (version 3). A
string saved under one release then fails to open under the next, with "No module
named …", on an install that has a working driver all along. `mysql://` has the same
gap: its default is mysqlclient, and the SQL tool ships PyMySQL.

So a URL with no driver is given an installed one before an engine is made from it.
A URL that names its driver is left exactly as it is.
"""
from __future__ import annotations

import importlib.util

#: For each backend, the drivers to try in order: (SQLAlchemy's driver name, its module).
_DRIVERS: dict[str, tuple[tuple[str, str], ...]] = {
    "postgresql": (("psycopg2", "psycopg2"), ("psycopg", "psycopg")),
    "mysql": (("mysqldb", "MySQLdb"), ("pymysql", "pymysql")),
}


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def with_installed_driver(url):
    """`url` (a string or a SQLAlchemy URL) with an installed driver named, when it named none.

    Returns what it was given when the URL already names a driver, or when its backend has no
    list here (sqlite, mssql), or when none of the listed drivers is installed (the error then
    names the default, as before).
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if "+" in parsed.drivername:
        return url
    for driver, module in _DRIVERS.get(parsed.drivername, ()):
        if _installed(module):
            return parsed.set(drivername=f"{parsed.drivername}+{driver}")
    return url
