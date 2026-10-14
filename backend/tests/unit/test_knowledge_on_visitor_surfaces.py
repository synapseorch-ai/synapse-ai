"""Knowledge where nobody can see a citation: the widget and messaging channels (B1b, D161).

Neither surface can draw Sources, so an answer reaches a visitor or a channel
without its `[K#]` labels (Naveen, D161); the run log and the member's chat
keep them. An owner can let a widget's visitors search only bases opened to
visitors, so the tools appear in the editor's list only then (D153 #12).
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from _fakes.knowledge_corpus import CONTRACTS, REPORTS, save_bases
from core.chat_widgets import limits as _limits


@pytest.fixture
def no_other_tools(monkeypatch):
    import core.tools as tools_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        return [], {}, [], ""

    monkeypatch.setattr(tools_mod, "aggregate_all_tools", aggregated)


async def _on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200


class TestWhatAnOwnerCanTick:
    async def _listed(self, client, agent_id):
        response = await client.get("/api/widgets/visitor-tools", params={"agent_id": agent_id})
        assert response.status_code == 200, response.text
        return response.json()["tools"]

    async def test_offered_when_a_base_is_open_to_visitors(self, client, no_other_tools):
        await _on(client)
        await save_bases(CONTRACTS.model_copy(update={"visitor_access": True}), REPORTS)
        agent = S.make_agent(knowledge=[CONTRACTS.id, REPORTS.id])
        await S.seed_agents([agent])
        assert await self._listed(client, agent["id"]) == [
            {"name": "read_knowledge", "category": "read_only"},
            {"name": "search_knowledge", "category": "read_only"},
        ]

    async def test_not_without_a_base_open_to_visitors(self, client, no_other_tools):
        await _on(client)
        await save_bases(CONTRACTS, REPORTS)
        agent = S.make_agent(knowledge=[CONTRACTS.id, REPORTS.id])
        await S.seed_agents([agent])
        assert await self._listed(client, agent["id"]) == []

    async def test_not_while_knowledge_is_off(self, client, no_other_tools):
        await save_bases(CONTRACTS.model_copy(update={"visitor_access": True}))
        agent = S.make_agent(knowledge=[CONTRACTS.id])
        await S.seed_agents([agent])
        assert await self._listed(client, agent["id"]) == []

    async def test_through_a_workflow_s_agent_step(self, client, no_other_tools, seed_orchestration):
        await _on(client)
        await save_bases(CONTRACTS.model_copy(update={"visitor_access": True}))
        worker = S.make_agent(knowledge=[CONTRACTS.id])
        orch = await seed_orchestration(entry_step_id="a", steps=[
            {"id": "a", "name": "A", "type": "agent", "agent_id": worker["id"], "next_step_id": None}])
        lead = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([worker, lead])
        assert [t["name"] for t in await self._listed(client, lead["id"])] == ["read_knowledge", "search_knowledge"]


class TestTheWidget:
    def test_an_answer_reaches_a_visitor_without_labels(self):
        from core.routes.widget import _visitor_view

        for kind, key in (("final", "response"), ("response", "content")):
            shown, ends = _visitor_view({"type": kind, key: "Hosting is 1,200 a month [K1][K2]."})
            assert shown == [{"type": kind, "content": "Hosting is 1,200 a month."}] and not ends

    async def test_the_history_without_labels(self, client, seed_agent):
        from core.session import _save_conversation_turn

        _limits.reset()
        agent = await seed_agent()
        widget = (await client.post("/api/widgets", json={
            "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})).json()
        token = (await client.post("/api/widget/session", json={
            "public_key": widget["public_key"], "origin": "https://acme.com"})).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        session_id = (await client.get("/api/widget/history", headers=headers)).json()["session_id"]
        await _save_conversation_turn(session_id=session_id, agent_id=agent["id"], user="what about [K1]?",
                                      assistant="Hosting is 1,200 [K1].")
        messages = (await client.get("/api/widget/history", headers=headers)).json()["messages"]
        assert messages == [{"role": "user", "content": "what about [K1]?"},
                            {"role": "assistant", "content": "Hosting is 1,200."}], "the visitor's words are theirs"
        _limits.reset()


class TestAChannel:
    @pytest.mark.parametrize("orchestrator", [False, True])
    async def test_a_reply_without_labels(self, monkeypatch, orchestrator):
        import core.react_engine as re_mod
        import core.server as server
        from core.messaging.manager import MessagingManager

        async def answer(*args, **kwargs):
            yield {"type": "final", "response": "It renews yearly [K2] ([K1])."}

        monkeypatch.setattr(re_mod, "run_orchestrator_agent" if orchestrator else "run_agent_step", answer)
        agent = S.make_orchestrator_agent("orch_x") if orchestrator else S.make_agent(tools=[])
        await S.seed_agents([agent])
        reply = await MessagingManager(server_module=server).run_agent("when?", agent["id"], "tg_1")
        assert reply == "It renews yearly."
