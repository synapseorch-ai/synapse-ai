"""A partner-only API is no way in (F1094, SAI2-P5 with SAI2-P3; SAI2 10-27, from P5's measure).

Haiku 4.5's res-02/03/06: the Researcher reported Booking.com, Uber and OpenTable as `api`, though its own words said
what kept the person out ("Managed Affiliate Partner registration, Partner Centre access"; "the privileged `request`
scope, which in production needs Uber's Full Access approval"), and the build then planned with them. The research
rules already say a partners-only provider is `none`; the model wrote `api` anyway. Code reads the model's own words
in `report_findings`, after the `api` verdict stands, and sets `how: "partner"` with the words it matched. The schema
the model writes to is unchanged (`_HOW`): `partner` is code's, so a model that writes it gets `unclear`.

A sentence that only wonders ("I did not read whether production keys need partner approval") is not a partner gate.
"""
from __future__ import annotations

import pytest

import core.builder_research as br
from core.native_builder.lookup import said

BOOKING = "https://developers.booking.com/connectivity/docs"


@pytest.fixture(autouse=True)
def _fresh_evidence():
    br._LOOSE.clear()
    yield
    br._LOOSE.clear()


async def _report(provider: str, how: str, needs: str, detail: str = "", page: str = BOOKING) -> dict:
    await br._read_host(page)
    card = await br.report_findings({"goal": "Book hotels", "options": [
        {"provider": provider, "how": how, "detail": detail, "needs": needs, "sources": [page]}]}, None, [])
    [option] = card["options"]
    return option


async def test_res_02s_own_needs_make_booking_com_partner_only():
    option = await _report("Booking.com", "api",
                           "Managed Affiliate Partner registration, Partner Centre access, an API key and an "
                           "X-Affiliate-Id.", "Its API can book hotel rooms, and a sandbox lets you test.")
    assert option["how"] == "partner"
    assert option["partner_words"] == ["Managed Affiliate Partner registration", "Partner Centre"]


async def test_a_gate_that_needs_an_approval_is_partner_only():
    option = await _report("Uber", "api", "An Uber developer account and an OAuth 2.0 app. Ride requests use the "
                           "privileged `request` scope, which in production needs Uber's Full Access approval.",
                           page="https://developer.uber.com/docs/riders")
    assert option["how"] == "partner" and option["partner_words"] == ["needs Uber's Full Access approval"]


@pytest.mark.parametrize("needs", [
    "A Mailchimp account and an API key.",
    "A GoDaddy account and an API key. I did not read whether production keys need partner approval.",
    "A Duffel account and a token. Whether a new workspace can get access without partner approval is not confirmed.",
    "A paid Calendly plan and a token. I did not confirm whether partner approval is also required.",
    "No contract and no partnership needed: a free account and a key.",
])
async def test_a_plain_public_api_stays_api(needs):
    option = await _report("Mailchimp", "api", needs, page="https://mailchimp.com/developer/")
    assert option["how"] == "api" and "partner_words" not in option


async def test_only_an_api_verdict_is_read_for_a_gate():
    """`none` already says no way in, and `unclear` was never a verdict."""
    option = await _report("Booking.com", "none", "Managed Affiliate Partner registration.")
    assert option["how"] == "none" and "partner_words" not in option


async def test_a_model_cannot_write_partner_itself():
    option = await _report("Booking.com", "partner", "Managed Affiliate Partner registration.")
    assert option["how"] == "unclear"


async def test_a_partner_gate_drops_the_spec():
    """A spec of calls the person can't make is no tool to build."""
    await br._read_host(BOOKING)
    option = await br.report_findings({"options": [{
        "provider": "Booking.com", "how": "api", "needs": "Approved partners only.", "sources": [BOOKING],
        "api": {"base_url": "https://distribution-xml.booking.com", "auth": "basic", "docs": BOOKING,
                "calls": [{"name": "book_room", "method": "POST", "path": "/book"}]}}]}, None, [])
    [option] = option["options"]
    assert option["how"] == "partner" and "api" not in option


def test_the_reply_says_it_is_for_approved_partners_and_offers_what_works():
    text = said({"options": [{"provider": "Booking.com", "how": "partner", "detail": "Its API can book rooms.",
                              "partner_words": ["Managed Affiliate Partner registration"]}]})
    assert "**Booking.com** is for approved partners only (Managed Affiliate Partner registration)." in text
    assert "Nothing here can do it for you directly" in text, "a partner-only provider is no way in"


def test_the_partner_value_stays_out_of_the_models_schema():
    assert "partner" not in br._HOW


def test_the_words_keep_the_name_before_them_but_not_an_article():
    assert br.partner_words("A Kayak partner account and test keys.") == ["Kayak partner account"]
    assert br.partner_words("A partner account.") == ["partner account"]


# ── SAI2's review of a0f640fa: words public developer docs use too ────────────────────────────────────────────────

@pytest.mark.parametrize("needs", [
    "A key. Its OpenAPI contract is published.",
    "An account; the API contract and contract tests are on GitHub.",
    "A data contract for each event, and an API key.",
    "A Google Cloud project; production needs OAuth app review approval.",
    "Production needs Meta's app review approval for the messaging permission.",
])
async def test_a_public_apis_own_words_stay_api(needs):
    option = await _report("Acme", "api", needs, page="https://acme.com/developers")
    assert option["how"] == "api" and "partner_words" not in option


@pytest.mark.parametrize("needs, words", [
    ("It needs a signed contract.", ["signed contract"]),
    ("Access is under contract only.", ["under contract"]),
    ("A commercial contract with Booking.com and a key.", ["commercial contract"]),
    ("A contract with Expedia Group.", ["contract with Expedia"]),
])
async def test_a_contract_beside_a_gate_word_is_partner_only(needs, words):
    option = await _report("Acme", "api", needs, page="https://acme.com/developers")
    assert option["how"] == "partner" and option["partner_words"] == words


# ── SAI2's (a), from the gate measure's res-03: the provider grants or approves the developer's access ─────────────

@pytest.mark.parametrize("needs, words", [
    ("A Uber developer account and an OAuth 2.0 app. For production use across all riders, Uber must grant FULL "
     "ACCESS; a sandbox is available.", ["Uber must grant FULL ACCESS"]),
    ("The ride-request scope is privileged, so Uber must approve the app before it is used beyond development.",
     ["Uber must approve the app"]),
    ("A developer account; ride booking requires Expedia Group to approve your access.",
     ["requires Expedia Group to approve your access"]),
    ("A key. Production access is granted by Kayak.", ["Production access is granted by Kayak"]),
])
async def test_a_provider_that_grants_the_access_is_partner_only(needs, words):
    option = await _report("Acme", "api", needs, page="https://acme.com/developers")
    assert option["how"] == "partner" and option["partner_words"] == words


@pytest.mark.parametrize("needs", [
    "Each patient must sign in to Uber and grant permission before any ride can be requested.",
    "An OAuth app; the user grants your app access to their calendar.",
    "Users grant access on the consent screen, and the token is kept on the Connect card.",
    "Patients must grant the app access on Uber's consent screen.",
    "You must grant the app access in Settings before the first call.",
    "Google must approve your OAuth app before it reads Gmail for anyone outside your organisation.",
    "A Stripe administrator must enable MCP access for the team.",
    "A Google account, and OAuth access granted to this workspace for the calendar.",
])
async def test_the_person_granting_the_app_access_stays_api(needs):
    option = await _report("Acme", "api", needs, page="https://acme.com/developers")
    assert option["how"] == "api" and "partner_words" not in option


@pytest.mark.xfail(strict=True, reason="known limit (SAI2, e89fdcfe): an app review said with no review word reads as a "
                                       "provider's grant; Meta's review is open to any developer. No recorded option "
                                       "says it this way (the 313-option replay); fix if a measure shows one")
async def test_an_app_review_with_no_review_word_stays_api():
    option = await _report("Meta", "api", "A Meta app. Meta must approve your app before it can post to a Page.",
                           page="https://developers.facebook.com/docs/pages-api")
    assert option["how"] == "api"
