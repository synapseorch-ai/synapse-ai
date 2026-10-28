"""Raw TCP through the host's egress proxy, for clients that speak no HTTP (F1117).

Where the environment names an egress proxy (`HTTPS_PROXY`, cloud's Smokescreen),
a process may have no other way out: cloud's fleet sits on a network with no
route to the internet. httpx and the browser already use the proxy. A database
driver or an SMTP client does not, so this opens their connection with
`CONNECT host:port` through it. The proxy, not this process, then decides where
the connection may go, on the address it dials.

Two shapes, one mechanism:

* `open_tunnel(host, port)`: a connected socket, for a client that can be handed
  one (cloud's mail sender overrides `smtplib.SMTP._get_socket`).
* `forwarder(host, port)`: a loopback address that forwards each connection made
  to it through `open_tunnel`, for a driver that only takes a host and a port
  (the SQL tool's).

With no proxy named, or a host `NO_PROXY` covers, the connection is made
directly, exactly as before: a single-user install still reaches the database on
its own machine. Stdlib only, and synchronous: drivers connect from threads.
"""
from __future__ import annotations

import socket
import threading
import urllib.request
from urllib.parse import urlsplit

#: The most of a proxy's answer to a CONNECT that is read before giving up on it.
_MAX_HEADER = 16 * 1024


class TunnelRefused(ConnectionError):
    """The egress proxy would not open the connection. The message is safe to show."""


def proxy_for(host: str) -> tuple[str, int] | None:
    """`(proxy_host, proxy_port)` for a connection to `host`, or None to connect directly."""
    proxies = urllib.request.getproxies_environment()
    url = proxies.get("https") or proxies.get("http")
    if not url or urllib.request.proxy_bypass_environment(host, proxies):
        return None
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or 80


def open_tunnel(host: str, port: int, timeout: float | None = 15.0) -> socket.socket:
    """A socket connected to `host:port`: through the egress proxy when there is one, else directly."""
    proxy = proxy_for(host)
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)

    sock = socket.create_connection(proxy, timeout=timeout)
    try:
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise TunnelRefused(f"the egress proxy closed the connection to {host}:{port}")
            head += chunk
            if len(head) > _MAX_HEADER:
                raise TunnelRefused(f"the egress proxy's answer for {host}:{port} was too long")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        status = status_line.split(" ", 2)
        if len(status) < 2 or status[1] != "200":
            raise TunnelRefused(f"the egress proxy refused {host}:{port} ({status_line.strip()})")
        # Anything after the blank line already belongs to the destination. A CONNECT
        # answer carries none in practice; refusing it keeps the stream exact.
        if head.split(b"\r\n\r\n", 1)[1]:
            raise TunnelRefused(f"the egress proxy sent data before {host}:{port} answered")
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _Forwarder:
    """A loopback listener whose every connection is a fresh tunnel to one destination."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.address = self._listener.getsockname()[:2]
        threading.Thread(target=self._serve, name=f"egress-tunnel[{host}:{port}]", daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(client,), daemon=True).start()

    def _one(self, client: socket.socket) -> None:
        try:
            upstream = open_tunnel(self.host, self.port)
        except OSError:
            # The driver sees its connection closed and reports a connect failure.
            client.close()
            return
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)
        client.close()
        upstream.close()


_forwarders: dict[tuple[str, int], _Forwarder] = {}
_lock = threading.Lock()


def forwarder(host: str, port: int) -> tuple[str, int] | None:
    """`("127.0.0.1", local_port)` that reaches `host:port` through the egress proxy, or None when
    the connection should be made directly (no proxy, or `NO_PROXY` covers it). One per destination
    per process."""
    if proxy_for(host) is None:
        return None
    with _lock:
        key = (host, int(port))
        if key not in _forwarders:
            _forwarders[key] = _Forwarder(host, int(port))
        return _forwarders[key].address
