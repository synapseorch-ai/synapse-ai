"""What the chat page needs to draw a Team agent's turn (TEAM1b, D286; 20-agent-teams.md Q1b, Q5).

- The page's answer frame carries the team's summary and its members' records. Before, the stream
  rebuilt `final` as `response` with four keys, so "answered alone" and the members' records never
  reached the page live. A widget visitor still gets the answer's words alone.
- The person can write to the lead while its members work ("To: the lead"), as they already could to
  a member: their words reach the lead's inbox, read before its next model call as theirs.
"""
import json

import pytest

from core.routes import chat as chat_routes
from tests.unit.test_team_runtime import (  # noqa: F401 — fixtures
    _lead_prompts, _turn, assign, llm, server, team)
from _fakes.fake_llm import tool_call

TEAM = {"tasks": [{"card_id": "c1", "title": "CHECK A", "member": "Ana", "member_id": "m_1", "status": "done",
                   "result": "A is fine"}], "tasks_used": 1, "answered_alone": False, "spent_usd": 0.0123}
MEMBERS = [{"id": "m_1", "agent_id": "ana", "name": "Ana", "task": "CHECK A", "card_id": "c1", "model": "fake",
            "status": "completed", "seconds": 4, "calls": [], "answer": "A is fine"}]


@pytest.fixture
def a_team_turn(monkeypatch):
    """The engine answers as a Team agent's lead does: its final carries `team` and `members`."""
    async def loop(*_a, **_kw):
        yield {"type": "final", "response": "All checked.", "team": TEAM, "members": MEMBERS}

    monkeypatch.setattr(chat_routes, "run_react_loop", loop)


def _frames(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines() if line.startswith("data: {")]


class TestTheAnswerFrame:
    async def test_the_page_gets_the_teams_summary_and_members(self, client, seed_agent, a_team_turn):
        agent = await seed_agent()
        stream = await client.post("/chat/stream", json={"message": "hi", "agent_id": agent["id"]})
        (response,) = [f for f in _frames(stream.text) if f["type"] == "response"]
        assert response["content"] == "All checked."
        assert response["team"] == TEAM and response["members"] == MEMBERS

    async def test_a_single_agents_answer_has_neither(self, client, seed_agent, monkeypatch):
        async def loop(*_a, **_kw):
            yield {"type": "final", "response": "Hello."}

        monkeypatch.setattr(chat_routes, "run_react_loop", loop)
        agent = await seed_agent()
        stream = await client.post("/chat/stream", json={"message": "hi", "agent_id": agent["id"]})
        (response,) = [f for f in _frames(stream.text) if f["type"] == "response"]
        assert "team" not in response and "members" not in response

    async def test_a_widget_visitor_gets_the_words_alone(self, client, seed_agent, a_team_turn):
        from core.chat_widgets import limits
        limits.reset()
        agent = await seed_agent()
        widget = (await client.post("/api/widgets", json={
            "name": "Support", "agent_id": agent["id"], "allowed_origins": ["https://acme.com"]})).json()
        token = (await client.post("/api/widget/session", json={
            "public_key": widget["public_key"], "origin": "https://acme.com"})).json()["token"]
        turn = await client.post("/api/widget/chat", json={"message": "hi"},
                                 headers={"Authorization": f"Bearer {token}"})
        assert "All checked." in turn.text
        assert "A is fine" not in turn.text and "answered_alone" not in turn.text
        limits.reset()


class TestThePersonWritesToTheLead:
    async def test_their_words_reach_the_leads_next_call_as_theirs(self, server, llm, team):
        from core import team as team_mod

        def person_writes(_kw):
            t = next(iter(team_mod._LIVE.values()))
            assert t.message_lead_from_person("Count back-orders as open")
            return "A is fine"

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [person_writes])
        await _turn(server)
        assert any("From the person (their own words): Count back-orders as open" in p for p in _lead_prompts(llm))

    async def test_blank_words_are_not_delivered(self, server, llm, team):
        from core import team as team_mod
        seen = []

        def person_writes(_kw):
            seen.append(next(iter(team_mod._LIVE.values())).message_lead_from_person("   "))
            return "A is fine"

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "done"])
        llm.on("CHECK A", [person_writes])
        await _turn(server)
        assert seen == [False]

    async def test_the_route_says_not_delivered_when_no_team_works(self, client):
        res = await client.post("/chat/s_nobody/lead/message", json={"text": "hello"})
        assert res.status_code == 200
        assert res.json() == {"session_id": "s_nobody", "delivered": False}

    async def test_the_route_delivers_to_the_live_teams_lead(self, client, monkeypatch):
        from core import team as team_mod
        got = []

        class Live:
            def message_lead_from_person(self, text):
                got.append(text)
                return True

        monkeypatch.setattr(team_mod, "find", lambda sid: Live() if sid == "s_live" else None)
        res = await client.post("/chat/s_live/lead/message", json={"text": "Count back-orders"})
        assert res.json() == {"session_id": "s_live", "delivered": True} and got == ["Count back-orders"]

    async def test_the_route_refuses_empty_words(self, client):
        res = await client.post("/chat/s_any/lead/message", json={"text": ""})
        assert res.status_code == 422


class TestTheTypesName:
    async def test_a_delegate_is_called_a_team(self, client):
        types = {t["value"]: t for t in (await client.get("/api/agent-types")).json()["types"]}
        assert types["delegate"]["label"] == "Team"
        assert "sub-agent" not in types["delegate"]["description"].lower()
