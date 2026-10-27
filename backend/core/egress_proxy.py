"""One egress proxy for every browser Synapse starts.

`core/egress_guard.py` decides where a fetch may go. A browser makes its own
requests — redirects, a page's scripts, in-page fetches, WebSockets, service
workers — and a hook inside the browser does not see every one of them. A
proxy does: the browser is started with this one as its proxy, Playwright puts
loopback through it too (`<-loopback>` in Chromium's bypass list), and it opens
no connection the guard refuses.

It also closes the window the guard leaves open on purpose. The guard resolves
a name, and the fetch it guards resolves it again; here the address that was
checked (`egress_guard.checked_addresses`) is the address dialled, and the name
is never looked up a second time.

What it speaks is the least a browser needs: `CONNECT host:port` for TLS and
WebSockets, and an absolute-form `http://` request for plain HTTP, sent on as
one request per connection (`Connection: close`) so a second request on the
same connection cannot name another host. A refusal is a `403` whose body is
the guard's message (it names the host asked for, never an address it
resolved to); anything that is not a proxy request is a `400`.

It listens on loopback and needs no credentials: what it reaches is the public
internet, which anything on the machine reaches already.

Deliberately stdlib-only, like the guard: the scraper's subprocess starts its own.
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

from core import egress_guard

#: What a Chromium behind this proxy is started with. WebRTC's UDP never goes through a proxy; with this flag WebRTC
#: takes the proxied path or none.
CHROMIUM_ARGS = ("--force-webrtc-ip-handling-policy=disable_non_proxied_udp",)

#: A request head larger than this is not one a browser sends.
_HEAD_LIMIT = 64 * 1024
_HEAD_TIMEOUT_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 10.0
#: After the browser closes its side, how long the other side has to finish.
_DRAIN_SECONDS = 30.0

_REASONS = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}

#: Headers meant for the proxy itself, never sent on.
_FOR_THE_PROXY = {"proxy-connection", "proxy-authorization", "proxy-authenticate"}


class _BadRequest(ValueError):
    pass


class _Request:
    def __init__(self, head: bytes):
        lines = head[:-4].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            raise _BadRequest("not an HTTP request")
        self.method, target, version = parts
        self.tunnel = self.method.upper() == "CONNECT"
        try:
            if self.tunnel:
                url = urlsplit(f"//{target}")
                self.host, self.port = url.hostname, url.port
                if not self.host or not self.port:
                    raise _BadRequest("CONNECT needs host:port")
                self.forward = b""
                return
            url = urlsplit(target)
            self.host, self.port = url.hostname, url.port or 80
        except ValueError as exc:
            raise _BadRequest(f"not an address: {exc}") from None
        if url.scheme != "http" or not self.host:
            raise _BadRequest("a proxy request is CONNECT host:port or an absolute http:// URL")

        headers = []
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() not in _FOR_THE_PROXY:
                headers.append((name.strip(), value.strip()))
        connection = {v.strip().lower() for n, value in headers if n.lower() == "connection"
                      for v in value.split(",")}
        upgrade = "upgrade" in connection and any(n.lower() == "upgrade" for n, _ in headers)
        if not upgrade:
            headers = [(n, v) for n, v in headers if n.lower() not in ("connection", "keep-alive")]
            headers.append(("Connection", "close"))
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        self.forward = "".join([f"{self.method} {path} {version}\r\n",
                                *(f"{n}: {v}\r\n" for n, v in headers), "\r\n"]).encode("latin-1")


class EgressProxy:
    """A loopback proxy that dials only what `core.egress_guard` allows. `start()` once, `close()` when done."""

    host = "127.0.0.1"

    def __init__(self) -> None:
        self.port = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.base_events.Server | None = None
        self._starting: asyncio.Future | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._closed = False

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> str:
        """Listen, once; every caller waits for the same start."""
        if self._starting is None:
            self.loop = asyncio.get_running_loop()
            self._starting = asyncio.ensure_future(self._listen())
        await asyncio.shield(self._starting)
        return self.url

    async def _listen(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, 0, limit=_HEAD_LIMIT)
        self.port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        upstream: asyncio.StreamWriter | None = None
        host = ""
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_SECONDS)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError):
                return
            request = _Request(head)
            host = request.host
            addresses = await egress_guard.checked_addresses(host)
            up_reader, upstream = await _dial(addresses, request.port)
            self._writers.add(upstream)
            if request.tunnel:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.write(request.forward)
            await _relay(reader, writer, up_reader, upstream)
        except _BadRequest as exc:
            _answer(writer, 400, str(exc))
        except egress_guard.BlockedEgress as exc:
            print(f"egress proxy: refused {host}: {exc}", file=sys.stderr)
            _answer(writer, 403, str(exc))
        except (OSError, TimeoutError):
            _answer(writer, 502, f"could not reach {host}")
        finally:
            for w in (upstream, writer):
                if w is not None:
                    self._writers.discard(w)
                    w.close()


async def _dial(addresses: list[str], port: int):
    """A connection to the first of `addresses` that answers. Never the name: the addresses are what was checked."""
    last: BaseException = OSError("no address")
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS)
        except (OSError, TimeoutError) as exc:
            last = exc
    raise last


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except ConnectionError:
        pass


async def _relay(client_r, client_w, up_r, up_w) -> None:
    """Both ways until the far side is done. The browser closing its side gives the far side a moment to finish."""
    outbound = asyncio.ensure_future(_pipe(client_r, up_w))
    inbound = asyncio.ensure_future(_pipe(up_r, client_w))
    try:
        done, _ = await asyncio.wait({outbound, inbound}, return_when=asyncio.FIRST_COMPLETED)
        if inbound not in done:
            if up_w.can_write_eof():
                try:
                    up_w.write_eof()
                except OSError:
                    pass
            await asyncio.wait({inbound}, timeout=_DRAIN_SECONDS)
    finally:
        for task in (outbound, inbound):
            task.cancel()


def _answer(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    body = f"{reason}\n".encode()
    try:
        writer.write(f"HTTP/1.1 {status} {_REASONS[status]}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nX-Synapse-Egress: refused\r\nConnection: close\r\n\r\n"
                     .encode() + body)
    except (OSError, RuntimeError):
        pass


_shared: EgressProxy | None = None


async def shared() -> EgressProxy:
    """This event loop's proxy, started on first use. A closed one, or one from another loop, is replaced."""
    global _shared
    loop = asyncio.get_running_loop()
    proxy = _shared
    if proxy is None or proxy.closed or (proxy.loop is not None and proxy.loop is not loop):
        proxy = _shared = EgressProxy()
    await proxy.start()
    return proxy
