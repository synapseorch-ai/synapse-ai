"""Download a model-supplied URL, with the egress policy applied to every hop (S6).

`core/egress_guard.py` decides *where* a request may go; this decides how a
download behaves once it is allowed to start. Kept apart because the guard is
deliberately stdlib-only and this needs an HTTP client.

Every tool that downloads a whole file from a URL a prompt chose wants the
same four things, and `parse_pdf` / `parse_xlsx` had none of them:

* **The destination checked** — and re-checked at every redirect, because the
  first URL being public says nothing about the second. Redirects are followed
  here, one hop at a time, never by the client.
* **A deadline for the whole download**, not just per read: a server that
  drips one byte a minute never trips a read timeout. On a fleet these tools
  are shared subprocesses, so a hung call is every tenant's hung call.
* **A byte cap, enforced while streaming.** A declared `Content-Length` over
  the cap is refused before the body is read; an undeclared one is cut off
  the moment it passes the cap, not after it has all arrived.
* **Messages safe to hand the model.** They name the URL the caller gave and
  the rule that refused it — never an address a name resolved to.

An unresolvable host is refused (`UnresolvedHost` is a `BlockedEgress`), as the
scraper does: the URL was invented by a model, so "unknown" is not "allowed".

`fetch` is the same policy for a caller that reads the answer itself — a
crawler (Knowledge's web sources, S1): it brings its own client (one pool for
many pages), sends its own headers (conditional GET), and gets the status,
the final URL and the headers back, with only a 2xx body read. A refusal by
an egress proxy (I1's Smokescreen, cloud) is a `FetchBlocked` too: a 407
answer to a plain-HTTP request carries `X-Smokescreen-Error`; on an HTTPS
tunnel httpcore turns the refused CONNECT into `httpx.ProxyError("407 …")`
and the header is lost, so both are read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from core import egress_guard

#: The largest download any caller gets. A PDF or spreadsheet an agent is asked
#: to read is well under this; anything larger is not a document to parse into
#: a prompt anyway.
MAX_BYTES = 25 * 1024 * 1024

#: The whole download — every hop, headers and body — must finish inside this.
TOTAL_TIMEOUT_SECONDS = 60.0

CONNECT_TIMEOUT_SECONDS = 10.0

MAX_REDIRECTS = 5

_REDIRECT_CODES = {301, 302, 303, 307, 308}


class FetchRefused(ValueError):
    """The download was not made or not finished. The message is safe to echo."""


class FetchBlocked(FetchRefused):
    """The egress guard, or an egress proxy, refused the address. A refusal, not a failure:
    asking again gets the same answer."""


class FetchTooLarge(FetchRefused):
    """The body is larger than the cap."""


#: The header Smokescreen names its refusal in (I1, D152).
PROXY_ERROR_HEADER = "X-Smokescreen-Error"


def _megabytes(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB"


async def fetch_bytes(url: str) -> bytes:
    """GET `url` and return its body, or raise `FetchRefused`."""
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            return await _fetch(url)
    except TimeoutError:
        raise FetchRefused(
            f"the download did not finish within {TOTAL_TIMEOUT_SECONDS:g} seconds"
        ) from None


async def _fetch(url: str) -> bytes:
    timeout = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                await egress_guard.check_url(url)
            except egress_guard.BlockedEgress as exc:
                raise FetchBlocked(str(exc)) from None

            try:
                async with client.stream("GET", url) as response:
                    location = response.headers.get("location")
                    if response.status_code in _REDIRECT_CODES and location:
                        url = urljoin(url, location)
                        continue
                    if response.status_code >= 400:
                        raise FetchRefused(
                            f"the server answered {response.status_code} for {url}"
                        )
                    return await _read_capped(response)
            except httpx.HTTPError as exc:
                raise FetchRefused(f"could not download {url}: {type(exc).__name__}") from None

    raise FetchRefused(f"more than {MAX_REDIRECTS} redirects; giving up")


async def _read_capped(response: httpx.Response, cap: int | None = None) -> bytes:
    cap = MAX_BYTES if cap is None else cap  # read at call time: the module's value is the one in force
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise FetchTooLarge(f"the file is larger than the {_megabytes(cap)} limit")

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > cap:
            raise FetchTooLarge(f"the file is larger than the {_megabytes(cap)} limit")
    return bytes(body)


# ── for a caller that reads the answer itself ──────────────────────────────────

@dataclass(frozen=True)
class Fetched:
    """One answered GET. `url` is where the answer came from (after redirects); `body` is read only
    for a 2xx — a 304, an error status or a redirect without a `Location` comes back empty."""

    url: str
    status: int
    headers: httpx.Headers
    body: bytes = b""
    redirects: tuple[str, ...] = field(default=())  # every URL before `url`, in order


def proxy_refusal(exc: BaseException) -> str | None:
    """What an egress proxy said when it refused a tunnel (`httpx.ProxyError("407 …")`), else None."""
    if isinstance(exc, httpx.ProxyError):
        text = str(exc).strip()
        if text.startswith("407"):
            return f"the egress proxy refused the address ({text})"
    return None


async def fetch(url: str, *, client: httpx.AsyncClient, headers: Mapping[str, str] | None = None,
                max_bytes: int | None = None, timeout_s: float = TOTAL_TIMEOUT_SECONDS) -> Fetched:
    """GET `url` through `client` with the egress policy on every hop, redirects followed here.

    Raises `FetchBlocked` (the guard or a proxy refused an address), `FetchTooLarge` (a 2xx body over
    `max_bytes`), or `FetchRefused` (no answer: a network error, the deadline, too many redirects).
    Any status is an answer: the caller decides what a 304, a 404 or a 503 means.
    """
    try:
        async with asyncio.timeout(timeout_s):
            return await _fetch_answer(url, client=client, headers=dict(headers or {}), max_bytes=max_bytes)
    except TimeoutError:
        raise FetchRefused(f"{url} did not answer within {timeout_s:g} seconds") from None


async def _fetch_answer(url: str, *, client: httpx.AsyncClient, headers: dict[str, str],
                        max_bytes: int | None) -> Fetched:
    seen: list[str] = []
    for _hop in range(MAX_REDIRECTS + 1):
        try:
            await egress_guard.check_url(url)
        except egress_guard.BlockedEgress as exc:
            raise FetchBlocked(str(exc)) from None
        try:
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                status = response.status_code
                location = response.headers.get("location")
                if status in _REDIRECT_CODES and location:
                    seen.append(url)
                    url = urljoin(url, location)
                    continue
                if status == 407 and response.headers.get(PROXY_ERROR_HEADER):
                    raise FetchBlocked(f"the egress proxy refused {url}: "
                                       f"{response.headers[PROXY_ERROR_HEADER][:300]}")
                body = await _read_capped(response, max_bytes) if 200 <= status < 300 else b""
                return Fetched(url=url, status=status, headers=response.headers, body=body, redirects=tuple(seen))
        except httpx.HTTPError as exc:
            refusal = proxy_refusal(exc)
            if refusal is not None:
                raise FetchBlocked(f"{refusal}: {url}") from None
            raise FetchRefused(f"could not fetch {url}: {type(exc).__name__}") from None
    raise FetchRefused(f"more than {MAX_REDIRECTS} redirects from {seen[0] if seen else url}; giving up")
