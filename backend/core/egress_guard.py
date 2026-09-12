"""Egress policy for tools that fetch user- or model-supplied URLs.

A tool's URLs are data, not configuration: they arrive from a prompt, and a
prompt can be steered by whatever the last scraped page said. A fetcher that
goes wherever it is pointed will sooner or later be pointed at the network
the process itself runs on — the cloud metadata endpoints (169.254.169.254,
169.254.170.2), loopback services on its own host, or anything else the
subnet can reach. On a cloud runtime the metadata endpoint alone turns one
injected URL into live credentials.

The policy: http(s) only, and only to addresses that are actually on the
public internet. Every address a hostname resolves to is checked, not just
the first — a name with one public and one private record must not pass.

`EGRESS_ALLOW_HOSTS` (comma-separated hostnames) exempts specific hosts from
the address checks, for installs that genuinely scrape an intranet or a
localhost dev server, and for tests dialing loopback stand-ins. Empty —
deny — is the default.

The DNS-rebinding window (resolution here vs. resolution at connect time) is
deliberately not closed: closing it means owning socket creation and TLS SNI
handling in every fetch path. Network-level egress control is the layer for
that; this module is the in-process guard in front of it.

Deliberately stdlib-only, and deliberately not a Settings consumer: it is
imported by tool subprocesses that must construct in any environment.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

#: Carrier-grade NAT. Not covered by `is_private`, and it is where several
#: managed-Kubernetes overlays put internal services.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

#: One resolution per host per window is enough for a page's subresources;
#: short enough that the cache is not itself a rebinding aid.
_RESOLVE_TTL_SECONDS = 60.0
_RESOLVE_CACHE_MAX = 512

_resolve_cache: dict[str, tuple[float, list[str]]] = {}


class BlockedEgress(ValueError):
    """A URL that must not be fetched. The message is safe to echo to the model."""


class MalformedUrl(BlockedEgress):
    """Not a URL with a scheme, so it names no destination to check.

    A subclass for the same reason `UnresolvedHost` is: the scraper should
    refuse what it cannot evaluate. A custom HTTP tool should not, because the
    commonest way to get here is an unrendered `{{global.api_base}}` — a missing
    variable, whose own error message names the variable and is worth more than
    this one. Nothing is conceded: a string with no scheme is one httpx refuses
    to send anywhere. A URL with a scheme that is simply not http stays a plain
    `BlockedEgress`.
    """


class UnresolvedHost(BlockedEgress):
    """The host could not be resolved, so nothing is known about where it points.

    A subclass, so every existing caller keeps failing closed: for the scraper,
    which is handed URLs a model invented, "unknown" is the right thing to
    refuse. A *saved* custom HTTP tool is the other case — the host may be an
    internal API, a service not yet deployed, or a DNS blip, and refusing it is
    a guess about somebody else's network. `core/builder_tools.py` already says
    so in as many words about the same URLs at creation time.

    Letting it through concedes nothing: httpx resolves through the same stack
    a moment later and fails the same lookup. What is not conceded is an
    address — an IP literal always "resolves", so `169.254.169.254` is still
    refused by the check below.
    """


def allowed_hosts() -> frozenset[str]:
    raw = os.environ.get("EGRESS_ALLOW_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def reject_address(address: str) -> str | None:
    """Return a reason to refuse this IP, or None to allow it.

    Grouped by what each range would reach, because the point is not that the
    addresses are unusual but that they are *ours*.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return f"{address} is not an IP address"

    # An IPv4-mapped IPv6 address (`::ffff:169.254.170.2`) reaches the IPv4
    # host while failing every IPv4 range check, so unwrap before testing.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        # 169.254.0.0/16 and fe80::/10 — the cloud metadata range.
        return "link-local, including the cloud metadata endpoint"
    # Before `is_private`, which also covers 0.0.0.0/8 and would report the
    # unspecified address as "private" — true, and unhelpful to whoever typed it.
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_private:
        return "private network"
    if ip.is_multicast or ip.is_reserved:
        return "multicast or reserved"
    if ip.version == 4 and ip in _CGNAT:
        return "shared address space"
    return None


async def resolve(host: str) -> list[str]:
    """All addresses `host` resolves to, via the event loop's resolver, cached briefly."""
    now = time.monotonic()
    hit = _resolve_cache.get(host)
    if hit is not None and now - hit[0] < _RESOLVE_TTL_SECONDS:
        return hit[1]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnresolvedHost(f"could not resolve {host}") from exc
    # sockaddr[0] is typed `str | int` because the tuple shape differs by
    # address family; for AF_INET and AF_INET6 it is always the address.
    addresses = sorted({str(info[4][0]) for info in infos})

    if len(_resolve_cache) >= _RESOLVE_CACHE_MAX:
        _resolve_cache.clear()
    _resolve_cache[host] = (now, addresses)
    return addresses


async def check_host(host: str) -> None:
    """Raise `BlockedEgress` unless every address behind `host` is public."""
    host = host.strip().lower().strip("[]")
    if not host:
        raise BlockedEgress("that URL has no host")
    if host in allowed_hosts():
        return

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        addresses = await resolve(host)

    for address in addresses:
        reason = reject_address(address)
        if reason is not None:
            # `host` is echoed because the caller supplied it. The addresses
            # it resolved to are not: that is internal topology, and reading
            # it back is exactly what an attacker is probing for.
            raise BlockedEgress(f"{host} resolves to a blocked range: {reason}")


async def check_url(url: str) -> None:
    """Check one URL, or raise `BlockedEgress`."""
    parsed = urlparse(url)
    if not parsed.scheme:
        raise MalformedUrl("that is not a URL; it has no http:// or https:// scheme")
    if parsed.scheme not in {"http", "https"}:
        raise BlockedEgress(
            f"{parsed.scheme} URLs are not supported; use http or https"
        )
    await check_host(parsed.hostname or "")
