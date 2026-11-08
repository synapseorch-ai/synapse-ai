"""What chat tabs read about conversations they don't stream (TABS1, D319; F925, F926).

Only the tab in front streams: a browser allows about six connections per host
on plain HTTP, so ten open tabs can't each hold one. The others learn whether
their conversation is working, waiting on the person, or has moved on from one
short request for all of them (`POST /api/sessions/status`), and History's
search asks the server, so it never misses a conversation the list didn't load.
"""
from __future__ import annotations

from core.session import _save_conversation_turn
from core.store import sessions as store
from tests.api_app.test_chat_turn_routes import _turn_in_a_model_call, chat_agent  # noqa: F401 — fixture


async def _status(client, *pairs: tuple[str, str]) -> dict[str, dict]:
    response = await client.post("/api/sessions/status", json={
        "sessions": [{"session_id": sid, "agent_id": aid} for sid, aid in pairs],
    })
    assert response.status_code == 200, response.text
    return {row["session_id"]: row for row in response.json()["sessions"]}


class TestOneRequestSaysHowEveryTabStands:
    async def test_a_running_turn_reads_as_running(self, client, live_server, gated_llm, chat_agent):
        async with _turn_in_a_model_call(live_server, gated_llm, "tab-run", chat_agent["id"]):
            rows = await _status(client, ("tab-run", chat_agent["id"]))
        assert rows["tab-run"]["running"] is True
        assert rows["tab-run"]["automated"] is False
        assert rows["tab-run"]["pending_approval"] is False

    async def test_a_turn_under_another_agent_is_not_this_tabs(self, client, live_server, gated_llm, chat_agent):
        # Before tabs one session id carried every agent's conversation; a tab is one of them.
        async with _turn_in_a_model_call(live_server, gated_llm, "tab-shared", chat_agent["id"]):
            rows = await _status(client, ("tab-shared", "agent_someone_else"))
        assert rows["tab-shared"]["running"] is False

    async def test_a_parked_question_reads_as_needing_the_person(self, client):
        await store.set_pending_approval("tab-ask", "agent_a", {
            "tool_name": "send_email", "tool_args": {}, "approval_id": "x1", "message": "send it",
        })
        rows = await _status(client, ("tab-ask", "agent_a"))
        assert rows["tab-ask"]["pending_approval"] is True
        assert rows["tab-ask"]["running"] is False

    async def test_a_finished_conversation_says_when_it_last_moved_and_how_long_it_is(self, client):
        await _save_conversation_turn("tab-done", "agent_a", "q1", "a1")
        await _save_conversation_turn("tab-done", "agent_a", "q2", "a2")
        row = (await _status(client, ("tab-done", "agent_a")))["tab-done"]
        assert row == {
            "session_id": "tab-done", "agent_id": "agent_a", "running": False, "automated": False,
            "pending_approval": False, "turn_count": 2, "last_updated": row["last_updated"],
        }
        assert row["last_updated"]

    async def test_a_tab_whose_conversation_has_no_row_yet_is_answered_not_dropped(self, client):
        # A new tab's first turn is saved when it ends; until then the tab still gets a row.
        row = (await _status(client, ("tab-new", "agent_a")))["tab-new"]
        assert row["running"] is False and row["turn_count"] == 0 and row["last_updated"] is None

    async def test_the_same_session_under_another_agent_is_another_conversation(self, client):
        await _save_conversation_turn("tab-two", "agent_a", "q", "a")
        rows = await _status(client, ("tab-two", "agent_b"))
        assert rows["tab-two"]["turn_count"] == 0

    async def test_ten_tabs_at_most(self, client):
        response = await client.post("/api/sessions/status", json={
            "sessions": [{"session_id": f"s{i}", "agent_id": "agent_a"} for i in range(11)],
        })
        assert response.status_code == 422


class TestHistorySearchAsksTheServer:
    async def _found(self, client, q: str, **params) -> set[str]:
        response = await client.get("/api/sessions", params={"q": q, **params})
        assert response.status_code == 200, response.text
        return {s["session_id"] for s in response.json()}

    async def test_matches_a_question_or_an_answer_in_any_turn_ignoring_case(self, client):
        await _save_conversation_turn("find-q", "agent_a", "Chase the Northwind invoices", "Done.")
        await _save_conversation_turn("find-a", "agent_a", "What is late?", "Two Northwind invoices.")
        await _save_conversation_turn("find-later", "agent_a", "hello", "hi")
        await _save_conversation_turn("find-later", "agent_a", "and northwind?", "Paid.")
        await _save_conversation_turn("find-none", "agent_a", "Rotterdam delays", "Fog.")
        assert await self._found(client, "NORTHWIND") == {"find-q", "find-a", "find-later"}

    async def test_an_empty_query_is_the_whole_list(self, client):
        await _save_conversation_turn("all-1", "agent_a", "q", "a")
        assert "all-1" in await self._found(client, "  ")

    async def test_search_keeps_the_agent_filter_and_hides_what_history_hides(self, client):
        await _save_conversation_turn("both-a", "agent_a", "invoice", "a")
        await _save_conversation_turn("both-b", "agent_b", "invoice", "a")
        await _save_conversation_turn("builder:default:x", "agent_native_builder_analyst", "invoice", "a")
        assert await self._found(client, "invoice", agent_id="agent_a") == {"both-a"}
        assert await self._found(client, "invoice") == {"both-a", "both-b"}
