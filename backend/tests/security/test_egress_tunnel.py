"""The SQL tool's database connection leaves through the host's egress proxy when there is one (F1117).

The proxy here is the engine's own (`core.egress_proxy.EgressProxy`), which answers
`CONNECT` and dials only what `core.egress_guard` allows. `localhost` is allowed for
the test's stand-in database, a loopback echo server. Without `core.egress_tunnel`
the SQL tool hands its driver the URL as saved, so each test below fails.
"""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest
from sqlalchemy.engine import make_url


@pytest.fixture
def database():
    """A loopback echo server standing in for the database."""
    server = socket.create_server(("127.0.0.1", 0))

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def proxy(monkeypatch):
    """The engine's egress proxy on its own loop, named as the host's proxy."""
    from core.egress_proxy import EgressProxy

    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost")
    for name in ("NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    instance = EgressProxy()
    url = asyncio.run_coroutine_threadsafe(instance.start(), loop).result(5)
    monkeypatch.setenv("HTTPS_PROXY", url)
    monkeypatch.setenv("https_proxy", url)
    yield instance
    asyncio.run_coroutine_threadsafe(instance.close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)


def _round_trip(address: tuple[str, int]) -> bytes:
    with socket.create_connection(address, timeout=5) as conn:
        conn.sendall(b"ping")
        return conn.recv(4)


def test_postgres_dials_the_tunnel_and_keeps_its_host_name(proxy, database):
    from tools.sql_agent import _through_egress

    url = make_url(_through_egress(f"postgresql://u:p@localhost:{database}/db"))

    assert url.host == "localhost", "TLS is checked against the database's own name"
    assert url.query["hostaddr"] == "127.0.0.1"
    assert url.port != database
    assert _round_trip(("127.0.0.1", url.port)) == b"ping"


def test_mysql_dials_the_tunnel(proxy, database):
    from tools.sql_agent import _through_egress

    url = make_url(_through_egress(f"mysql+pymysql://u:p@localhost:{database}/db"))

    assert (url.host, url.port != database) == ("127.0.0.1", True)
    assert _round_trip((url.host, url.port)) == b"ping"


def test_an_address_the_proxy_refuses_is_never_reached(proxy):
    from core.egress_tunnel import TunnelRefused, open_tunnel

    with pytest.raises(TunnelRefused, match="refused"):
        open_tunnel("10.255.255.1", 5432, timeout=5)


def test_with_no_proxy_the_url_is_dialled_as_saved(monkeypatch):
    """A single-user install with no egress proxy: unchanged, so a local database still works."""
    from tools.sql_agent import _through_egress

    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    saved = "postgresql://u:p@localhost:5432/db"

    assert _through_egress(saved) == saved


def test_a_host_no_proxy_covers_is_dialled_as_saved(proxy, monkeypatch):
    from tools.sql_agent import _through_egress

    monkeypatch.setenv("NO_PROXY", "db.internal")
    saved = "postgresql://u:p@db.internal:5432/db"

    assert _through_egress(saved) == saved
