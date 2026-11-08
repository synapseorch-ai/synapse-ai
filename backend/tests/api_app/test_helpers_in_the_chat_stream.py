"""The app's chat stream carries what a helper does, and keeps whose work is whose (SUB1a, D279; F547).

The chat route rebuilds each event it forwards and drops what it does not list. So a
helper's events — had the engine sent any — would never have reached the page; and of
what it did send, `tool_result` lost `subtask_name` (the page's "· helper name" suffix
worked only in scale mode, which forwards the raw event), and `llm_thought` and `status`
lost `delegate_from`, so a delegate's thoughts arrived as its lead's own (F547).
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from core.tools import VirtualTool


def _data(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, None)


@pytest.fixture
def tools(monkeypatch):
    import core.react_engine as re_mod
    import core.server as server

    async def aggregated(server_module, agent, custom_tools, **_):
        wanted = agent.get("tools") or []
        held = [LOOKUP] if ("all" in wanted or "lookup" in wanted) else []
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    monkeypatch.setitem(server._session_tools, "_test", [])


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


def spawn(task, tools, **args):
    return json.dumps({"tool": "spawn_subtask", "arguments": {"task": task, "tools": tools, **args}})


class TestAHelperInTheChatStream:
    async def test_it_starts_works_and_finishes_on_the_page(self, client, seed_agent, tools, llm):
        agent = await seed_agent(tools=["spawn_subtask", "lookup"], skip_default_tools=True)
        llm.on("PLAN IT", [spawn("CHECK A", [], name="Check A"), "all done"])
        llm.on("CHECK A", ["A is fine"])
        reply = await client.post("/chat/stream", json={
            "message": "PLAN IT", "session_id": "s_stream_helper", "agent_id": agent["id"]})
        frames = _data(reply.text)
        kinds = [f["type"] for f in frames]
        assert "helper_started" in kinds and "helper_finished" in kinds
        finished = [f for f in frames if f["type"] == "helper_finished"][0]
        assert finished["record"]["answer"] == "A is fine" and finished["helper_name"] == "Check A"
        result = [f for f in frames if f["type"] == "tool_result" and f["tool_name"] == "spawn_subtask"][0]
        assert result["subtask_name"] == "Check A"
        assert [f for f in frames if f["type"] == "response"][0]["content"] == "all done"

    async def test_a_reopened_conversation_draws_its_lane(self, client, seed_agent, tools, llm):
        agent = await seed_agent(tools=["spawn_subtask", "lookup"], skip_default_tools=True)
        llm.on("PLAN IT", [spawn("CHECK A", [], name="Check A"), "all done"])
        llm.on("CHECK A", ["A is fine"])
        await client.post("/chat/stream", json={
            "message": "PLAN IT", "session_id": "s_reopen_helper", "agent_id": agent["id"]})
        history = (await client.get(f"/api/sessions/s_reopen_helper/history?agent_id={agent['id']}")).json()
        [kept] = history["turns"][0]["helpers"]
        assert kept["name"] == "Check A" and kept["answer"] == "A is fine"


class TestADelegatesWorkIsTaggedAsItsOwn:
    async def test_its_thoughts_arrive_tagged(self, client, tools, llm):
        """F547: `llm_thought` was rebuilt without `delegate_from`, so the page filed it as the lead's."""
        member = S.make_agent(id="member_f547", name="Member", tools=[], skip_default_tools=True)
        lead = S.make_agent(id="lead_f547", name="Lead", type="delegate", tools=[], skip_default_tools=True,
                            delegate_agent_ids=[member["id"]])
        await S.seed_agents([member, lead])
        llm.on("LEAD ASK", [tool_call("delegate_to_agent", agent_id=member["id"], task="MEMBER WORK"), "led"])
        llm.on("MEMBER WORK", ["the member's own thought, then its answer"])
        reply = await client.post("/chat/stream", json={
            "message": "LEAD ASK", "session_id": "s_f547", "agent_id": lead["id"]})
        thoughts = [f for f in _data(reply.text) if f["type"] == "llm_thought"]
        members = [f for f in thoughts if "member's own" in f["thought"]]
        assert members and all(f.get("delegate_from") == lead["id"] for f in members)
        assert members[0].get("delegate_agent_name") == "Member"
        leads = [f for f in thoughts if "member's own" not in f["thought"]]
        assert leads and not any(f.get("delegate_from") for f in leads)
