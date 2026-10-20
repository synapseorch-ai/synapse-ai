"""A research run stops when it has read enough (F765, BWA1, D274).

BWA1's self-hosted measure (2026-10-20, Gemini Flash): the Researcher ignored "keep tool calls few" — 30 web searches,
23 pages and 13 Registry searches for one persona, about 415k tokens, eleven minutes and $0.21; across the batches,
48 web searches in one run. Only its prompt bounded it. Naveen chose a generous backstop that good research does not
reach (the batches' thorough runs: at most 8 searches, 17 pages, 3 Registry searches for a provider and 3 catalog
calls, in at most 11 turns): per research run, 12 web searches, 20 pages, 3 Registry searches per provider and 3
catalog calls. Past it a tool answers that the research has read enough, without searching, and the step's turns are
fewer, so it ends.
"""
from __future__ import annotations

import json

import pytest

import core.builder_research as br
from core.native_builder import bundled_definitions


@pytest.fixture(autouse=True)
def _fresh_evidence():
    br._EVIDENCE.clear()
    yield
    br._EVIDENCE.clear()


@pytest.fixture
def pages(monkeypatch):
    fetched: list[str] = []

    async def scrape(server_module, args):
        fetched.append(args["url"])
        return {"text": "Stripe API documentation", "links": [{"url": "https://docs.stripe.com/api", "text": "API"}]}

    monkeypatch.setattr(br, "_scrape", scrape)
    return fetched


async def test_twelve_web_searches_then_it_has_read_enough(pages):
    for i in range(12):
        got = await br.search_the_web({"query": f"stripe api {i}"}, None, [])
        assert not got.get("enough"), i
    over = await br.search_the_web({"query": "stripe api again"}, None, [])
    assert over["enough"] is True and "report now" in over["note"]
    assert len(pages) == 12, "the thirteenth search is not made"


async def test_twenty_pages_then_it_has_read_enough(pages):
    for i in range(20):
        got = await br.read_web_page({"url": f"https://docs.stripe.com/page{i}"}, None, [])
        assert not got.get("enough"), i
    over = await br.read_web_page({"url": "https://docs.stripe.com/more"}, None, [])
    assert over["enough"] is True and len(pages) == 20


async def test_three_registry_searches_for_each_provider(monkeypatch):
    import core.safe_fetch as sf

    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        return json.dumps({"servers": []}).encode()

    monkeypatch.setattr(sf, "fetch_bytes", fetch)
    for _ in range(3):
        assert not (await br.search_mcp_registry({"provider": "Stripe"}, None, [])).get("enough")
    assert (await br.search_mcp_registry({"provider": "stripe"}, None, []))["enough"] is True
    assert not (await br.search_mcp_registry({"provider": "Shippo"}, None, [])).get("enough"), "per provider"
    assert len(calls) == 4


async def test_three_catalog_calls(monkeypatch):
    import core.builder_connect as bc

    async def options():
        return {"catalog": [{"slug": "stripe", "name": "Stripe"}], "providers": {}, "screens": {}}

    monkeypatch.setattr(bc, "_read_options", options)
    for _ in range(3):
        assert (await br.find_in_catalog({"query": "Stripe"}, None, []))["apps"]
    assert (await br.find_in_catalog({"query": "Stripe"}, None, []))["enough"] is True


async def test_a_reported_research_starts_the_next_one_afresh(pages):
    for i in range(12):
        await br.search_the_web({"query": f"q{i}"}, None, [])
    await br.report_findings({"options": [{"provider": "Stripe", "how": "unclear"}]}, None, [])
    assert not (await br.search_the_web({"query": "a new look-up"}, None, [])).get("enough")


def test_the_research_steps_end_in_fewer_turns():
    steps = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}
    for sid in ("step_research", "step_plan_research"):
        assert 12 <= steps[sid]["max_turns"] <= 14, (sid, steps[sid]["max_turns"])
