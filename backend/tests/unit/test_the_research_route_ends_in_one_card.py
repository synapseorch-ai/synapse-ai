"""The research route ends in one findings card and a reply in code's words (SAI2-P5, D300).

The route is code first: `look_up_providers` (no model), then the Researcher only for what is left, finishing on
`report_findings` (E2), then `finish_findings` and `say` in code. Before, a quick step turned the Researcher's prose
into the card (the model forgot the card's tool in 2 of 4 live runs when it held it), and the reply was the
Researcher's own words. Now:
- a report passes through `finish_findings` once: it is never reported twice, which would read the research's
  cleared record and call every verdict `unclear` (SAI2's hazard);
- a Researcher that ends without reporting still leaves a card: code's verdicts, and each provider left over
  `unclear`, with why (F958's class: our words, never a missing answer);
- the reply is written by code from the findings, so channels and v1 read words too, and nothing in it asks for a
  key or says anything was booked.
"""
from __future__ import annotations

import json

import pytest

import core.builder_connect as bc
import core.builder_research as br
import core.safe_fetch as sf
from core.native_builder import build_plan, lookup

SHIPPO_OWN = {"server": {"name": "com.shippo/mcp", "websiteUrl": "https://goshippo.com",
                         "remotes": [{"url": "https://mcp.goshippo.com/mcp"}]}}


@pytest.fixture(autouse=True)
def _fresh_evidence():
    br._LOOSE.clear()
    yield
    br._LOOSE.clear()


@pytest.fixture
def world(monkeypatch):
    state: dict = {}

    async def options():
        return {"catalog": [{"slug": "stripe", "name": "Stripe", "url": "https://stripe.com"}], "providers": {},
                "screens": {}}

    async def fetch(url):
        term = url.split("search=", 1)[1].split("&", 1)[0].lower()
        return json.dumps({"servers": [SHIPPO_OWN] if term == "shippo" else []}).encode()

    async def scrape(server_module, args):
        if args["url"].startswith(br.SEARCH_URL):
            return {"markdown": "", "links": [{"href": "https://mailchimp.com/developer/"}]
                    if "Mailchimp" in args["url"] else []}
        return {"markdown": "# Mailchimp Marketing API\n\nAuthentication: an API key. Endpoints and reference."}

    monkeypatch.setattr(bc, "_read_options", options)
    monkeypatch.setattr(sf, "fetch_bytes", fetch)
    monkeypatch.setattr(br, "_scrape", scrape)
    monkeypatch.setattr(build_plan, "_state", lambda: state)
    return state


async def test_a_report_passes_through_once_and_keeps_its_verdicts(world):
    """SAI2's hazard: the Researcher's report, then finish_findings, and no second report_findings."""
    world.update(request={"providers": ["Shippo", "Mailchimp"]}, user_message="Labels")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    await br.read_web_page({"url": "https://mailchimp.com/developer/"}, None, [])
    reported_events: list[dict] = []
    world["findings"] = await br.report_findings({"goal": "Labels", "options": [
        {"provider": "Mailchimp", "how": "api", "needs": "A Mailchimp account",
         "sources": ["https://mailchimp.com/developer/"], "own_domains": ["mailchimp.com"]}]}, None, reported_events)
    assert [(o["provider"], o["how"]) for o in world["findings"]["options"]] == [("Shippo", "mcp"), ("Mailchimp", "api")]
    events: list[dict] = []
    done = await lookup.finish_findings({}, None, events)
    assert events == [], "the card was put up once, by the report"
    assert [(o["provider"], o["how"]) for o in done["options"]] == [("Shippo", "mcp"), ("Mailchimp", "api")]
    assert "**Shippo** has its own MCP server." in done["said"] and "**Mailchimp** has a public API." in done["said"]


async def test_everything_found_by_code_is_the_card_with_no_model(world):
    world.update(request={"providers": ["Stripe", "Shippo"]}, user_message="Links and labels")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    events: list[dict] = []
    done = await lookup.finish_findings({}, None, events)
    [card] = events
    assert card["type"] == "research_findings" and card["goal"] == "Links and labels"
    assert [(o["provider"], o["how"]) for o in card["options"]] == [("Stripe", "catalog_app"), ("Shippo", "mcp")]
    assert done["status"] == "reported" and "Say \"build it\"" in done["said"]


async def test_a_researcher_that_never_reported_still_leaves_a_card(world):
    world.update(request={"providers": ["Stripe", "Mailchimp", "IRCTC"]})
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    world["findings"] = {"status": "no_report", "fallback": True}
    events: list[dict] = []
    done = await lookup.finish_findings({}, None, events)
    [card] = events
    assert [(o["provider"], o["how"]) for o in card["options"]] == [
        ("Stripe", "catalog_app"), ("Mailchimp", "unclear"), ("IRCTC", "unclear")]
    mailchimp = card["options"][1]
    assert mailchimp["sources"] == ["https://mailchimp.com/developer/"] and "developer docs" in mailchimp["detail"]
    assert "**IRCTC**: I couldn't confirm a way in." in done["said"]


async def test_nothing_to_show_says_so_with_no_card(world):
    world["lookup"] = {"findings": {"goal": "Book hotels", "options": []}, "found": [], "left": []}
    world["findings"] = {"status": "no_report", "fallback": True}
    events: list[dict] = []
    done = await lookup.finish_findings({}, None, events)
    assert events == [] and done["options"] == [] and "Tell me the service you use" in done["said"]


@pytest.mark.parametrize("how, words", [
    ("catalog_app", "is in your Apps catalogue"), ("mcp", "has its own MCP server"), ("api", "has a public API"),
    ("none", "has no public way in"), ("unclear", "I couldn't confirm a way in")])
def test_the_reply_says_each_verdict_in_its_words(how, words):
    said = lookup.said({"options": [{"provider": "Acme", "how": how, "detail": "Details.", "needs": "An account"}]})
    assert f"**Acme** {words}" in said or f"**Acme**: {words}" in said
    assert "It needs: An account." in said


def test_with_no_way_in_the_reply_offers_what_works_and_asks_for_nothing():
    said = lookup.said({"options": [
        {"provider": "OpenTable", "how": "none", "detail": "Its API is for approved partners only."}]})
    assert "leaves the booking or ordering to you" in said
    for asked in ("password", "your key", "card number", "booked", "paid"):
        assert asked not in said.lower()


def test_the_route_is_code_then_the_researcher_then_code():
    from core.native_builder import bundled_definitions

    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    first = steps["step_research"]
    assert (first["type"], first["forced_tool"], first["tool_arguments"]) == (
        "tool", "look_up_providers", {"by_route": True})
    assert first["next_step_id"] == "step_lookup_route"
    assert steps["step_lookup_route"]["switch_cases"] == {"found": "step_findings",
                                                          "research": "step_lookup_research"}
    research = steps["step_lookup_research"]
    assert research["finish_tools"] == ["report_findings"] and research["finish_fallback"] == {"status": "no_report"}
    assert "{state.lookup.left}" in research["prompt_template"]
    assert research["next_step_id"] == research["on_exhausted_step_id"] == "step_findings"
    card = steps["step_findings"]
    assert (card["forced_tool"], card["tool_arguments"], card["next_step_id"]) == (
        "finish_findings", {}, "step_findings_said")
    said = steps["step_findings_said"]
    assert (said["type"], said["print_content"], said["output_key"]) == ("print", "{state.findings.said}", "summary")


@pytest.mark.parametrize("value, branch", [
    ({"found": [{"provider": "Stripe"}], "left": []}, "found"),
    ({"found": [{"provider": "Stripe"}], "left": [{"provider": "IRCTC"}]}, "research"),
    ({"found": [], "left": []}, "research"),
])
def test_only_everything_found_skips_the_researcher(value, branch):
    from core.native_builder import bundled_definitions
    from core.orchestration.steps import _eval_state_expr

    route = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_lookup_route")
    got, error = _eval_state_expr(route["switch_expression"], {"lookup": value}, default=None)
    assert error is None and got == branch


def test_a_researcher_that_names_its_own_providers_looks_them_up_in_code():
    """11 of research 12's openings name no provider: the Researcher names them, and one code call looks them all up
    (catalogue, Registry, developer page), instead of a catalogue call, a Registry search, a web search and a page
    each. It holds the tool, and its step's words say when to call it."""
    from core.native_builder import bundled_definitions

    researcher = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_researcher")
    assert "look_up_providers" in researcher["tools"]
    step = next(s for s in bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_lookup_research")
    assert "If none is listed" in step["prompt_template"] and "`look_up_providers` once" in step["prompt_template"]


async def test_providers_the_researcher_names_are_named(world):
    got = await lookup.look_up_providers({"providers": ["Stripe", "Shippo"]}, None, [])
    assert [(f["provider"], f["how"]) for f in got["found"]] == [("Stripe", "catalog_app"), ("Shippo", "mcp")]


async def test_a_researcher_that_looked_up_its_own_and_never_reported_still_says_them(world):
    world["lookup"] = {"findings": {"goal": "Payments", "options": []}, "found": [], "left": []}
    await lookup.look_up_providers({"providers": ["Stripe"]}, None, [])
    world["findings"] = {"status": "no_report", "fallback": True}
    events: list[dict] = []
    await lookup.finish_findings({}, None, events)
    assert [(o["provider"], o["how"]) for o in events[0]["options"]] == [("Stripe", "catalog_app")]


async def test_a_researchers_own_unsettled_look_up_stays_on_the_fallback_card(world):
    """SAI2's review, N3a: what the Researcher looked up itself and never reported is said unclear, with why."""
    world["lookup"] = {"findings": {"goal": "Mail", "options": []}, "found": [], "left": []}
    await lookup.look_up_providers({"providers": ["Mailchimp"]}, None, [])
    world["findings"] = {"status": "no_report", "fallback": True}
    events: list[dict] = []
    await lookup.finish_findings({}, None, events)
    [option] = events[0]["options"]
    assert (option["provider"], option["how"]) == ("Mailchimp", "unclear") and "developer docs" in option["detail"]


SEEN_NOTHING = "I don't see anything in this workspace that does this today."


async def test_a_request_naming_no_provider_opens_with_what_code_saw(world):
    """F997 (SAI2, cnt-02 "Call my mom right now"): the person named no provider, so the reply opens with what code
    saw here, then the options. Code has not checked every tool (F998), so it says what it saw, never "can't"."""
    world.update(request={"providers": []}, user_message="Call my mom right now and tell her I'll be late.")
    world["lookup"] = {"findings": {"goal": "Call my mom", "options": []}, "found": [],
                       "left": [{"provider": "Twilio", "note": "Its own pages are for the research to read."}]}
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert done["said"].startswith(SEEN_NOTHING + "\n\n- **Twilio**")


async def test_a_request_naming_no_provider_with_nothing_found_says_both(world):
    world.update(request={"providers": []}, user_message="Call my mom right now.")
    world["lookup"] = {"findings": {"goal": "Call my mom", "options": []}, "found": [], "left": []}
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert done["said"].startswith(SEEN_NOTHING + " I couldn't find who provides this")


async def test_a_named_provider_gets_no_such_line(world):
    world.update(request={"providers": ["Stripe"]}, user_message="Take payments through Stripe")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert SEEN_NOTHING not in done["said"] and done["said"].startswith("- **Stripe**")


async def test_a_researchers_report_on_a_request_naming_no_provider_opens_with_the_line(world):
    world.update(request={"providers": []}, user_message="Send our patients text reminders")
    world["lookup"] = {"findings": {"goal": "Text reminders", "options": []}, "found": [], "left": []}
    await lookup.look_up_providers({"providers": ["Stripe"]}, None, [])
    world["findings"] = await br.report_findings({"goal": "Text reminders", "options": [
        {"provider": "Stripe", "how": "catalog_app", "app": {"slug": "stripe", "name": "Stripe"}}]}, None, [])
    done = await lookup.finish_findings({}, None, [])
    assert done["status"] == "reported" and done["said"].startswith(SEEN_NOTHING + "\n\n- **Stripe**")


async def test_a_run_with_no_request_claims_nothing_about_the_workspace(world):
    """A paused run on the old definition, or a channel turn, has no `request`: code doesn't know what was named."""
    world["lookup"] = {"findings": {"goal": "Book hotels", "options": []}, "found": [], "left": []}
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert SEEN_NOTHING not in done["said"]


@pytest.mark.parametrize("category", ["hotel booking", "phone call"])
async def test_a_category_the_classify_named_as_a_provider_still_gets_the_line(world, category):
    """SAI2's (b), from res-02: the classify wrote request.providers = ["hotel booking"]. A "provider" the look-up
    matched to nothing (no catalogue entry, no Registry server of its own, no own site) and that is no domain counts
    as unnamed, so it can't hide the line. True even of a real company too small to find: code still saw nothing here
    that does the job."""
    world.update(request={"providers": [category]}, user_message="Call my mom right now.")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    assert world["lookup"]["unmatched"] == [category]
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert done["said"].startswith(SEEN_NOTHING)


async def test_a_provider_the_look_up_found_anything_of_keeps_its_name(world):
    """Shippo has its own server, Mailchimp its own developer page: both matched, so no line."""
    world.update(request={"providers": ["Mailchimp", "Shippo"]}, user_message="Mail and labels")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    assert world["lookup"]["unmatched"] == []
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert SEEN_NOTHING not in done["said"]


async def test_one_matched_provider_among_unmatched_ones_keeps_the_request_named(world):
    world.update(request={"providers": ["hotel booking", "Stripe"]}, user_message="Book hotels and pay with Stripe")
    world["lookup"] = await lookup.look_up_providers({"by_route": True}, None, [])
    assert world["lookup"]["unmatched"] == ["hotel booking"]
    world["findings"] = {"status": "no_report", "fallback": True}
    done = await lookup.finish_findings({}, None, [])
    assert SEEN_NOTHING not in done["said"]
