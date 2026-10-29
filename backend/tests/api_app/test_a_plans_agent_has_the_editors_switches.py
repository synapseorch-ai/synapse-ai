"""F1223 (BLD1; CAP3 stage 1, P1): a plan's new agent has the agent editor's switches, held to the same checks.

A plan's agent had a name, a purpose, tools and what its model needs, and nothing else: no memory, no knowledge base,
no Browser settings or kept session, no MCP server. So brief 3's labeller remembered nothing and brief 5's Browser
agent stopped at the portal's sign-in. Now a plan's agent says `memory`, `knowledge` (bases by name), `browser`
(sites, the sites it starts signed in on, its mode) and `servers` (every tool a connected server has now). Code holds
each to what its save checks: one knowledge check at every agent door (`core.agent_knowledge`, the app's
`POST /api/agents` too, where only a base *added* is asked about), the plan's memory and Browser allowance, and the
kept sessions there are: a site with none is said on the card, and the agent is built without it (Naveen, 10-29).
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import agent_knowledge, builder_access, builder_guard, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, objects as O
from core.native_builder.build_save import save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router = {"tracker__list_tickets": object(), "tracker__label_ticket": object()}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_switches", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()
    builder_tools.set_memory_check(None)


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return client


async def _kb(client, name="Help docs", folder="help"):
    resp = await client.post("/api/knowledge/bases", json={"name": name, "folder": folder})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _turn_off(client, kb):
    resp = await client.patch(f"/api/knowledge/bases/{kb['id']}", json={"revision": kb["revision"],
                                                                         "status": "disabled"})
    assert resp.status_code == 200, resp.text


def _agent(**more):
    return S.make_agent(id="agent_help", name="Help desk", **more)


# ── one knowledge check, at every door ────────────────────────────────────────────────────────────────────────────

class TestTheKnowledgeCheck:
    async def test_knowledge_off_is_said_with_its_switch(self, client):
        assert "turn it on in Settings › General › Knowledge" in await agent_knowledge.refusal(["kb_x"])

    async def test_a_base_that_is_not_here_or_is_off_is_refused(self, on):
        kb = await _kb(on)
        assert await agent_knowledge.refusal([kb["id"]]) is None
        assert "no knowledge base 'kb_gone'" in await agent_knowledge.refusal([kb["id"], "kb_gone"])
        await _turn_off(on, kb)
        assert "is turned off" in await agent_knowledge.refusal([kb["id"]])

    async def test_only_a_base_added_is_asked_about(self, on):
        kb = await _kb(on)
        await _turn_off(on, kb)
        assert await agent_knowledge.refusal([kb["id"], "kb_gone"], before=[kb["id"], "kb_gone"]) is None

    async def test_more_than_ten_is_refused_before_any_read(self, client):
        assert "at most 10" in await agent_knowledge.refusal([f"kb_{i}" for i in range(11)])


class TestTheAppsDoor:
    async def test_post_agents_refuses_a_base_that_is_not_here(self, on):
        resp = await on.post("/api/agents", json=_agent(knowledge=["kb_gone"]))
        assert resp.status_code == 422 and "no knowledge base 'kb_gone'" in resp.json()["detail"]

    async def test_an_agent_whose_base_went_since_still_saves_unchanged(self, on):
        # 8a's pin: a stricter door never breaks an agent saved before it.
        kb = await _kb(on)
        assert (await on.post("/api/agents", json=_agent(knowledge=[kb["id"], "kb_old"]))).status_code == 422
        await S.seed_agents([_agent(knowledge=[kb["id"], "kb_old"])])
        await _turn_off(on, kb)
        resp = await on.post("/api/agents", json=_agent(knowledge=[kb["id"], "kb_old"], description="Renamed"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["knowledge"] == [kb["id"], "kb_old"]


class TestTheBuildersDoor:
    async def _call(self, run, tool, args):
        with live(run):
            raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
        return json.loads(raw) if isinstance(raw, str) else raw

    async def test_create_agent_gives_it_a_base(self, on, run):
        kb = await _kb(on)
        out = await self._call(run, "create_agent", {"name": "Help desk", "type": "conversational", "tools": [],
                                                     "knowledge": [kb["id"], kb["id"]]})
        assert out["agent"]["knowledge"] == [kb["id"]]

    async def test_create_agent_refuses_a_base_that_is_not_here(self, on, run):
        out = await self._call(run, "create_agent", {"name": "Help desk", "type": "conversational", "tools": [],
                                                     "knowledge": ["kb_gone"]})
        assert "no knowledge base 'kb_gone'" in out["error"]

    async def test_update_agent_asks_only_about_a_base_it_adds(self, on, run):
        kb = await _kb(on)
        await S.seed_agents([_agent(knowledge=["kb_old"])])
        out = await self._call(run, "update_agent", {"agent_id": "agent_help",
                                                     "fields": {"knowledge": ["kb_old", kb["id"]]}})
        assert out["agent"]["knowledge"] == ["kb_old", kb["id"]]
        out = await self._call(run, "update_agent", {"agent_id": "agent_help",
                                                     "fields": {"knowledge": ["kb_old", "kb_new_gone"]}})
        assert "no knowledge base 'kb_new_gone'" in out["error"]


# ── the plan's agent ──────────────────────────────────────────────────────────────────────────────────────────────

def _plan(agent: dict) -> dict:
    return {"kind": "create", "title": "Answer from the help docs", "summary": "Answers questions from the docs.",
            "steps": [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers the question",
                       "agent": agent["name"], "prompt": "Answer {state.input}.", "next": "end"}],
            "agents": [{"purpose": "Answers from the docs", **agent}]}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


class TestThePlansAgent:
    async def test_its_switches_reach_the_card_and_the_save(self, on, run, monkeypatch):
        async def as_run_calls(names, server_module):
            return [n.partition("__")[2] or n for n in names]

        monkeypatch.setattr(builder_guard, "canonical_names", as_run_calls)
        kb = await _kb(on)
        found = await _examine(run, _plan({"name": "Help desk", "memory": True, "knowledge": ["help DOCS"],
                                           "servers": ["Tracker"]}))
        assert found.errors == [] and found.plan is not None
        [agent] = found.plan.agents
        assert agent.knowledge == ["Help docs"] and found.plan.knowledge_ids == {"Help docs": kb["id"]}
        assert {"list_tickets", "label_ticket"} <= set(agent.tools)
        card = render(found.plan)
        assert "  - Memory: on" in card and "  - Knowledge: answers from “Help docs”" in card
        assert "MCP server Tracker: its tools as of now; tools it adds later are not added" in card
        [call] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_agent"]
        assert call["args"]["memory"] is True and call["args"]["knowledge"] == [kb["id"]]

    async def test_a_base_or_server_that_is_not_here_goes_back_to_the_planner(self, on, run):
        found = await _examine(run, _plan({"name": "Help desk", "knowledge": ["Nowhere"], "servers": ["Ghost"]}))
        assert any("there is no knowledge base 'Nowhere' here" in e for e in found.errors)
        assert any("'Ghost' is no MCP server connected here; connected: tracker" in e for e in found.errors)

    async def test_knowledge_off_is_the_feature_s_card(self, client, run):
        found = await _examine(run, _plan({"name": "Help desk", "knowledge": ["Help docs"]}))
        assert "knowledge" in found.features

    async def test_a_site_with_no_kept_session_is_said_and_built_without(self, client, run):
        found = await _examine(run, _plan({"name": "Portal clerk", "type": "browser", "browser": {
            "sites": ["portal.example.com"], "sessions": ["portal.example.com"]}}))
        [agent] = found.plan.agents
        assert agent.browser.sessions_missing == ["portal.example.com"]
        assert "No kept session for portal.example.com yet: sign in to it under Agents › Browser › Sessions" \
            in render(found.plan)
        [call] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_agent"]
        assert call["args"]["type"] == "browser" and call["args"]["browser"]["sessions"] == []
        assert call["args"]["browser"]["sites"] == ["portal.example.com"]

    async def test_browser_settings_on_an_agent_that_is_no_browser_agent_go_back(self, client, run):
        found = await _examine(run, _plan({"name": "Help desk", "browser": {"sites": ["a.example.com"]}}))
        assert any("only a browser agent has browser settings" in e for e in found.errors)

    async def test_memory_over_the_plan_s_allowance_is_the_plan_s_room(self, client, run):
        async def full(adding, agent_id):
            return "Your plan keeps memory for 2 agents; turn it off on one first."

        builder_tools.set_memory_check(full)
        found = await _examine(run, _plan({"name": "Help desk", "memory": True}))
        assert found.plan.room == "Your plan keeps memory for 2 agents; turn it off on one first."

    async def test_a_model_never_writes_what_code_fills(self, client, run):
        plan = O.Plan.from_draft(_plan({"name": "Help desk"}) | {"knowledge_ids": {"x": "kb_x"}})
        assert plan.knowledge_ids == {}
