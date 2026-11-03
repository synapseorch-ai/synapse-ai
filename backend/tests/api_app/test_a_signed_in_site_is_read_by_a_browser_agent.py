"""A site the person is signed in to is read by a browser agent with that session (F1300, TRI1; CAP3's tier 1 cap-01,
A1 and A2 and FF14's baseline; Naveen 11-03: build as restated).

"Every morning, check my supplier portal (http://portal.localhost:9411) for new orders and keep a board of what's
waiting": the pack held the person's kept session for localhost (`signed_in_sites`), but no line of the planner's named
it, and # Structure's rule 1 says a fetch is a Tool step, so the plan read the portal with `scrape_url`, as no one. The
board got no orders. Now a step that reads a kept-session site with an anonymous read — a Tool step's page read or GET
tool, or an agent holding one — is said once as a browser agent with that site and session, reading only: matched by
site (`core.sites.same_site`, the rule the sessions use), only when the plan can run a browser agent (the host's
`browser_check`: cloud's Free plan keeps no sessions and gets no fix it can't have), and never where F1301's one finding
already names the agent (`structure.covered_by_browser_agent`). The prompt names `signed_in_sites` and makes the
exception to rule 1, its numbering unchanged.
"""
from __future__ import annotations

import re

import pytest

from core import builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.native_builder import build_plan, objects as O, structure
from core.native_builder.seed import bundled_definitions

PORTAL = "http://portal.localhost:9411"
LOOKS = ["browser_navigate", "browser_snapshot", "browser_find"]


def _plan(steps: list[dict], agents: list[dict] | None = None) -> O.Plan:
    return O.Plan.from_draft({"kind": "create", "title": "Supplier orders", "summary": "Keeps a board of new orders.",
                              "steps": steps, "agents": agents or []})


def _read(sid="read_portal", tool="scrape_url", url=f"{PORTAL}/orders") -> dict:
    return {"id": sid, "type": "tool", "name": "Read the portal", "does": "Reads the portal's orders", "tool": tool,
            "config": {"tool_arguments": {"url": url}}, "next": "end"}


def _ask(agent: str, prompt: str = f"Open {PORTAL}/orders and list the orders with status New.") -> dict:
    return {"id": "read_orders", "type": "agent", "name": "Read Orders", "does": "Reads the portal's orders",
            "agent": agent, "prompt": prompt, "next": "end"}


def _said(found: list[str]) -> list[str]:
    return [f for f in found if "signed-in session" in f]


@pytest.fixture
def kept(monkeypatch):
    from core import site_sessions

    async def listed(caller=None):
        return [{"site": "localhost", "usable": True}, {"site": "example.com", "usable": True},
                {"site": "intranet.test", "usable": False}]

    monkeypatch.setattr(site_sessions, "listed", listed)


class TestASignedInSiteReadAsNoOne:
    async def test_cap_01s_scrape_is_said_as_a_browser_agent_with_the_session(self, client, kept):
        said = _said(await structure.issues(_plan([_read()]), build_plan.Workspace()))
        assert said == [
            "structure: step read_portal reads portal.localhost with `scrape_url`, which reaches it as no one, but the "
            "person keeps a signed-in session for localhost: read it with a browser agent (type browser) with "
            'browser.sites ["portal.localhost"], browser.sessions ["localhost"] and browser.mode read_only, told to '
            "read only"], said

    @pytest.mark.parametrize("tool", ["read_web_page", "scrape_structured", "extract_links"])
    async def test_any_anonymous_read_of_it(self, client, kept, tool):
        assert len(_said(await structure.issues(_plan([_read(tool=tool)]), build_plan.Workspace()))) == 1

    async def test_a_new_agent_reading_it_with_a_page_read(self, client, kept):
        plan = _plan([_ask("Order Reader")], [{"name": "Order Reader", "purpose": "Reads orders",
                                               "tools": ["scrape_url"]}])
        said = _said(await structure.issues(plan, build_plan.Workspace()))
        assert len(said) == 1 and "step read_orders" in said[0] and 'browser.sessions ["localhost"]' in said[0]

    async def test_an_agent_here_reading_it_with_a_page_read(self, client, kept):
        ws = build_plan.Workspace(agents={"agent_r": {"name": "Reader", "type": "conversational",
                                                      "tools": ["scrape_url"]}})
        assert len(_said(await structure.issues(_plan([_ask("Reader")]), ws))) == 1


class TestOtherwiseNothingIsSaid:
    async def test_a_public_page(self, client, kept):
        assert _said(await structure.issues(_plan([_read(url="https://news.example.org/today")]),
                                            build_plan.Workspace())) == []

    async def test_no_kept_session(self, client, monkeypatch):
        from core import site_sessions

        async def none(caller=None):
            return []

        monkeypatch.setattr(site_sessions, "listed", none)
        assert _said(await structure.issues(_plan([_read()]), build_plan.Workspace())) == []

    async def test_a_session_that_is_not_usable(self, client, kept):
        assert _said(await structure.issues(_plan([_read(url="http://intranet.test/orders")]),
                                            build_plan.Workspace())) == []

    async def test_a_plan_that_cannot_run_a_browser_agent(self, client, kept, monkeypatch):
        async def no_browser(kind):
            return "Your plan has no browser for workflows."

        monkeypatch.setattr(builder_tools, "_browser_check", no_browser)
        assert _said(await structure.issues(_plan([_read()]), build_plan.Workspace())) == []

    async def test_a_browser_agent_already(self, client, kept):
        plan = _plan([_ask("Portal")], [{"name": "Portal", "purpose": "Reads orders", "type": "browser",
                                         "browser": {"sites": ["portal.localhost"], "sessions": ["localhost"]}}])
        assert _said(await structure.issues(plan, build_plan.Workspace())) == []


async def test_where_f1301_covers_the_agent_the_person_gets_one_finding(client, kept):
    """The overlap (agreed with BLD1, 79's ask): a new plain agent with the browser's tools on the kept-session site is
    F1301's one finding, naming the site and the session; F1300 says nothing of it."""
    plan = _plan([_ask("Portal Order Checker")],
                 [{"name": "Portal Order Checker", "purpose": "Reads orders", "tools": [*LOOKS, "scrape_url"]}])
    found = [f for f in await structure.issues(plan, build_plan.Workspace()) if "browser" in f]
    assert len(found) == 1, found
    assert 'browser.sites ["portal.localhost"]' in found[0] and 'browser.sessions ["localhost"]' in found[0]
    assert "signed-in session for" not in found[0], "F1301's finding, not F1300's"


class TestThePlannerIsTold:
    @pytest.fixture(scope="class")
    def prompt(self):
        return next(a for a in bundled_definitions()["agents"]
                    if a["id"] == "agent_native_builder_planner")["system_prompt"]

    def test_it_is_given_the_signed_in_sites(self, prompt):
        given = prompt[prompt.index("# What you are given"):prompt.index("# Choose")]
        assert "`signed_in_sites`" in given

    def test_rule_1_has_the_exception_and_the_numbering_stands(self, prompt):
        start = prompt.index("# Structure")
        structure_ = prompt[start:prompt.index("\n# ", start + 1)]
        rule_1 = next(line for line in structure_.splitlines() if line.startswith("1. "))
        assert "`signed_in_sites`" in rule_1 and "browser agent with that kept session" in rule_1
        assert re.findall(r"^(\d+)\. ", structure_, re.M) == ["1", "2", "3", "4", "5"]
