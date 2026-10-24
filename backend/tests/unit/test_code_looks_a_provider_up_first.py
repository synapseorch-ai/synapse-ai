"""Code looks a provider up before any model reads the web (SAI2-P5, D300).

Research was web-first: the Researcher searched, read page heads of 6000 characters and wrote prose a quick model
turned into the findings card. It was the weakest class on every model (Nova Pro 1/12 at SAI2's re-baseline) and the
dearest (about 150k tokens a persona). `look_up_providers` looks where the answer is known first, in code:

1. this product's Apps catalogue (a hit is `catalog_app`, with the app);
2. the MCP Registry (the provider's own reachable server is `mcp`, by `provider_owns`, its anchors by F951);
3. one web search for the provider's developer page, read only on the provider's own domain (`_providers_own_page`).

Code gives only the two verdicts it can stand behind: an app in the catalogue and the provider's own server. A
developer page is a lead, never a verdict (Naveen, 10-24): Booking.com, OpenTable and Uber have developer pages and
are partners-only, so whether an API is open is for the research to read. Code never writes `none` or `unclear`,
never an `api` spec, and says when a source could not be read.
"""
from __future__ import annotations

import json
from urllib.parse import unquote_plus

import pytest

import core.builder_connect as bc
import core.builder_research as br
import core.safe_fetch as sf
from core.native_builder import lookup

CATALOG = [
    {"slug": "stripe", "name": "Stripe", "auth": "oauth", "url": "https://stripe.com"},
    {"slug": "google_calendar", "name": "Google Calendar", "auth": "oauth", "url": "https://calendar.google.com"},
]

SHIPPO_OWN = {"server": {"name": "com.shippo/mcp", "description": "Shippo's own", "websiteUrl": "https://goshippo.com",
                         "remotes": [{"type": "streamable-http", "url": "https://mcp.goshippo.com/mcp"}]}}
MAILCHIMP_THIRD_PARTY = {"server": {"name": "io.github.someone/mailchimp-mcp", "description": "unofficial",
                                    "remotes": [{"url": "https://mailchimp-mcp.someone.dev/mcp"}]}}
DUFFEL_BY_HAND = {"server": {"name": "com.duffel/mcp", "websiteUrl": "https://duffel.com",
                             "repository": {"url": "https://github.com/duffelhq/mcp"}}}

DOCS = ("# Mailchimp Marketing API\n\nIntro text.\n\n## Authentication\n\nUse an API key in the Authorization "
        "header. Endpoints live under https://{dc}.api.mailchimp.com/3.0.\n\n## Reference\n\nLists, campaigns.")


@pytest.fixture(autouse=True)
def _fresh_evidence():
    br._LOOSE.clear()
    yield
    br._LOOSE.clear()


@pytest.fixture
def world(monkeypatch):
    """The catalogue, the Registry and the web, with what each was asked."""
    asked = {"registry": [], "scraped": []}
    state = {"catalog": list(CATALOG), "registry": {}, "results": {}, "pages": {}, "registry_down": False,
             "refused": False}

    async def options():
        return {"catalog": state["catalog"], "providers": {}, "screens": {}}

    async def fetch(url):
        asked["registry"].append(url)
        if state["registry_down"]:
            raise sf.FetchRefused("blocked")
        term = url.split("search=", 1)[1].split("&", 1)[0].lower()
        return json.dumps({"servers": state["registry"].get(term, [])}).encode()

    async def scrape(server_module, args):
        url = args["url"]
        asked["scraped"].append(url)
        if url.startswith(br.SEARCH_URL):
            if state["refused"]:
                return {"markdown": "Unfortunately, bots use DuckDuckGo too.", "links": []}
            term = unquote_plus(url[len(br.SEARCH_URL):]).lower().removesuffix(" api documentation").replace(" ", "")
            return {"markdown": "results", "links": [{"href": u, "text": u} for u in state["results"].get(term, [])]}
        if url in state["pages"]:
            return {"markdown": state["pages"][url]}
        return {"error": "404"}

    monkeypatch.setattr(bc, "_read_options", options)
    monkeypatch.setattr(sf, "fetch_bytes", fetch)
    monkeypatch.setattr(br, "_scrape", scrape)
    return state, asked


async def _look(**args) -> dict:
    return await lookup.look_up_providers(args, None, [])


# ── 1. The catalogue ──────────────────────────────────────────────────────────────────────────────────────────────

async def test_an_app_in_the_catalogue_is_found_by_code(world):
    _, asked = world
    got = await _look(providers=["Stripe"], need="Send a payment link")
    assert got["left"] == []
    [stripe] = got["found"]
    assert stripe["how"] == "catalog_app" and stripe["app"] == {"slug": "stripe", "name": "Stripe"}
    assert stripe["provider"] == "Stripe" and "api" not in stripe
    assert got["findings"] == {"goal": "Send a payment link", "options": [stripe]}
    assert asked == {"registry": [], "scraped": []}, "a catalogue hit needs nothing else"


async def test_a_catalogue_word_alone_is_not_the_provider(world):
    """"Google Drive" shares a word with Google Calendar; a verdict needs the provider's whole name."""
    state, _ = world
    got = await _look(providers=["Google Drive"])
    assert got["found"] == [] and [x["provider"] for x in got["left"]] == ["Google Drive"]


# ── 2. The Registry ───────────────────────────────────────────────────────────────────────────────────────────────

async def test_the_providers_own_reachable_server_is_found(world):
    state, asked = world
    state["registry"]["shippo"] = [SHIPPO_OWN]
    got = await _look(providers=["Shippo"])
    [shippo] = got["found"]
    assert shippo["how"] == "mcp" and shippo["sources"][0] == "https://goshippo.com"
    assert {"goshippo.com", "shippo.com"} <= set(shippo["own_domains"])
    assert "https://mcp.goshippo.com/mcp" in shippo["detail"], "the card can say where it connects"
    assert asked["scraped"] == [], "found in the Registry: the web is not searched"


async def test_a_third_partys_server_is_not_the_providers(world):
    state, _ = world
    state["registry"]["mailchimp"] = [MAILCHIMP_THIRD_PARTY]
    got = await _look(providers=["Mailchimp"])
    assert got["found"] == [] and got["left"][0]["provider"] == "Mailchimp"


async def test_an_own_server_installed_by_hand_lends_its_anchors_and_goes_on(world):
    state, asked = world
    state["registry"]["duffel"] = [DUFFEL_BY_HAND]
    state["results"]["duffel"] = ["https://duffel.com.evil.io/docs", "https://duffel.com/docs/api"]
    state["pages"]["https://duffel.com/docs/api"] = "# Duffel API\n\nAuthentication with an access token. Endpoints."
    got = await _look(providers=["Duffel"])
    assert got["found"] == []
    [left] = got["left"]
    assert left["lead"]["sources"] == ["https://duffel.com/docs/api"]
    assert "https://duffel.com.evil.io/docs" not in asked["scraped"]


# ── 3. The provider's own developer page: a lead ──────────────────────────────────────────────────────────────────

async def test_an_own_developer_page_is_a_lead_never_a_verdict(world):
    state, asked = world
    state["results"]["mailchimp"] = ["https://mailchimp.evil.com/api", "https://evilmailchimp.io/docs",
                                     "https://mailchimp.com/developer/marketing/"]
    state["pages"]["https://mailchimp.com/developer/marketing/"] = DOCS
    got = await _look(providers=["Mailchimp"])
    assert got["found"] == []
    [left] = got["left"]
    lead = left["lead"]
    assert lead["how"] == "api" and "api" not in lead, "a lead has no spec: only report_findings writes one"
    assert lead["sources"] == ["https://mailchimp.com/developer/marketing/"]
    assert lead["own_domains"] == ["mailchimp.com"]
    assert left["page"] == "https://mailchimp.com/developer/marketing/"
    assert "page_text" not in left, "a page's words stay out of the step's output and the run's history"
    assert asked["scraped"][1:] == ["https://mailchimp.com/developer/marketing/"], "look-alikes are never read"
    assert "mailchimp.com" in br._evidence()["read_hosts"], "report_findings may cite the page it read"


async def test_only_look_alikes_found_reads_nothing_and_says_so(world):
    state, asked = world
    state["results"]["mailchimp"] = ["https://mailchimp.evil.com/api", "https://mailchimp.xyz/docs"]
    got = await _look(providers=["Mailchimp"])
    [left] = got["left"]
    assert "lead" not in left and "own" in left["note"]
    assert len(asked["scraped"]) == 1, "only the search page"


async def test_a_page_without_api_words_is_no_lead_but_is_handed_on(world):
    """What a `none` rests on: the provider's own page, read, saying orders go by phone."""
    state, _ = world
    state["results"]["henryschein"] = ["https://henryschein.com/us-en/ordering"]
    state["pages"]["https://henryschein.com/us-en/ordering"] = "# Ordering\n\nOrders are placed by phone or online."
    got = await _look(providers=["Henry Schein"])
    [left] = got["left"]
    assert "lead" not in left and left["page"] == "https://henryschein.com/us-en/ordering"
    assert "nothing of an API" in left["note"]
    assert "henryschein.com" in br._evidence()["read_hosts"], "a none may rest on it"


async def test_code_never_writes_none_unclear_or_a_spec(world):
    state, _ = world
    state["registry"]["shippo"] = [SHIPPO_OWN]
    state["results"]["mailchimp"] = ["https://mailchimp.com/developer/"]
    state["pages"]["https://mailchimp.com/developer/"] = DOCS
    got = await _look(providers=["Stripe", "Shippo", "Mailchimp", "IRCTC", "OpenTable"])
    assert {f["how"] for f in got["found"]} <= {"catalog_app", "mcp"}
    assert all(x["lead"]["how"] == "api" for x in got["left"] if "lead" in x)
    assert not any("api" in f for f in got["found"]) and not any("api" in x.get("lead", {}) for x in got["left"])


# ── What could not be read is said ────────────────────────────────────────────────────────────────────────────────

async def test_a_registry_that_cannot_be_read_is_said(world):
    state, _ = world
    state["registry_down"] = True
    [left] = (await _look(providers=["Mailchimp"]))["left"]
    assert "Registry could not be read" in left["note"]


async def test_a_refused_search_is_said(world):
    state, _ = world
    state["refused"] = True
    [left] = (await _look(providers=["Mailchimp"]))["left"]
    assert "refused" in left["note"]


# ── Cached in the run, bounded, and apart from the researcher's backstop ──────────────────────────────────────────

async def test_a_second_look_up_in_the_run_reads_nothing_again(world):
    state, asked = world
    state["registry"]["shippo"] = [SHIPPO_OWN]
    state["results"]["mailchimp"] = ["https://mailchimp.com/developer/"]
    state["pages"]["https://mailchimp.com/developer/"] = DOCS
    first = await _look(providers=["Shippo", "Mailchimp"])
    before = {k: list(v) for k, v in asked.items()}
    again = await _look(providers=["shippo", "Mailchimp"])
    assert asked == before and again["found"] == first["found"] and again["left"] == first["left"]


async def test_the_look_up_spends_none_of_the_researchers_backstop(world):
    state, _ = world
    state["results"]["mailchimp"] = ["https://mailchimp.com/developer/"]
    state["pages"]["https://mailchimp.com/developer/"] = DOCS
    await _look(providers=["Mailchimp", "Calendly"])
    assert not br._evidence().get("spent")


async def test_at_most_five_providers_are_looked_up(world):
    got = await _look(providers=[f"Provider{i}" for i in range(8)])
    assert len(got["found"]) + len(got["left"]) == 5


# ── Inputs: P4's providers, the build route's needs, or the message alone ─────────────────────────────────────────

async def test_with_no_providers_code_takes_them_from_the_message(world):
    """A paused run on the old definition, or a channel turn, carries no `request` (SAI2, 10-24)."""
    state, _ = world
    got = await _look(need="Send our patients a Stripe link, and book them in through calendly.com")
    assert [f["provider"] for f in got["found"]] == ["Stripe"]
    assert [x["provider"] for x in got["left"]] == ["Calendly"]


async def test_a_message_naming_nothing_known_leaves_it_all_to_the_research(world):
    got = await _look(need="Can an agent book hotel rooms for our staff conferences?")
    assert got == {"findings": {"goal": "Can an agent book hotel rooms for our staff conferences?", "options": []},
                   "found": [], "left": []}


async def test_a_need_without_a_provider_takes_a_catalogue_name_only(world):
    """The build route's needs (SAI2-P3): a tool the Plan calls and what its step does."""
    got = await _look(needs=[{"tool": "send_payment_link", "does": "Send the patient a Stripe payment link"},
                             {"tool": "send_newsletter", "does": "Send the monthly newsletter"}])
    assert [f["provider"] for f in got["found"]] == ["Stripe"]
    assert got["left"] == [{"tool": "send_newsletter", "does": "Send the monthly newsletter",
                            "note": "No provider is named for this yet: the research finds who provides it."}]


async def test_a_need_keeps_its_tool_on_what_is_left(world):
    got = await _look(needs=[{"tool": "add_subscriber", "does": "Add a subscriber", "provider": "Mailchimp"}])
    [left] = got["left"]
    assert left["provider"] == "Mailchimp" and left["tool"] == "add_subscriber"


# ── The contract: report_findings and the build route read it unchanged (P3B-HANDOFF.md) ──────────────────────────

FINDING_KEYS = {"provider", "how", "detail", "needs", "sources", "own_domains"}


async def test_every_found_is_report_findings_shape_and_the_build_reads_it(world):
    from core.native_builder import objects as O
    from core.native_builder.build_plan import _found

    state, _ = world
    state["registry"]["shippo"] = [SHIPPO_OWN]
    got = await _look(providers=["Stripe", "Shippo"])
    for f in got["found"]:
        assert FINDING_KEYS <= set(f) <= FINDING_KEYS | {"app"}, f
    plan = O.PlanDraft(kind="create", title="t", summary="s", steps=[
        O.Step(id="a", type="tool", name="Link", does="Send a Stripe payment link", tool="stripe_payment_link"),
        O.Step(id="b", type="tool", name="Label", does="Buy a Shippo label", tool="shippo_buy_label")])
    status = _found(plan, {"plan_findings": got["findings"]})
    assert (status["stripe_payment_link"].status, status["stripe_payment_link"].via) == ("found", "catalog_app")
    assert (status["shippo_buy_label"].status, status["shippo_buy_label"].via) == ("found", "mcp_server")


async def test_report_findings_puts_up_the_look_ups_finals_with_no_model(world):
    """The research route with everything found: `step_findings` calls report_findings with nothing of its own."""
    state, _ = world
    state["registry"]["shippo"] = [SHIPPO_OWN]
    got = await _look(providers=["Stripe", "Shippo"], need="Labels and links")
    events: list[dict] = []
    card = await br.report_findings({}, None, events)
    assert card["options"] == got["found"]
    assert [e["type"] for e in events] == ["research_findings"]


async def test_report_findings_merges_the_look_up_with_the_researchers_report(world):
    state, _ = world
    state["results"]["mailchimp"] = ["https://mailchimp.com/developer/"]
    state["pages"]["https://mailchimp.com/developer/"] = DOCS
    got = await _look(providers=["Stripe", "Mailchimp"], need="Links and a newsletter")
    events: list[dict] = []
    card = await br.report_findings({"goal": "Links and a newsletter", "options": [
        {"provider": "Mailchimp", "how": "api", "detail": "Open API with a key.", "needs": "A Mailchimp account",
         "sources": ["https://mailchimp.com/developer/"]}]}, None, events)
    assert [(o["provider"], o["how"]) for o in card["options"]] == [("Stripe", "catalog_app"), ("Mailchimp", "api")]
    assert len(events) == 1 and events[0]["options"] == card["options"]
    assert got["left"][0]["provider"] == "Mailchimp"


async def test_a_model_cannot_upgrade_or_drop_a_look_up_final(world):
    state, _ = world
    await _look(providers=["Stripe"])
    card = await br.report_findings({"options": [{"provider": "Stripe", "how": "mcp", "sources": []}]}, None, [])
    assert [(o["provider"], o["how"]) for o in card["options"]] == [("Stripe", "catalog_app")], "one Stripe, code's"


async def test_a_checked_none_on_the_providers_own_page_replaces_a_look_up_final(world):
    """The one downgrade: a verdict of none resting on a page of the provider's own the research read."""
    state, _ = world
    state["pages"]["https://stripe.com/partners"] = "# Partners\n\nThis API is for partners only."
    await _look(providers=["Stripe"])
    await br.read_web_page({"url": "https://stripe.com/partners"}, None, [])
    card = await br.report_findings({"options": [{"provider": "Stripe", "how": "none", "detail": "Partners only.",
                                                  "sources": ["https://stripe.com/partners"]}]}, None, [])
    assert [(o["provider"], o["how"]) for o in card["options"]] == [("Stripe", "none")]


def test_the_look_up_is_a_read_the_builder_may_call():
    from core.builder_access import READS
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    assert "look_up_providers" in READS
    assert any(s["function"]["name"] == "look_up_providers" for s in BUILDER_TOOL_SCHEMAS)


async def test_the_researcher_reads_a_looked_up_page_again_for_free(world):
    """The look-up's page goes to the Researcher through read_web_page, from the run's record: no second read, no
    backstop spent, and at most 3000 characters."""
    state, asked = world
    long_docs = DOCS + "\n\n" + "\n\n".join(f"Changelog entry {i}, nothing to see." for i in range(400))
    state["results"]["mailchimp"] = ["https://mailchimp.com/developer/marketing/"]
    state["pages"]["https://mailchimp.com/developer/marketing/"] = long_docs
    await _look(providers=["Mailchimp"])
    before = list(asked["scraped"])
    got = await br.read_web_page({"url": "https://mailchimp.com/developer/marketing/"}, None, [])
    assert asked["scraped"] == before and not br._evidence().get("spent")
    assert "## Authentication" in got["page_text"] and len(got["page_text"]) <= 3000
    await br.report_findings({"options": [{"provider": "Mailchimp", "how": "unclear"}]}, None, [])
    assert br.PAGES_KEY not in br._evidence(), "a report clears the pages with the rest of the record"
