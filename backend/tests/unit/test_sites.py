"""A host's site, one rule for everything that groups hosts by owner (BRW1, D328, F1138): the browser's gate, the
sessions it keeps (one per site; a cookie only ever goes to its own) and the builder's research."""
from __future__ import annotations

import pytest

from core.sites import registrable_domain, same_site


@pytest.mark.parametrize("host, site", [
    ("learn.microsoft.com", "microsoft.com"), ("www.example.com", "example.com"), ("example.com.", "example.com"),
    ("dev.tesco.co.uk", "tesco.co.uk"), ("shop.naver.co.kr", "naver.co.kr"), ("x.gov.uk", "x.gov.uk"),
    ("acme.workers.dev", "acme.workers.dev"), ("abc.execute-api.us-east-1.amazonaws.com",
                                               "abc.execute-api.us-east-1.amazonaws.com"),
    ("localhost", "localhost"), ("PORTAL.Example.COM", "example.com"),
])
def test_a_hosts_site_is_its_registrable_domain(host, site):
    assert registrable_domain(host) == site


def test_two_tenants_of_one_host_are_two_sites():
    assert same_site("api.shop.co.uk", "www.shop.co.uk")
    assert not same_site("a.co.uk", "b.co.uk")
    assert not same_site("acme.workers.dev", "evil.workers.dev"), "each tenant is its own owner"
    assert not same_site("", "example.com")


def test_the_gate_and_the_builders_research_use_the_same_rule():
    from core import builder_research
    from core.browser import gate
    for a, b in (("api.shop.co.uk", "www.shop.co.uk"), ("acme.workers.dev", "evil.workers.dev"),
                 ("shop.naver.co.kr", "pay.naver.co.kr")):
        assert gate.same_site(a, b) == same_site(a, b)
        assert (builder_research._registrable_domain(a) == builder_research._registrable_domain(b)) == same_site(a, b)
