"""Synapse AI reaches public sites behind DNS64/NAT64, and nothing private through it (F1238).

On a DNS64 network a public host with no IPv6 of its own resolves to a NAT64 address: its IPv4 inside an IPv6
prefix, `64:ff9b::5db8:d822` for 93.184.216.34. Python calls the well-known prefix `is_reserved`, so the guard refused
every public site as "multicast or reserved": the browser, the scraper, HTTP tools and the MCP connect URL, on any
install behind DNS64 (this laptop's WSL DNS does it now).

A NAT64 address reaches its embedded IPv4, so that IPv4 is what the guard judges, by the same rules: a private,
loopback or metadata IPv4 inside is refused as itself (RFC 6052 forbids those in the well-known prefix; a gateway
that forwards one anyway is not trusted to). The well-known `64:ff9b::/96` is always read. The local-use
`64:ff9b:1::/48` (RFC 8215) has a prefix length the operator picks, which moves the IPv4 (RFC 6052 §2.2), so it is
read only under a prefix the install declares in `EGRESS_NAT64_PREFIXES`; undeclared, it stays refused and says so
(Naveen, 10-30). The egress proxy still dials the IPv6 address it checked: behind NAT64 that is the way out.
"""
from __future__ import annotations

import asyncio

import pytest

from core import egress_guard, egress_proxy
from core.egress_guard import reject_address


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    monkeypatch.delenv("EGRESS_NAT64_PREFIXES", raising=False)
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


def _wkp(v4: str) -> str:
    """`v4` in the well-known prefix, as DNS64 writes it."""
    import ipaddress
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Address("64:ff9b::")) | int(ipaddress.IPv4Address(v4))))


class TestTheWellKnownPrefix:
    @pytest.mark.parametrize("v4", ["93.184.216.34", "8.8.8.8", "1.1.1.1"])
    def test_a_public_ipv4_inside_is_allowed(self, v4):
        assert reject_address(_wkp(v4)) is None

    @pytest.mark.parametrize("v4, said", [
        ("169.254.169.254", "metadata"), ("169.254.170.2", "metadata"), ("127.0.0.1", "loopback"),
        ("10.0.0.1", "private"), ("192.168.1.1", "private"), ("172.16.0.1", "private"), ("100.64.0.1", "shared"),
        ("0.0.0.0", "unspecified"), ("224.0.0.1", "multicast"),
    ])
    def test_a_private_ipv4_inside_is_refused_as_itself(self, v4, said):
        assert said in (reject_address(_wkp(v4)) or "")

    def test_the_mapped_form_is_unchanged(self):
        assert "metadata" in reject_address("::ffff:169.254.170.2")
        assert reject_address("::ffff:93.184.216.34") is None


class TestTheLocalUsePrefix:
    def test_undeclared_it_is_refused_naming_the_setting(self):
        said = reject_address("64:ff9b:1::5db8:d822") or ""
        assert "EGRESS_NAT64_PREFIXES" in said and "NAT64" in said

    def test_a_declared_96_reads_the_low_32_bits(self, monkeypatch):
        monkeypatch.setenv("EGRESS_NAT64_PREFIXES", "64:ff9b:1:abcd::/96")
        assert reject_address("64:ff9b:1:abcd::5db8:d822") is None
        assert "metadata" in reject_address("64:ff9b:1:abcd::a9fe:a9fe")

    def test_a_declared_64_reads_past_the_u_octet(self, monkeypatch):
        # RFC 6052's /64 form: the IPv4 in bits 72-103, after the zero u-octet (bits 64-71).
        monkeypatch.setenv("EGRESS_NAT64_PREFIXES", "64:ff9b:1:abcd::/64")
        assert reject_address("64:ff9b:1:abcd:5d:b8d8:2200:0") is None          # 93.184.216.34
        assert "private" in reject_address("64:ff9b:1:abcd:0a:0:100:0")         # 10.0.0.1
        assert "not a NAT64" in reject_address("64:ff9b:1:abcd:ff5d:b8d8:2200:0"), "a u-octet that isn't zero"

    def test_a_declared_48_reads_around_the_u_octet(self, monkeypatch):
        monkeypatch.setenv("EGRESS_NAT64_PREFIXES", "64:ff9b:1::/48")
        assert reject_address("64:ff9b:1:5db8:d8:2200::") is None                # 93.184.216.34
        assert "loopback" in reject_address("64:ff9b:1:7f00:0:100::")           # 127.0.0.1

    def test_a_prefix_of_a_length_rfc_6052_has_not_is_ignored(self, monkeypatch):
        monkeypatch.setenv("EGRESS_NAT64_PREFIXES", "64:ff9b:1:abcd::/80, not-a-prefix")
        assert "EGRESS_NAT64_PREFIXES" in (reject_address("64:ff9b:1:abcd::5db8:d822") or "")


def _resolve_to(monkeypatch, table: dict[str, list[str]]):
    async def fake_resolve(host):
        return table[host]

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)


async def test_a_name_behind_dns64_passes_every_door(monkeypatch):
    _resolve_to(monkeypatch, {"example.com": [_wkp("93.184.216.34")], "inside.example": [_wkp("10.0.0.7")]})
    await egress_guard.check_host("example.com")
    await egress_guard.check_url("https://example.com/page")
    from core.builder_tools import _refuse_inward_url
    assert await _refuse_inward_url("https://example.com/v1/send") is None
    with pytest.raises(egress_guard.BlockedAddress):
        await egress_guard.check_host("inside.example")
    assert "private" in (await _refuse_inward_url("https://inside.example/x"))["error"]


async def test_the_proxy_dials_the_ipv6_address_it_checked(monkeypatch):
    address = _wkp("93.184.216.34")
    _resolve_to(monkeypatch, {"example.com": [address]})
    dialled: list[list[str]] = []

    async def dial(addresses, port):
        dialled.append(list(addresses))
        raise OSError("not dialling in a test")

    monkeypatch.setattr(egress_proxy, "_dial", dial)
    proxy = egress_proxy.EgressProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection(proxy.host, proxy.port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        writer.close()
    finally:
        await proxy.close()
    assert dialled == [[address]], "the IPv6 address, never the name or the embedded IPv4"
    assert b" 502 " in head


def test_the_setting_reaches_the_shared_tool_servers():
    from core.scale.worker_server_module import SHARED_TOOL_ENV_NAMES

    assert "EGRESS_NAT64_PREFIXES" in SHARED_TOOL_ENV_NAMES


def test_the_browser_component_is_given_the_setting(monkeypatch):
    from core.browser import client

    monkeypatch.setenv("EGRESS_NAT64_PREFIXES", "64:ff9b:1:abcd::/96")
    host = client._Host()  # nothing starts until first use
    assert host._env().get("EGRESS_NAT64_PREFIXES") == "64:ff9b:1:abcd::/96"
