"""D340 F (BLD1): a plan's knowledge base can be filled from web pages, as the Knowledge screen's "Add pages" fills one.

A knowledge base in a plan took only files already in the Vault, so "build me a support bot on our docs site" could
not carry its base: pages were an op on a base that already existed. Now a plan's knowledge part names its pages, a
sitemap or a site, held to what `add_knowledge_pages` takes (its own request, one place), a sitemap or a site capped
by Knowledge's crawl limit, said on the card; the save makes the base, then adds the pages through that same tool.
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.native_builder.build_save import save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_kb_pages", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return client


def _plan(**part) -> dict:
    return {"kind": "create", "title": "Support bot", "summary": "Answers from our docs site.",
            "steps": [{"id": "find", "type": "retrieve", "name": "Find it", "does": "Searches the docs",
                       "config": {"retrieve": {"kb_ids": ["Docs"], "query": "{{state.user_input}}"}}, "next": "end"}],
            "knowledge": [{"name": "Docs", "purpose": "Our help site", **part}]}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


class TestPages:
    async def test_its_pages_are_added_after_it_is_made_and_the_card_names_them(self, on, run):
        urls = ["https://docs.example.com/start", "https://docs.example.com/billing"]
        found = await _examine(run, _plan(urls=urls))
        assert found.errors == [], found.errors
        calls = save_calls(found.plan, {}, run.shared_state)
        tools = [c["tool"] for c in calls]
        assert tools.index("create_knowledge_base") < tools.index("add_knowledge_pages")
        [pages] = [c for c in calls if c["tool"] == "add_knowledge_pages"]
        assert pages["args"] == {"knowledge_base": "Docs", "urls": urls, "kind": "page"}
        assert ("“Docs”: made with it, in the Vault folder knowledge/docs; it reads https://docs.example.com/start, "
                "https://docs.example.com/billing, when it is made, with its embedding model") in render(found.plan)

    async def test_a_sitemap_or_a_site_is_one_url_and_the_card_says_its_cap(self, on, run):
        found = await _examine(run, _plan(urls=["https://docs.example.com/sitemap.xml"], kind="sitemap"))
        assert found.errors == [] and found.plan.knowledge[0].pages_cap > 0
        assert (f"it reads the pages https://docs.example.com/sitemap.xml lists, up to "
                f"{found.plan.knowledge[0].pages_cap} pages") in render(found.plan)
        found = await _examine(run, _plan(urls=["https://a.example.com", "https://b.example.com"], kind="site"))
        assert any("a site is one URL, its start" in e for e in found.errors)

    async def test_what_the_tool_refuses_goes_back_to_the_planner(self, on, run):
        found = await _examine(run, _plan(urls=["ftp://docs.example.com/start"]))
        assert any(e.startswith("knowledge 'Docs': those pages can't be added:") for e in found.errors)

    async def test_the_save_s_call_adds_them_through_the_knowledge_tool(self, on, run):
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller
        from core.product_tools.roles import Role

        found = await _examine(run, _plan(urls=["https://docs.example.com/start"]))
        caller = Caller(role=Role.BUILDER, via="assistant")
        areas.ensure_loaded()
        calls = save_calls(found.plan, {}, run.shared_state)
        await registry.invoke("create_knowledge_base", caller, next(c for c in calls
                                                                    if c["tool"] == "create_knowledge_base")["args"])
        out = await registry.invoke("add_knowledge_pages", caller,
                                    next(c for c in calls if c["tool"] == "add_knowledge_pages")["args"])
        assert out["knowledge_base"] == "Docs"
