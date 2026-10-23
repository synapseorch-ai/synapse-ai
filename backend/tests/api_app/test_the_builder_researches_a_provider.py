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
    ("com.shippo/shippo-mcp", "Shippo", "goshippo.com", True),                 # named for it; its site is goshippo.com
    ("io.stripe/mcp", "Stripe", "stripe.com", False),                          # whoever holds stripe.io
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


async def test_a_verdict_stands_only_on_what_the_run_read():
    """BWA1's live run: gpt-4.1-mini reported "public API" for three providers from memory, citing pages it never
    read. A verdict needs its evidence from this run: `api`/`none` a page of the provider's that was read, `mcp` the
    provider's own server in the Registry. Anything else reaches the card as `unclear`, with no unread source."""
    from core.builder_tools import _current_run

    token = _current_run.set("run_research_1")
    try:
        events: list[dict] = []
        await read_web_page({"url": "https://duffel.com/docs/api"},
                            _server(_Session({"success": True, "markdown": "Duffel API: test mode"})), [])
        got = await report_findings({"goal": "book a flight", "options": [
            {"provider": "Duffel", "how": "API", "sources": ["https://duffel.com/docs/api", "javascript:alert(1)"]},
            {"provider": "IRCTC", "how": "none", "sources": ["https://www.irctc.co.in/partners"]},
            {"provider": "Amadeus", "how": "mcp"},
            {"provider": "USPS", "how": "none", "sources": ["https://duffel.com/docs/api"]},
            {"how": "api"},
        ]}, None, events)
    finally:
        _current_run.reset(token)
    (card,) = events
    assert card["type"] == "research_findings" and got["status"] == "reported"
    assert [(o["provider"], o["how"], o["sources"]) for o in card["options"]] == [
        ("Duffel", "api", ["https://duffel.com/docs/api"]),   # read this run
        ("IRCTC", "unclear", []),                              # cited, never read
        ("Amadeus", "unclear", []),                            # no own server seen
        ("USPS", "unclear", ["https://duffel.com/docs/api"])]  # read, but another company's page
    assert "error" in await report_findings({"options": []}, None, [])


@pytest.mark.parametrize("provider, page, how", [
    ("Microsoft Teams", "https://learn.microsoft.com/en-us/graph/api/channel-post-message", "api"),
    ("Google Calendar", "https://developers.google.com/calendar/api", "api"),
    ("Tesco Groceries", "https://dev.tesco.co.uk/docs", "api"),
    ("Microsoft Teams", "https://teamsbot.io/docs", "unclear"),            # someone else's site
    ("Common Appointment Systems", "https://learn.microsoft.com/graph", "unclear"),
])
async def test_a_products_page_on_its_companys_domain_is_its_own(provider, page, how):
    """F667 (Naveen's brand-word rule, 2026-10-16): Microsoft's own Graph page did not count for "Microsoft Teams",
    because the host must contain the provider's whole name. A page now also counts when the host's registrable name
    is the provider name's first word."""
    from core.builder_tools import _current_run

    token = _current_run.set(f"run_brand_{provider}_{how}")
    try:
        await read_web_page({"url": page}, _server(_Session({"success": True, "markdown": "API docs"})), [])
        events: list[dict] = []
        await report_findings({"options": [{"provider": provider, "how": "api", "sources": [page]}]}, None, events)
    finally:
        _current_run.reset(token)
    assert events[0]["options"][0]["how"] == how


async def test_the_providers_own_server_in_the_registry_is_evidence_for_mcp(monkeypatch):
    import core.safe_fetch as sf
    from core.builder_tools import _current_run

    async def fake(url):
        return json.dumps(REGISTRY_ANSWER).encode()

    monkeypatch.setattr(sf, "fetch_bytes", fake)
    token = _current_run.set("run_research_2")
    try:
        await search_mcp_registry({"provider": "Stripe", "domain": "stripe.com"}, None, [])
        events: list[dict] = []
        await report_findings({"options": [{"provider": "Stripe", "how": "mcp"}]}, None, events)
    finally:
        _current_run.reset(token)
    assert events[0]["options"][0]["how"] == "mcp"
    assert events[0]["options"][0]["sources"] == ["https://mcp.stripe.com"], "its own server is the source"


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


async def test_a_web_reader_that_cannot_run_is_said_once_with_what_to_do_instead():
    """BWA1's live run: the scraper's browser was missing and the model called the web nine more times."""
    session = _Session({"error": "BrowserType.launch: Executable doesn't exist at /x/chrome-headless-shell"})

    async def failing(name, args):
        from types import SimpleNamespace
        return SimpleNamespace(content=[SimpleNamespace(text="BrowserType.launch: Executable doesn't exist at /x")])

    session.call_tool = failing
    got = await read_web_page({"url": "https://docs.goshippo.com/"}, _server(session), [])
    assert "Do not call search_the_web or read_web_page again" in got["error"]


DDG_REFUSED = ("Unfortunately, bots use DuckDuckGo too. Please complete the following challenge to confirm this "
               "search was made by a human.")


@pytest.mark.parametrize("answer, outcome", [
    ({"success": True, "markdown": "results", "links": [
        {"href": "//duckduckgo.com/l/?uddg=https%3A%2F%2Fduffel.com%2Fdocs", "text": "Duffel docs"}]}, "results"),
    ({"success": True, "markdown": DDG_REFUSED, "links": []}, "blocked"),
    ({"success": True, "markdown": "No results.", "links": [{"href": "/html/?q=next", "text": "Next"}]}, "empty"),
    ({"error": "Crawl failed: net::ERR_CONNECTION_RESET"}, "failed"),
])
async def test_every_web_search_is_counted_by_how_it_went_never_by_what_it_asked(monkeypatch, caplog, answer,
                                                                                  outcome):
    """Naveen (2026-10-16): cloud's searches come from AWS addresses, which search engines often refuse as automated,
    and dev searches from a home connection. So each search is counted by how it went, and the ones that found
    nothing are logged; R1's counts choose cloud's fallback. Never the words searched for: they are the person's."""
    from core.telemetry import metrics

    counted: list[str] = []
    monkeypatch.setattr(metrics, "builder_web_search", counted.append)
    with caplog.at_level("WARNING", logger="core.builder_research"):
        got = await search_the_web({"query": "Duffel flights API sandbox"}, _server(_Session(answer)), [])
    assert counted == [outcome]
    logged = [r.getMessage() for r in caplog.records if r.name == "core.builder_research"]
    assert len(logged) == (0 if outcome == "results" else 1)
    assert not any("Duffel" in line or "sandbox" in line for line in logged)
    if outcome == "blocked":
        assert got["results"] == [] and "refused this search" in got["note"]


async def test_a_web_reader_that_is_not_running_counts_as_a_failed_search(monkeypatch):
    from types import SimpleNamespace

    from core.telemetry import metrics

    counted: list[str] = []
    monkeypatch.setattr(metrics, "builder_web_search", counted.append)
    await search_the_web({"query": "Duffel API"}, SimpleNamespace(tool_router={}, agent_sessions={}), [])
    await search_the_web({"query": " "}, SimpleNamespace(tool_router={}, agent_sessions={}), [])
    assert counted == ["failed"], "no words, no search"


async def test_with_no_web_reader_the_research_says_the_web_was_not_checked():
    from types import SimpleNamespace
    got = await search_the_web({"query": "Duffel API"}, SimpleNamespace(tool_router={}, agent_sessions={}), [])
    assert got["results"] == [] and "cannot be read here" in got["note"]


def test_the_researcher_holds_the_three_sources_and_nothing_that_writes():
    researcher = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_researcher")
    assert {"find_in_catalog", "search_mcp_registry", "search_the_web", "read_web_page"} <= set(researcher["tools"])
    # The card is a step of its own after the Researcher (the model forgot it in 2 of 4 live runs), never its tool.
    assert "report_findings" not in researcher["tools"]
    writes = {"create_agent", "update_agent", "create_http_tool", "create_schedule", "delete_agent", "prepare_connection"}
    assert not writes & set(researcher["tools"])


@pytest.fixture
async def builder():
    await seed_native_builder()


@pytest.mark.parametrize("answer, researches", [
    ("find who can", True), ("  Find who can ", True),
    ("approve", False), ("find who can, and also add a step", False), (None, False)])
def test_find_who_can_on_the_plan_goes_to_research_by_the_words_alone(answer, researches):
    """Naveen (2026-10-16): the plan card's "Missing here" offers "Find who can", sending the goal to research. The
    card answers with fixed words, as "Build it" answers `approve`, and the branch is decided in code: a small model
    named research's own route 6 to 9 times in 12, too few for a button. Any other answer reads as it did."""
    from core.native_builder import bundled_definitions
    from core.orchestration.steps import _eval_state_expr

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    assert steps["step_human_review"]["next_step_id"] == "step_review_research"
    branch = steps["step_review_research"]
    assert branch["type"] == "if_else"
    feedback = {} if answer is None else {"feedback": answer}
    value, error = _eval_state_expr(branch["if_condition"], {"review_feedback": feedback}, default=False)
    assert error is None
    assert (branch["if_true_step_id"] if value else branch["if_false_step_id"]) == (
        "step_research" if researches else "step_approval_route")


def test_the_cards_words_are_the_words_the_branch_matches():
    """The synced card sends `RESEARCH_WORDS`; the branch matches them in code. Changed on one side only, the button
    would send the plan to be revised instead."""
    import re
    from pathlib import Path

    from core.native_builder import bundled_definitions

    card = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/cards.tsx"
    if not card.exists():
        pytest.skip("no frontend in this checkout")
    words = re.search(r"export const RESEARCH_WORDS = '([^']+)';", card.read_text()).group(1)
    branch = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_review_research")
    assert f'"{words}"' in branch["if_condition"]


async def test_a_plan_answered_find_who_can_is_researched_and_builds_nothing(client, builder, fake_llm):
    from core.store.resources import save_tool

    await save_tool({"name": "list_kits", "description": "The retainer kits waiting to ship.", "tool_type": "http",
                     "method": "GET", "url": "https://kits.example/v1/waiting", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}})
    fake_llm.script([
        route("create"),                                                   # step_intent
        tool_call("submit_plan", kind="create", title="Kit labels", summary="Labels for each kit.", steps=[
            {"id": "kits", "type": "tool", "name": "List kits", "does": "Reads the kits", "tool": "list_kits"}]),
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Buy shipping labels for our retainer kits"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    fake_llm.script([
        "Shippo has a public API with a test mode.\nShippo — api — an account and a key — https://docs.goshippo.com",
        tool_call("report_findings", goal="buy shipping labels", options=[
            {"provider": "Shippo", "how": "api", "sources": ["https://docs.goshippo.com"]}]),
    ])
    after = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "find who can"}})
    steps = [e.get("step_name") for e in after if e["type"] == "step_start"]
    assert "Research a Provider" in steps and "Route: Approved or Revise" not in steps, steps
    assert [e["options"][0]["provider"] for e in after if e["type"] == "research_findings"] == ["Shippo"]
    assert not [e for e in after if e["type"] == "resource_saved"], "research builds nothing"


async def test_a_goal_with_no_tool_is_researched_and_ends_in_findings(client, builder, fake_llm):
    fake_llm.script([
        route("research"),
        "Shippo has a public API with a test mode; the next step is a plan for you to approve.\n"
        "Shippo — api — an account and an API key — https://docs.goshippo.com",
        tool_call("report_findings", goal="buy shipping labels", options=[
            {"provider": "Shippo", "how": "api", "needs": "An account and an API key",
             "sources": ["https://docs.goshippo.com"]}]),
    ])
    events = await stream(client, "/api/builder/chat", {"message": "Automate buying shipping labels for our kits"})
    steps = [e.get("step_name") for e in events if e["type"] == "step_start"]
    assert "Research a Provider" in steps and "Make the Findings Card" in steps
    cards = [e for e in events if e["type"] == "research_findings"]
    assert len(cards) == 1 and cards[0]["options"][0]["provider"] == "Shippo"
    assert not [e for e in events if e["type"] == "resource_saved"], "research builds nothing"


def test_the_findings_step_asks_for_its_own_tool_on_the_researchers_report():
    """Its words named `search_help`, copied from the how-to step, while its forced tool is `report_findings` (F764):
    the model read an instruction for the wrong tool, and the report it turns into the card only as an input."""
    step = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_findings")
    assert step["forced_tool"] == "report_findings"
    assert "search_help" not in step["prompt_template"]
    assert "`report_findings`" in step["prompt_template"] and "{state.summary}" in step["prompt_template"]
