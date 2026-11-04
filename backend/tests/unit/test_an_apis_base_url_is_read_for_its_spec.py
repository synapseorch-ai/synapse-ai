"""An API the person names by its base URL is read for its spec before anything else (F1317, CAP3's prod-13; 79 11-03).

prod-13's person gave two APIs by their base URLs only (the shop, the billing), both serving /openapi.json. The named
site's read found no spec (a base links none, and answers 404 or JSON at its root), so the planner scraped them and
planned five agents with no tools. Now, for a site the person named whose root is not a web page, the read tries the
usual spec addresses on its origin (/openapi.json, /swagger.json, /v3/api-docs), at most those three, a GET each
through the same guarded fetch and on the plan's web budget, and stops at the first that is a spec. A host the guard
or the host's rule refuses is tried no further: not one more request. A spec found is the person's, read once (F1298's
carry), and the plan is handed its tools; none found, the planner is told to ask for its spec or docs.
"""
from __future__ import annotations

import json

import httpx
import pytest

from core import builder_research as br, safe_fetch
from core.native_builder import named_sites, own_api
from core.native_builder.named_sites import read

BASE = "http://shop.example:9422"
SPEC = {"openapi": "3.0.0", "info": {"title": "Tidewell Goods", "version": "1"},
        "servers": [{"url": BASE}],
        "paths": {"/orders": {"get": {"operationId": "listOrders", "summary": "New orders"}}}}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("EGRESS_ALLOW_HOSTS", raising=False)
    br._LOOSE.clear()
    yield
    br._LOOSE.clear()


def _answers(monkeypatch, routes: dict[str, tuple[int, str, str]]) -> list[str]:
    """`safe_fetch.fetch` answering each URL from `routes` ({url: (status, content type, body)}), else 404."""
    calls: list[str] = []

    async def fetch(url, **_):
        calls.append(url)
        status, kind, body = routes.get(url, (404, "text/plain", ""))
        return safe_fetch.Fetched(url=url, status=status, headers=httpx.Headers({"content-type": kind}),
                                  body=body.encode() if 200 <= status < 300 else b"")

    monkeypatch.setattr(safe_fetch, "fetch", fetch)
    return calls


SPEC_JSON = (200, "application/json", json.dumps(SPEC))


class TestTheSpecIsLookedFor:
    async def test_a_base_that_answers_404_is_read_for_its_spec(self, monkeypatch):
        calls = _answers(monkeypatch, {f"{BASE}/openapi.json": SPEC_JSON})
        page = await read(BASE)
        assert page.api_spec == f"{BASE}/openapi.json", page
        assert calls == [BASE, f"{BASE}/openapi.json"], "the first spec found ends the looking"

    async def test_a_json_root_is_read_for_its_spec_in_order(self, monkeypatch):
        calls = _answers(monkeypatch, {BASE: (200, "application/json", '{"ok": true}'),
                                       f"{BASE}/swagger.json": SPEC_JSON})
        page = await read(BASE)
        assert page.api_spec == f"{BASE}/swagger.json"
        assert calls == [BASE, f"{BASE}/openapi.json", f"{BASE}/swagger.json"]

    async def test_at_most_three_and_none_found_is_said(self, monkeypatch):
        calls = _answers(monkeypatch, {})
        page = await read(BASE)
        assert page.api_spec is None
        assert calls == [BASE, f"{BASE}/openapi.json", f"{BASE}/swagger.json", f"{BASE}/v3/api-docs"]
        assert "ask the person for its spec" in page.why_not, page.why_not

    async def test_what_is_not_a_spec_is_not_one(self, monkeypatch):
        _answers(monkeypatch, {f"{BASE}/openapi.json": (200, "text/html", "<html><title>Not here</title></html>"),
                               f"{BASE}/swagger.json": (200, "application/json", '{"name": "not a spec"}')})
        assert (await read(BASE)).api_spec is None


class TestNotLookedFor:
    async def test_a_web_page_is_not_probed(self, monkeypatch):
        calls = _answers(monkeypatch, {BASE: (200, "text/html", "<html><title>Shop</title><h1>Shop</h1></html>")})
        await read(BASE)
        assert calls == [BASE]

    async def test_a_refused_host_is_sent_nothing_more(self, monkeypatch):
        calls: list[str] = []

        async def blocked(url, **_):
            calls.append(url)
            raise safe_fetch.FetchBlocked("blocked: an address inside this network")

        monkeypatch.setattr(safe_fetch, "fetch", blocked)
        page = await read(BASE)
        assert calls == [BASE], "not one request past the guard's refusal"
        assert page.api_spec is None and page.why_not.startswith("not read:")

    async def test_a_host_the_hosts_rule_refuses_is_probed_nowhere(self, monkeypatch):
        from core import mcp_client

        calls = _answers(monkeypatch, {})

        async def refused(url):
            return "that address is inside this deployment"

        monkeypatch.setattr(mcp_client, "_url_rule", refused)
        await read(BASE)
        assert calls == [BASE], "zero probe requests to a host the rule refuses"

    async def test_with_the_web_budget_spent_none_is_made(self, monkeypatch):
        calls = _answers(monkeypatch, {f"{BASE}/openapi.json": SPEC_JSON})
        for _ in range(br.WEB_PER_PLAN - 1):
            await br.web_call()
        page = await read(BASE)
        assert calls == [BASE] and page.api_spec is None


class TestTheSpecFoundIsThePersons:
    async def test_the_gather_reads_it_once_and_hands_the_plan_its_tools(self, monkeypatch):
        from core.native_builder.build_plan import _named_sites

        _answers(monkeypatch, {f"{BASE}/openapi.json": SPEC_JSON})

        async def spec_bytes(url, **_):
            assert url == f"{BASE}/openapi.json"
            return json.dumps(SPEC).encode()

        monkeypatch.setattr(safe_fetch, "fetch_bytes", spec_bytes)
        state = {"user_message": f"Take the new orders from our shop at {BASE} and invoice each one."}
        sites = await _named_sites(state)
        kept = state.get(own_api.SPECS_KEY) or {}
        assert [v["spec"] for v in kept.values()] == [f"{BASE}/openapi.json"], kept
        [site] = sites
        assert [t["tool"] for t in site["api"]["tools"]] == ["tidewell_goods_listorders"], site
        assert named_sites.PAGES_KEY in state
