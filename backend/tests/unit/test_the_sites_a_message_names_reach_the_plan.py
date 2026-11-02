"""The sites a message names reach the planner, read once a conversation, and the person's word comes first (D340 D2's
follow-up; Naveen 10-31: the plan as restated, and "sometimes the read might be wrong so user can answer with not right
or different or give something else .. we should be flexible to take that too").

B built the read (`named_sites.read_named`: the guarded GET, typed facts only, on the plan's web budget); nothing called
it, so the planner still planned blind. Now gather reads the sites the person's message names into the pack's
`named_sites` and keeps each in the run's state (`named_pages`), which the chat route carries to the next run: a re-plan
or the next turn never fetches a reached site again. A site that could not be read is tried again when named again,
and what the person says about a site outranks what was read — the planner is told so.
"""
from __future__ import annotations

import inspect

import pytest

import core.builder_tools  # noqa: F401 — loads the builder's modules in the product's order
from core.native_builder import build_plan, named_sites
from core.native_builder.seed import bundled_definitions

SITE = "https://acme.example"


def _page(url, reached=True, **facts):
    return {"url": url, "reached": reached, "final_url": url if reached else "", "status": 200 if reached else 0,
            "title": facts.get("title", "Acme" if reached else ""), "what": facts.get("what", ""),
            "kind": facts.get("kind", "site" if reached else ""), "same_site_links": 9, "api_spec": None,
            "why_not": facts.get("why_not", ""), "redirects": []}


@pytest.fixture
def reads(monkeypatch):
    """Each URL `named_sites.read` was asked for; a URL in `fail` is not reached."""
    asked: list[str] = []
    fail: set[str] = set()

    async def read(url):
        asked.append(url)
        page = named_sites.NamedPage(url=url)
        if url in fail:
            page.why_not = "it answered 503"
            return page
        page.reached, page.final_url, page.title, page.kind, page.what = True, url, "Acme", "site", "Tools for teams"
        return page

    monkeypatch.setattr(named_sites, "read", read)
    return asked, fail


class TestGatherReadsThem:
    async def test_a_named_site_reaches_the_pack_as_facts_and_is_kept(self, reads):
        state = {"user_message": f"Scrape the blog on {SITE} every Monday"}
        sites = await build_plan._named_sites(state)
        assert sites == [{"url": SITE, "final_url": SITE, "title": "Acme", "what": "Tools for teams", "kind": "site"}]
        assert reads[0] == [SITE] and state[named_sites.PAGES_KEY][SITE]["reached"] is True

    async def test_a_reached_site_is_never_read_again_in_the_conversation(self, reads):
        state = {"user_message": f"Use {SITE} again", named_sites.PAGES_KEY: {SITE: _page(SITE, title="Kept")}}
        sites = await build_plan._named_sites(state)
        assert reads[0] == [] and sites[0]["title"] == "Kept", "the page as first read: it never changes the plan"

    async def test_one_that_could_not_be_read_is_said_so_and_tried_again_when_named_again(self, reads):
        asked, fail = reads
        fail.add(SITE)
        state = {"user_message": f"Watch {SITE}"}
        [site] = await build_plan._named_sites(state)
        assert site == {"url": SITE, "why_not": "it answered 503"}
        fail.clear()
        state["user_message"] = f"It's up now: {SITE}"
        [site] = await build_plan._named_sites(state)
        assert asked == [SITE, SITE] and site["title"] == "Acme"
        assert state[named_sites.PAGES_KEY][SITE]["reached"] is True

    async def test_another_address_the_person_gives_is_read(self, reads):
        state = {"user_message": "No, the right one is https://blog.acme.example",
                 named_sites.PAGES_KEY: {SITE: _page(SITE)}}
        await build_plan._named_sites(state)
        assert reads[0] == ["https://blog.acme.example"]
        assert set(state[named_sites.PAGES_KEY]) == {SITE, "https://blog.acme.example"}, "the earlier read is kept"

    async def test_a_message_that_names_none_fetches_nothing(self, reads):
        state = {"user_message": "Email me a summary of yesterday's runs"}
        assert await build_plan._named_sites(state) == [] and reads[0] == []
        assert named_sites.PAGES_KEY not in state

    async def test_a_read_that_raises_is_a_pack_without_sites_never_a_failed_gather(self, monkeypatch):
        async def boom(text, *, already=None):
            raise RuntimeError("network")

        monkeypatch.setattr(named_sites, "read_named", boom)
        assert await build_plan._named_sites({"user_message": f"Read {SITE}"}) == []

    async def test_the_pack_carries_them(self, reads, monkeypatch):
        state = {"user_message": f"Summarise {SITE} weekly"}
        monkeypatch.setattr(build_plan, "_state", lambda: state)
        pack = await build_plan.read_plan_context({}, type("S", (), {"agent_sessions": {}, "tool_router": {}})(), [])
        assert pack["named_sites"][0]["url"] == SITE

    async def test_research_reuses_the_conversations_reads(self, reads, monkeypatch):
        from core.native_builder import research_service as R

        state = {named_sites.PAGES_KEY: {SITE: _page(SITE)},
                 "plan_draft": {"questions": [{"kind": "public", "ask": "What does it hold?", "why": "",
                                               "url": SITE}]}}
        monkeypatch.setattr(R, "_state", lambda: state)
        out = await R.answer_research({}, None, [])
        assert reads[0] == [] and out["answers"][0]["sure"] is True


class TestTheConversationKeepsThem:
    def test_the_previous_runs_reads_seed_the_next(self):
        previous = {named_sites.PAGES_KEY: {SITE: _page(SITE), "junk": "not a page"}}
        assert named_sites.carried(previous) == {SITE: _page(SITE)}
        assert named_sites.carried({}) == {} and named_sites.carried(None) == {}

    def test_the_chat_route_seeds_them(self):
        from core.routes import builder

        assert "named_sites.PAGES_KEY: named_sites.carried(previous)" in inspect.getsource(builder)

    def test_the_state_declares_them(self):
        schema = bundled_definitions()["orchestration"]["state_schema"]
        assert schema[named_sites.PAGES_KEY]["type"] == "dict"

    def test_only_a_reached_read_is_settled(self):
        assert named_sites.settled({SITE: _page(SITE), "https://b.example": _page("https://b.example", False)}) == {
            SITE: _page(SITE)}


class TestThePersonsWordComesFirst:
    @pytest.fixture(scope="class")
    def prompt(self):
        return next(a for a in bundled_definitions()["agents"]
                    if a["id"] == "agent_native_builder_planner")["system_prompt"]

    def test_the_planner_is_told_what_the_sites_are(self, prompt):
        assert "the sites the message names, as code read them (`named_sites`" in prompt

    def test_a_correction_outranks_the_read_and_the_research(self, prompt):
        assert "What the person says comes before anything read or found" in prompt
        assert "wrong or different, or give something else, plan from their words" in prompt

    def test_an_address_research_found_is_given_not_guessed(self):
        assert "plan_answers" in build_plan.GIVEN_KEYS and "plan_context" in build_plan.GIVEN_KEYS
