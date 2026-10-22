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
"""

from __future__ import annotations

import asyncio
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
                raise FetchRefused(str(exc)) from None

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


async def _read_capped(response: httpx.Response) -> bytes:
    cap = MAX_BYTES
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise FetchRefused(f"the file is larger than the {_megabytes(cap)} limit")

    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > cap:
            raise FetchRefused(f"the file is larger than the {_megabytes(cap)} limit")
    return bytes(body)
