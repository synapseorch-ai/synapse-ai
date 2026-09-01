"""The web scraper must not reach private networks.

`web_scraper`'s seven tools all fetch model-supplied URLs through Chromium
from inside the worker task. On cloud runtimes the task metadata endpoint
(169.254.170.2 / 169.254.169.254) turns one prompt-injected URL into live
credentials, and everything else on the subnet — loopback services, RFC1918
neighbours — relies on being unreachable from outside.

Two layers are under test, because a URL-string check alone is bypassable
from inside the browser (redirect hops, `js_code` navigation, in-page
fetches):

  * `core.egress_guard` — the address policy and the pre-flight `check_url`
  * `tools.web_scraper._guard_route` — the Playwright route interceptor that
    aborts blocked requests at the browser's own network layer

Plus the success-derivation fix that rode along: `crawl_multiple` and
`scrape_with_session` used to report top-level ``success: true`` even when
every URL or step had failed.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import egress_guard
from core.egress_guard import BlockedEgress, check_host, check_url, reject_address
from tools import web_scraper


@pytest.fixture(autouse=True)
def _clean_guard_state(monkeypatch):
    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    egress_guard._resolve_cache.clear()
    yield
    egress_guard._resolve_cache.clear()


# ---------------------------------------------------------------------------
# Address policy
# ---------------------------------------------------------------------------

BLOCKED_ADDRESSES = [
    "169.254.170.2",        # ECS task credentials endpoint
    "169.254.169.254",      # EC2/IMDS metadata endpoint
    "10.0.0.1",             # RFC1918
    "172.16.0.1",           # RFC1918
    "192.168.1.1",          # RFC1918
    "127.0.0.1",            # loopback
    "0.0.0.0",              # unspecified
    "100.64.0.1",           # carrier-grade NAT / k8s overlays
    "224.0.0.1",            # multicast
    "::1",                  # IPv6 loopback
    "::ffff:169.254.170.2", # IPv4-mapped — must unwrap before range checks
    "fe80::1",              # IPv6 link-local
    "fc00::1",              # IPv6 ULA
    "fd00:ec2::254",        # EC2 IPv6 metadata endpoint (inside fc00::/7)
]

ALLOWED_ADDRESSES = [
    "93.184.216.34",         # example.com
    "8.8.8.8",
    "2606:2800:220:1:248:1893:25c8:1946",
]


@pytest.mark.parametrize("address", BLOCKED_ADDRESSES)
def test_private_and_special_addresses_are_rejected(address):
    assert reject_address(address) is not None


@pytest.mark.parametrize("address", ALLOWED_ADDRESSES)
def test_public_addresses_are_allowed(address):
    assert reject_address(address) is None


def test_non_ip_input_is_rejected():
    assert reject_address("not-an-ip") is not None


# ---------------------------------------------------------------------------
# check_url / check_host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "raw://<html></html>",
    "about:blank",
    "chrome://settings",
    "javascript:alert(1)",
    "",
])
async def test_non_http_schemes_are_rejected(url):
    with pytest.raises(BlockedEgress):
        await check_url(url)


async def test_url_without_host_is_rejected():
    with pytest.raises(BlockedEgress):
        await check_url("http:///path-only")


@pytest.mark.parametrize("url", [
    "http://169.254.170.2/v2/credentials",
    "http://[::ffff:169.254.170.2]/",
    "https://10.0.0.8:8080/admin",
    "http://[::1]:6379/",
])
async def test_literal_private_urls_are_rejected(url):
    with pytest.raises(BlockedEgress) as exc:
        await check_url(url)
    # The reason is echoed; the resolved topology is not.
    assert "blocked range" in str(exc.value)


async def test_hostname_with_one_private_record_is_rejected(monkeypatch):
    """A name with one public and one private A record must not pass."""
    async def fake_resolve(host):
        return ["93.184.216.34", "10.0.0.5"]

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)
    with pytest.raises(BlockedEgress):
        await check_host("evil.example.com")


async def test_hostname_with_only_public_records_passes(monkeypatch):
    async def fake_resolve(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)
    await check_host("example.com")  # must not raise


async def test_unresolvable_hostname_is_rejected(monkeypatch):
    async def fake_resolve(host):
        raise BlockedEgress(f"could not resolve {host}")

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)
    with pytest.raises(BlockedEgress):
        await check_host("no-such-host.invalid")


async def test_allowlisted_host_is_exempt(monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "localhost, intranet.corp")
    # Exempt hosts skip resolution entirely — no DNS in this test.
    await check_host("localhost")
    await check_host("INTRANET.CORP")
    with pytest.raises(BlockedEgress):
        await check_host("127.0.0.1")  # a literal address is not the same exemption


# ---------------------------------------------------------------------------
# Browser-layer route guard
# ---------------------------------------------------------------------------

class FakeRoute:
    def __init__(self, url: str):
        self.request = SimpleNamespace(url=url)
        self.aborted_with: str | None = None
        self.continued = False

    async def abort(self, error_code: str = "failed"):
        self.aborted_with = error_code

    async def continue_(self):
        self.continued = True


@pytest.mark.parametrize("url", [
    "http://169.254.170.2/v2/credentials",
    "http://192.168.1.1/router",
    "http://127.0.0.1:8000/api/scale/tenants",
    "file:///etc/passwd",
])
async def test_guard_route_aborts_blocked_requests(url):
    route = FakeRoute(url)
    await web_scraper._guard_route(route)
    assert route.aborted_with == "blockedbyclient"
    assert not route.continued


async def test_guard_route_continues_public_requests():
    route = FakeRoute("https://93.184.216.34/page")
    await web_scraper._guard_route(route)
    assert route.continued
    assert route.aborted_with is None


async def test_guard_route_fails_closed_on_resolution_failure(monkeypatch):
    async def fake_resolve(host):
        raise OSError("resolver exploded")

    monkeypatch.setattr(egress_guard, "resolve", fake_resolve)
    route = FakeRoute("https://example.com/")
    await web_scraper._guard_route(route)
    assert route.aborted_with == "blockedbyclient"


class FakeContext:
    def __init__(self):
        self.routes: list = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


async def test_egress_guard_attaches_once_per_context():
    ctx = FakeContext()
    page = SimpleNamespace(context=ctx)
    await web_scraper._attach_egress_guard(page, context=ctx)
    await web_scraper._attach_egress_guard(page, context=ctx)  # session reuse
    assert len(ctx.routes) == 1
    assert ctx.routes[0][1] is web_scraper._guard_route


# ---------------------------------------------------------------------------
# Handler pre-flight and success derivation
# ---------------------------------------------------------------------------

def _payload(result) -> dict:
    return json.loads(result[0].text)


class FakeCrawler:
    """arun succeeds or fails per-URL according to `outcomes`."""

    def __init__(self, outcomes: dict[str, bool]):
        self.outcomes = outcomes
        self.seen: list[str] = []

    async def arun(self, url: str, config=None):
        self.seen.append(url)
        ok = self.outcomes.get(url, True)
        return SimpleNamespace(
            success=ok,
            markdown="# page" if ok else "",
            links={},
            media={},
            error_message=None if ok else "boom",
            extracted_content=None,
            screenshot=None,
        )


@pytest.fixture
def fake_crawler(monkeypatch):
    def install(outcomes: dict[str, bool]) -> FakeCrawler:
        crawler = FakeCrawler(outcomes)

        async def fake_get_crawler():
            return crawler

        monkeypatch.setattr(web_scraper, "get_crawler", fake_get_crawler)
        return crawler

    return install


async def test_scrape_url_preflight_blocks_metadata_endpoint(fake_crawler):
    crawler = fake_crawler({})
    payload = _payload(await web_scraper._handle_scrape_url(
        {"url": "http://169.254.170.2/v2/credentials"}
    ))
    assert payload["success"] is False
    assert "blocked" in payload["error"].lower()
    assert crawler.seen == []  # never reached the browser


async def test_crawl_multiple_all_fail_is_failure(fake_crawler):
    fake_crawler({})
    payload = _payload(await web_scraper._handle_crawl_multiple(
        {"urls": ["http://10.0.0.1/", "http://192.168.0.1/"]}
    ))
    assert payload["success"] is False
    assert payload["succeeded"] == 0
    assert payload["failed"] == 2
    assert all(not r["success"] for r in payload["results"])


async def test_crawl_multiple_partial_success_keeps_counts(fake_crawler):
    crawler = fake_crawler({"https://93.184.216.34/": True})
    payload = _payload(await web_scraper._handle_crawl_multiple(
        {"urls": ["https://93.184.216.34/", "http://169.254.170.2/"]}
    ))
    assert payload["success"] is True
    assert payload["succeeded"] == 1
    assert payload["failed"] == 1
    assert crawler.seen == ["https://93.184.216.34/"]  # blocked URL never fetched


async def test_crawl_multiple_upstream_failures_count(fake_crawler):
    fake_crawler({"https://93.184.216.34/": False, "https://8.8.8.8/": False})
    payload = _payload(await web_scraper._handle_crawl_multiple(
        {"urls": ["https://93.184.216.34/", "https://8.8.8.8/"]}
    ))
    assert payload["success"] is False
    assert payload["failed"] == 2


async def test_session_step_to_private_url_fails_that_step(fake_crawler):
    fake_crawler({"https://93.184.216.34/login": True})
    payload = _payload(await web_scraper._handle_scrape_with_session({
        "session_id": "s1",
        "steps": [
            {"url": "https://93.184.216.34/login"},
            {"url": "http://169.254.170.2/v2/credentials"},
        ],
    }))
    assert payload["success"] is False
    assert payload["results"][0]["success"] is True
    assert payload["results"][1]["success"] is False
    assert "blocked" in payload["results"][1]["error"].lower()


async def test_session_all_steps_succeed(fake_crawler):
    fake_crawler({})
    payload = _payload(await web_scraper._handle_scrape_with_session({
        "session_id": "s2",
        "steps": [
            {"url": "https://93.184.216.34/a"},
            {"url": "https://93.184.216.34/b"},
        ],
    }))
    assert payload["success"] is True
    assert payload["steps_completed"] == 2
