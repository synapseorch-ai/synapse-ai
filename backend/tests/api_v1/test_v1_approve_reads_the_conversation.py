"""
v1's approve reads the agent from the conversation it answers (F211).

`POST /api/v1/chat` with `agent: "SQL Bot"` stopped on a tool approval; the
approve with only `approval_id` said "This conversation is not waiting on an
approval." — it looked under the workspace's first agent, not SQL Bot. The
session and the call already name the agent, as cloud's approve reads them
(`api/public/v1.py::chat_approve`). Naming the agent still works.
"""
from __future__ import annotations

import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events

from tests.api_v1.test_v1_cli_surface import APPROVAL


async def _park(client, api_key, agent_id, monkeypatch, session="f211_s", approval_id="appr-1"):
    import core.react_engine as re
    monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.status(), {**APPROVAL, "approval_id": approval_id}]))
    resp = await client.post("/api/v1/chat/stream", json={
        "message": "email them", "agent": agent_id, "session_id": session,
    }, headers=api_key["headers"])
    assert any(e["type"] == "awaiting_approval" for e in data_events(resp.text.split("\n\n")))


@pytest.fixture
async def agents():
    """Two agents; the one the conversation used is not the workspace's first."""
    from _fakes import seed as S
    first = S.make_agent(id="agent_a_first", name="Assistant")
    sql = S.make_agent(id="agent_sql", name="SQL Bot")
    await S.seed_agents([first, sql])  # one write: seeding replaces the list
    from core.routes.api_v1 import _resolve_agent_for_api
    assert (await _resolve_agent_for_api(None))["id"] == "agent_a_first"
    return first, sql


@pytest.fixture
def resumed(monkeypatch):
    import core.react_engine as re
    seen = {}

    async def _loop(request, *args, **kwargs):
        seen["agent_id"] = request.agent_id
        seen["resume_state"] = kwargs.get("resume_state")
        yield {"type": "final", "response": "sent it"}

    def arm():
        monkeypatch.setattr(re, "run_react_loop", _loop)
    return seen, arm


class TestTheConversationNamesTheAgent:
    async def test_the_approval_id_alone_is_enough(self, client, api_key, agents, monkeypatch, resumed):
        _, sql = agents
        await _park(client, api_key, sql["id"], monkeypatch)
        seen, arm = resumed
        arm()
        resp = await client.post("/api/v1/chat/f211_s/approve",
                                 json={"approve": True, "approval_id": "appr-1"},
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert {"type": "response", "content": "sent it", "intent": "chat",
                "session_id": "f211_s"} in data_events(resp.text.split("\n\n"))
        assert seen["agent_id"] == "agent_sql" and seen["resume_state"] is not None

    async def test_so_is_nothing_but_the_decision(self, client, api_key, agents, monkeypatch, resumed):
        _, sql = agents
        await _park(client, api_key, sql["id"], monkeypatch)
        seen, arm = resumed
        arm()
        resp = await client.post("/api/v1/chat/f211_s/approve", json={"approve": False},
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert seen["agent_id"] == "agent_sql"

    async def test_a_second_answer_is_still_refused(self, client, api_key, agents, monkeypatch, resumed):
        """F139 holds: an approval is spent once."""
        _, sql = agents
        await _park(client, api_key, sql["id"], monkeypatch)
        _, arm = resumed
        arm()
        body = {"approve": True, "approval_id": "appr-1"}
        assert (await client.post("/api/v1/chat/f211_s/approve", json=body,
                                  headers=api_key["headers"])).status_code == 200
        again = await client.post("/api/v1/chat/f211_s/approve", json=body, headers=api_key["headers"])
        assert again.status_code == 409


class TestTwoAgentsWaitingInOneConversation:
    async def test_without_a_name_or_an_id_it_asks_which(self, client, api_key, agents, monkeypatch):
        first, sql = agents
        await _park(client, api_key, first["id"], monkeypatch, approval_id="appr-a")
        await _park(client, api_key, sql["id"], monkeypatch, approval_id="appr-b")
        resp = await client.post("/api/v1/chat/f211_s/approve", json={"approve": True},
                                 headers=api_key["headers"])
        assert resp.status_code == 409
        assert "more than one" in resp.json()["detail"]

    async def test_the_id_picks_the_one(self, client, api_key, agents, monkeypatch, resumed):
        first, sql = agents
        await _park(client, api_key, first["id"], monkeypatch, approval_id="appr-a")
        await _park(client, api_key, sql["id"], monkeypatch, approval_id="appr-b")
        seen, arm = resumed
        arm()
        resp = await client.post("/api/v1/chat/f211_s/approve",
                                 json={"approve": True, "approval_id": "appr-b"},
                                 headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert seen["agent_id"] == "agent_sql"
        from core.store import sessions as store
        assert await store.get_pending_approval("f211_s", "agent_a_first") is not None, "the other still waits"


class TestNamingTheAgentStillWorks:
    async def test_a_named_agent_is_used(self, client, api_key, agents, monkeypatch, resumed):
        _, sql = agents
        await _park(client, api_key, sql["id"], monkeypatch)
        seen, arm = resumed
        arm()
        resp = await client.post("/api/v1/chat/f211_s/approve",
                                 json={"approve": True, "agent": "SQL Bot"}, headers=api_key["headers"])
        assert resp.status_code == 200, resp.text
        assert seen["agent_id"] == "agent_sql"

    async def test_an_unknown_name_is_a_404(self, client, api_key, agents, monkeypatch):
        _, sql = agents
        await _park(client, api_key, sql["id"], monkeypatch)
        resp = await client.post("/api/v1/chat/f211_s/approve",
                                 json={"approve": True, "agent": "Nobody"}, headers=api_key["headers"])
        assert resp.status_code == 404

    async def test_nothing_waiting_is_still_a_409(self, client, api_key, agents):
        resp = await client.post("/api/v1/chat/f211_none/approve", json={"approve": True},
                                 headers=api_key["headers"])
        assert resp.status_code == 409
        assert resp.json()["detail"] == "This conversation is not waiting on an approval."
