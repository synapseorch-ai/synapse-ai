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


class TestALeadThatWaitsPatiently:
    """F622 (TEAM1b's live pass): `wait` said "Something arrived" in the same words every time, so a lead that
    waited for three members' news one by one made three identical calls and the repeat guard (F476) ended its
    turn, before it could pause on a member's question. Each arrival now names who it came from, and counts."""

    async def test_three_waits_that_each_get_news_do_not_end_the_turn(self, server, llm, team):
        import asyncio as _asyncio

        async def ana(_kw):
            await _asyncio.sleep(0.2)
            return "A fine"

        async def ben_says(_kw):
            await _asyncio.sleep(0.6)
            return tool_call("message", to="lead", text="Halfway")

        async def ben_ends(_kw):
            await _asyncio.sleep(0.6)
            return "B fine"

        from tests.unit.test_team_runtime import both
        llm.on("PLAN IT", [both(assign("Ana", "CHECK A"), assign("Ben", "CHECK B")),
                           tool_call("wait", seconds=10), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), "all checked"])
        llm.on("CHECK A", [ana]).on("CHECK B", [ben_says, ben_ends])
        events = await _turn(server)
        waits = [e["preview"] for e in events if e.get("type") == "tool_result" and e.get("tool_name") == "wait"]
        assert len(waits) == 3 and len(set(waits)) == 3, waits
        assert "Ana" in waits[0]
        assert [e for e in events if e.get("type") == "final"][-1]["response"] == "all checked"


class TestAPausedTeamKeepsItsTimes:
    """F623 (TEAM1b's live pass): a team resumed from a pause lost its members' times — the snapshot kept no
    start or end, so every record read 0 s, and the saved turn kept that."""

    async def test_a_member_that_finished_before_the_pause_keeps_its_seconds(self, server, llm, team):
        import asyncio as _asyncio
        from core.chat_approval import parked_payload, resume_state_for
        from tests.unit.test_team_runtime import both

        async def slow(_kw):
            await _asyncio.sleep(0.6)
            return "A fine"

        llm.on("PLAN IT", [both(assign("Ben", "CHANGE B"), assign("Ana", "CHECK A")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=9), "unused"])
        async def ben_asks(_kw):
            await _asyncio.sleep(0.6)
            return tool_call("danger")

        llm.on("CHANGE B", [ben_asks, "changed"]).on("CHECK A", [slow])
        first = await _turn(server, progress_events=True)
        before = {f["member_name"]: f["seconds"] for f in first if f.get("type") == "member_finished"}
        assert before["Ana"] >= 0.5
        card = [e for e in first if e.get("type") == "human_input_required"][-1]
        snap = {k: v for k, v in [e for e in first if e.get("type") == "_step_progress"][-1].items() if k != "type"}
        parked = parked_payload(card, message="PLAN IT", progress=snap)

        llm._scripts.clear()
        llm.on("PLAN IT", [tool_call("wait", seconds=10), "all changed"]).on("CHANGE B", ["changed"])
        events = await _turn(server, progress_events=True, resume_state=resume_state_for(parked, approve=True))
        after = {m["name"]: m["seconds"] for m in [e for e in events if e.get("type") == "final"][-1]["members"]}
        assert after["Ana"] == before["Ana"]
        # The one that waited on the person counts the work it did before the pause, not only after it, and
        # not the time it waited.
        assert 0.6 <= after["Ben"] < 3

    async def test_a_member_answered_while_the_team_works_keeps_its_seconds(self, server, llm, team):
        """The same for an answer given while the others work (TEAM1c's live answer): the member carried on,
        but its time restarted at the answer (measured: the clerk read 0.8 s for about 2.5 s of work)."""
        import asyncio as _asyncio
        from core import team as team_mod
        from tests.unit.test_team_runtime import both

        async def ben_asks(_kw):
            await _asyncio.sleep(1.0)
            return tool_call("danger")

        async def ana_answers_ben(_kw):
            await _asyncio.sleep(1.8)
            t = next(iter(team_mod._LIVE.values()))
            ben = next(m for m in t.waiting() if m.name == "Ben")
            assert t.answer(ben.question_id, approve=True)
            await _asyncio.sleep(0.3)
            return "A fine"

        llm.on("PLAN IT", [both(assign("Ben", "CHANGE B"), assign("Ana", "CHECK A")), tool_call("wait", seconds=10),
                           tool_call("wait", seconds=10), tool_call("wait", seconds=10), "done"])
        llm.on("CHANGE B", [ben_asks, "changed"]).on("CHECK A", [ana_answers_ben])
        events = await _turn(server, progress_events=True)
        ben = next(f for f in events if f.get("type") == "member_finished" and f["member_name"] == "Ben")
        assert ben["record"]["calls"][-1]["tool"] == "danger" and ben["status"] == "completed"
        # It worked a second before it asked; the 0.8 s it waited on the person is not work.
        assert 1.0 <= ben["seconds"] < 1.6, ben["seconds"]
