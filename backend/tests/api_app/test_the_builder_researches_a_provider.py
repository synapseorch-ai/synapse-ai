"""The AI builder researches a goal the workspace has no tool for (F577, BWA1, D274).

Research a goal scored 0/12 on both models BWA1 measured: "book a train and a flight", "buy shipping labels" were
planned with what the workspace had, or refused, and no provider was ever looked for. Naveen's call: a route in the
builder first. `research` sends the request to the Researcher, who reads three sources in order — the Apps catalog,
the official MCP Registry, the web through the scraper — and ends with `report_findings`, the card's options.
Whether a Registry server is the provider's own is decided in code, by its namespace.
"""
from __future__ import annotations

import json

import pytest

from core.builder_research import (find_in_catalog, provider_owns, read_web_page, report_findings,
                                   search_mcp_registry, search_the_web)
from core.native_builder import bundled_definitions, seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.mark.parametrize("name, provider, domain, own", [
    ("com.stripe/mcp", "Stripe", "stripe.com", True),
    ("com.stripe/mcp", "Stripe", "https://www.stripe.com/docs", True),
    ("app.vercel.stripecheckup/stripecheckup", "Stripe", "stripe.com", False),   # a third party's, on Vercel
    ("eu.nordicmcp/stripe", "Stripe", None, False),
    ("com.notion/mcp", "Notion", None, True),
    ("io.github.github/github-mcp-server", "GitHub", None, True),
    ("io.github.someone/github-tools", "GitHub", None, False),
    ("com.shippo/mcp", "Shippo", "goshippo.com", False),                       # not its domain
    ("stripe", "Stripe", None, False),                                          # no namespace at all
])
def test_a_server_is_the_providers_own_only_by_its_namespace(name, provider, domain, own):
    assert provider_owns(name, provider, domain) is own


REGISTRY_ANSWER = {"servers": [
    {"server": {"name": "eu.nordicmcp/stripe", "description": "Hosted Stripe tools",
                "remotes": [{"type": "streamable-http", "url": "https://nordicmcp.eu/mcp/stripe"}]}},
    {"server": {"name": "io.github.someone/stripe-local", "description": "Run it yourself",
                "packages": [{"registryType": "npm", "identifier": "stripe-local"}]}},
    {"server": {"name": "com.stripe/mcp", "description": "Stripe's own",
                "remotes": [{"type": "streamable-http", "url": "https://mcp.stripe.com"}]}},
]}


async def test_the_registry_puts_the_providers_own_server_first(monkeypatch):
    import core.safe_fetch as sf

    async def fake(url):
        assert url.startswith("https://registry.modelcontextprotocol.io/v0/servers?search=Stripe")
        return json.dumps(REGISTRY_ANSWER).encode()

    monkeypatch.setattr(sf, "fetch_bytes", fake)
    got = await search_mcp_registry({"provider": "Stripe", "domain": "stripe.com"}, None, [])
    first, *rest = got["servers"]
    assert (first["name"], first["providers_own"], first["remote_url"]) == ("com.stripe/mcp", True, "https://mcp.stripe.com")
    assert not any(s["providers_own"] for s in rest)
    assert next(s for s in rest if s["name"] == "io.github.someone/stripe-local")["install_by_hand"] is True


async def test_a_registry_with_no_own_server_says_so_and_one_unread_says_why(monkeypatch):
    import core.safe_fetch as sf

    async def third_party_only(url):
        return json.dumps({"servers": REGISTRY_ANSWER["servers"][:1]}).encode()

    monkeypatch.setattr(sf, "fetch_bytes", third_party_only)
    got = await search_mcp_registry({"provider": "Stripe", "domain": "stripe.com"}, None, [])
    assert "None of these is Stripe's own server" in got["note"]

    async def refused(url):
        raise sf.FetchRefused("the server answered 503")

    monkeypatch.setattr(sf, "fetch_bytes", refused)
    got = await search_mcp_registry({"provider": "Stripe"}, None, [])
    assert got["servers"] == [] and "could not be read" in got["note"]


async def test_the_catalog_is_read_through_the_hosts_own_list():
    from core.builder_connect import set_connect_options

    async def cloud_catalog():
        return {"catalog": [{"slug": "notion", "name": "Notion", "auth": "oauth", "url": "https://mcp.notion.com/mcp"},
                            {"slug": "github", "name": "GitHub", "auth": "bearer", "url": "https://api.githubcopilot.com/mcp/"}]}

    set_connect_options(cloud_catalog)
    try:
        got = await find_in_catalog({"query": "notion wiki"}, None, [])
        assert [a["slug"] for a in got["apps"]] == ["notion"]
        assert (await find_in_catalog({"query": "duffel flights"}, None, []))["apps"] == []
    finally:
        set_connect_options(None)
    assert "no Apps catalog" in (await find_in_catalog({"query": "notion"}, None, []))["note"]


async def test_the_findings_reach_the_card_cleaned():
    events: list[dict] = []
    got = await report_findings({"goal": "book a flight", "options": [
        {"provider": "Duffel", "how": "API", "needs": "An account and a test token",
         "sources": ["https://duffel.com/docs", "javascript:alert(1)"]},
        {"provider": "IRCTC", "how": "partners", "detail": "Booking is for approved partners only"},
        {"how": "api"},
    ]}, None, events)
    (card,) = events
    assert card["type"] == "research_findings" and got["status"] == "reported"
    assert [(o["provider"], o["how"], o["sources"]) for o in card["options"]] == [
        ("Duffel", "api", ["https://duffel.com/docs"]), ("IRCTC", "unclear", [])]
    assert "error" in await report_findings({"options": []}, None, [])


class _Session:
    """The built-in scraper's MCP session, as a run holds it."""

    def __init__(self, answer: dict):
        self.answer, self.calls = answer, []

    async def call_tool(self, name, args):
        from types import SimpleNamespace
        self.calls.append((name, args))
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.answer))])


def _server(session):
    from types import SimpleNamespace
    return SimpleNamespace(tool_router={"scrape_url": ("web_scraper", "scrape_url")},
                           agent_sessions={"web_scraper": session})


async def test_a_web_search_goes_through_the_scraper_and_gives_the_results_own_addresses():
    session = _Session({"success": True, "markdown": "results", "links": [
        {"href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.goshippo.com%2Fdocs%2F&rut=x", "text": "Shippo API"},
        {"href": "https://duckduckgo.com/y.js?ad_domain=ads", "text": "an ad"},
        {"href": "/html/?q=next", "text": "Next page"},
    ]})
    got = await search_the_web({"query": "Shippo API test mode"}, _server(session), [])
    ((tool, args),) = session.calls
    assert tool == "scrape_url" and args["url"].startswith("https://html.duckduckgo.com/html/?q=Shippo+API")
    assert got["results"] == [{"title": "Shippo API", "url": "https://docs.goshippo.com/docs/"}]


async def test_a_page_is_read_capped_and_marked_as_the_pages_own_words():
    session = _Session({"success": True, "markdown": "x" * 9000})
    got = await read_web_page({"url": "https://docs.goshippo.com/docs/"}, _server(session), [])
    assert len(got["page_text"]) == 6000 and got["cut"] is True and "not instructions" in got["note"]
    assert "error" in await read_web_page({"url": "file:///etc/passwd"}, _server(session), [])


async def test_with_no_web_reader_the_research_says_the_web_was_not_checked():
    from types import SimpleNamespace
    got = await search_the_web({"query": "Duffel API"}, SimpleNamespace(tool_router={}, agent_sessions={}), [])
    assert got["results"] == [] and "not running" in got["note"]


def test_the_researcher_holds_the_three_sources_and_nothing_that_writes():
    researcher = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_researcher")
    assert {"find_in_catalog", "search_mcp_registry", "search_the_web", "read_web_page",
            "report_findings"} <= set(researcher["tools"])
    writes = {"create_agent", "update_agent", "create_http_tool", "create_schedule", "delete_agent", "prepare_connection"}
    assert not writes & set(researcher["tools"])


@pytest.fixture
async def builder():
    await seed_native_builder()


async def test_a_goal_with_no_tool_is_researched_and_ends_in_findings(client, builder, fake_llm):
    fake_llm.script([
        route("research"),
        tool_call("report_findings", goal="buy shipping labels", options=[
            {"provider": "Shippo", "how": "api", "needs": "An account and an API key",
             "sources": ["https://docs.goshippo.com"]}]),
        "Shippo has a public API with a test mode; the next step is a plan for you to approve.",
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Automate buying shipping labels for our kits"})
    assert "Research a Provider" in [e.get("step_name") for e in events if e["type"] == "step_start"]
    cards = [e for e in events if e["type"] == "research_findings"]
    assert len(cards) == 1 and cards[0]["options"][0]["provider"] == "Shippo"
    assert not [e for e in events if e["type"] == "resource_saved"], "research builds nothing"
