"""A browser agent is offered as the product's own way in only where this workspace may have one (F1643).

F1629 made a page behind a login the product's own browser agent's, planned straight away, wherever a browser is
installed. A host can also gate browser agents by plan (`builder_tools.set_browser_check`): a workspace whose plan has
none was routed to a browser-agent plan that its save then refused. Now the offer asks the host's check too, once, in
the async callers (`lookup.browser_agent_offered`), and passes the answer down: a plan without browser agents keeps
the request on the research route, as with no browser installed. A check that fails or does not answer in time offers
nothing, since an offer that may be refused is not made, and the turn carries on. With no host check, as on a
self-hosted install, the offer is what it was.

Near misses: reading a web page stays the web reader's whatever the check says, and a check that refuses Browser
steps but not browser agents still offers the agent.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core import builder_tools
from core.native_builder import classify, lookup
from core.native_builder.objects import Request

LOGIN = "Log in to my supplier's portal and download this month's invoice."
READING = "Read the pricing page at example.com and list its plans."
PLAN_SAYS = "Browser agents are on a paid plan."


@pytest.fixture
def browser(monkeypatch):
    """Whether a browser is installed here, and the host's check (None: none set). Restored after."""
    world = {"installed": True}
    monkeypatch.setattr(lookup, "_browser_installed", lambda: world["installed"])
    yield world
    builder_tools.set_browser_check(None)


def _check(refuse_kinds=(), *, raises=False, sleeps=0.0, asked=None):
    async def check(kind):
        if asked is not None:
            asked.append(kind)
        if sleeps:
            await asyncio.sleep(sleeps)
        if raises:
            raise RuntimeError("the plan service did not answer")
        return PLAN_SAYS if kind in refuse_kinds else None
    return check


class TestTheOfferAsksThePlan:
    async def test_no_host_check_and_a_browser_offers_it(self, browser):
        assert await lookup.browser_agent_offered() is True

    async def test_no_browser_offers_nothing(self, browser):
        browser["installed"] = False
        assert await lookup.browser_agent_offered() is False

    async def test_a_plan_that_has_browser_agents_offers_it(self, browser):
        asked: list = []
        builder_tools.set_browser_check(_check(asked=asked))
        assert await lookup.browser_agent_offered() is True and asked == ["agent"]

    async def test_a_plan_without_browser_agents_offers_nothing(self, browser):
        builder_tools.set_browser_check(_check(refuse_kinds=("agent",)))
        assert await lookup.browser_agent_offered() is False

    async def test_a_check_that_refuses_only_browser_steps_still_offers_the_agent(self, browser):
        builder_tools.set_browser_check(_check(refuse_kinds=("step",)))
        assert await lookup.browser_agent_offered() is True

    async def test_a_check_that_fails_offers_nothing_and_says_so_in_the_log(self, browser, caplog):
        builder_tools.set_browser_check(_check(raises=True))
        with caplog.at_level(logging.WARNING):
            assert await lookup.browser_agent_offered() is False
        assert any(r.levelno == logging.WARNING for r in caplog.records), caplog.records

    async def test_a_check_that_does_not_answer_in_time_offers_nothing(self, browser, monkeypatch):
        monkeypatch.setattr(lookup, "BROWSER_CHECK_SECONDS", 0.05)
        builder_tools.set_browser_check(_check(sleeps=2.0))
        assert await lookup.browser_agent_offered() is False


class TestTheSentenceFollowsTheAnswer:
    def test_not_offered_is_not_claimed(self, browser):
        assert lookup.own_capability(LOGIN, browser=False) is None

    def test_offered_is_claimed(self, browser):
        said = lookup.own_capability(LOGIN, browser=True)
        assert said and "browser agent" in said

    def test_reading_a_page_is_the_web_readers_whatever_the_plan(self, browser):
        said = lookup.own_capability(READING, browser=False)
        assert said and "web reader" in said


def _override(message: str, *, browser_agent):
    return classify.override(Request(kind="research", needs="none"), message=message,
                             state={"may_change": True, "may_act": True}, inventory={}, connectable=set(),
                             browser_agent=browser_agent)


class TestTheRouterFollowsTheAnswer:
    def test_a_plan_without_browser_agents_keeps_the_research_route(self, browser):
        assert _override(LOGIN, browser_agent=False).kind == "research"

    def test_a_plan_with_them_plans_it(self, browser):
        assert _override(LOGIN, browser_agent=True).kind == "build"

    def test_reading_a_page_is_planned_whatever_the_plan(self, browser):
        assert _override(READING, browser_agent=False).kind == "build"

    async def test_the_classify_asks_the_plan_before_it_routes(self, browser, monkeypatch):
        """`_classified`, the async caller, asks once and passes the answer to the router."""
        from core.orchestration import engine

        builder_tools.set_browser_check(_check(refuse_kinds=("agent",)))
        state = {"user_message": LOGIN, "may_change": True, "may_act": True}
        monkeypatch.setattr(engine, "live_run", lambda run_id: SimpleNamespace(shared_state=state))

        async def nothing(*a, **k):
            return {}

        async def none_connectable(*a, **k):
            return set()

        monkeypatch.setattr(classify, "_inventory", nothing)
        monkeypatch.setattr(classify, "_connectable", none_connectable)
        token = builder_tools._current_run.set("run_f1643_test")
        try:
            await classify._classified(Request(kind="research", needs="none"), [])
        finally:
            builder_tools._current_run.reset(token)
        assert state["intent"] == "research", state["intent"]   # a build would record "create"


class TestTheResearchRouteFollowsTheAnswer:
    async def test_a_plan_without_browser_agents_never_claims_the_workspace_does_it(self, browser, monkeypatch):
        from core.native_builder import build_plan

        state = {"request": {"providers": []}, "user_message": LOGIN, "findings": {"status": "no_report"},
                 "lookup": {"findings": {"goal": "Invoices", "options": []}, "found": [], "left": [],
                            "unmatched": []}}
        monkeypatch.setattr(build_plan, "_state", lambda: state)
        monkeypatch.setattr(lookup.research, "_evidence", lambda: {})
        builder_tools.set_browser_check(_check(refuse_kinds=("agent",)))
        done = await lookup.finish_findings({}, None, [])
        assert done["said"].startswith(lookup.SEEN_NOTHING), done["said"]

    async def test_a_plan_with_them_never_says_nothing_here_does_it(self, browser, monkeypatch):
        from core.native_builder import build_plan

        state = {"request": {"providers": []}, "user_message": LOGIN, "findings": {"status": "no_report"},
                 "lookup": {"findings": {"goal": "Invoices", "options": []}, "found": [], "left": [],
                            "unmatched": []}}
        monkeypatch.setattr(build_plan, "_state", lambda: state)
        monkeypatch.setattr(lookup.research, "_evidence", lambda: {})
        builder_tools.set_browser_check(_check())
        done = await lookup.finish_findings({}, None, [])
        assert not done["said"].startswith(lookup.SEEN_NOTHING), done["said"]
