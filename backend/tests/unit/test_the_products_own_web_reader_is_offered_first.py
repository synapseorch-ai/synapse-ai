"""A need the product itself meets is offered from the product, never looked up outside as missing (F1629).

Asked to open a docs site, read its sidebar's section names and save them to the Vault, Synapse AI took the research
route, looked up outside browser services and said "Nothing here can do it for you directly … leaves the booking or
ordering to you": the research said nothing in the workspace did it whenever no provider was named, without looking,
and closed every request with words written for a booking. Now code first matches the need against what the product
itself does (reading or crawling a web page with its own web reader; a page that needs clicks or a login with a
browser agent, where a browser is installed), offers that to build, and only otherwise looks outside; and the close
of a lookup that found no way in fits any request. Each match has its near miss: a need the product does not meet is
looked up as before.
"""
from __future__ import annotations

import pytest

from core.native_builder import lookup


@pytest.fixture
def browser_here(monkeypatch):
    monkeypatch.setattr(lookup, "_browser_installed", lambda: True, raising=False)


class TestTheProductsOwnWayIsOffered:
    def test_reading_a_web_page(self, browser_here):
        said = lookup.own_capability("Open docs.synapseorch.com in the browser, read the names of the main sections "
                                     "in its sidebar, and save them as a list to the Vault file docs-sections.md.")
        assert said and "web reader" in said and "build" in said, said

    def test_a_page_behind_a_login(self, browser_here):
        said = lookup.own_capability("Log in to my supplier's portal and download this month's invoice.")
        assert said and "browser agent" in said, said

    def test_no_browser_installed_offers_no_browser_agent(self, monkeypatch):
        monkeypatch.setattr(lookup, "_browser_installed", lambda: False, raising=False)
        said = lookup.own_capability("Log in to my supplier's portal and download this month's invoice.")
        assert not said or "browser agent" not in said, said


class TestANeedTheProductDoesNotMeetIsLookedUp:
    @pytest.mark.parametrize("need", ["Book me a flight to Paris next Friday.",
                                      "Send an SMS to my customers when an order ships."])
    def test_it_is_not_claimed(self, browser_here, need):
        assert lookup.own_capability(need) is None


class TestTheCloseFitsAnyRequest:
    def test_no_way_in_never_speaks_of_booking(self):
        said = lookup.said({"options": [{"provider": "Example", "how": "none"}]})
        assert "booking or ordering" not in said, said


def _routed(message: str, kind: str = "research", providers: list[str] | None = None):
    from core.native_builder.classify import override
    from core.native_builder.objects import Request

    return override(Request(kind=kind, needs="none", providers=providers or []), message=message,
                    state={"may_change": True, "may_act": True}, inventory={}, connectable=set())


class TestAResearchAskTheProductMeetsIsPlanned:
    def test_reading_a_web_page_is_planned_with_the_web_reader(self, browser_here):
        routed = _routed("Open docs.synapseorch.com in the browser, read the names of the main sections in its "
                         "sidebar, and save them as a list to the Vault file docs-sections.md.")
        assert (routed.kind, routed.route) == ("build", "step_plan_room"), routed


class TestWhatIsNotAThingToMakeIsNotBuilt:
    """Near misses: a question about a page wants an answer; a booking or a message is the outside lookup's; a named
    outside service is looked up as before."""

    @pytest.mark.parametrize("message", [
        "What does https://example.com/pricing say about refunds?",
        "Can you read this page and tell me what it says about refunds? https://example.com/pricing",
        "Read https://example.com/pricing and tell me what it says about refunds",
    ])
    def test_a_question_about_a_page(self, browser_here, message):
        assert _routed(message).kind != "build", message

    @pytest.mark.parametrize("message", ["Book me a flight to Paris next Friday.",
                                         "Send an SMS to my customers when an order ships."])
    def test_a_need_the_product_does_not_meet(self, browser_here, message):
        assert _routed(message).kind == "research", message

    def test_a_named_outside_service(self, browser_here):
        routed = _routed("Read my Notion pages and summarise them every Friday.", providers=["Notion"])
        assert routed.kind == "research", routed
