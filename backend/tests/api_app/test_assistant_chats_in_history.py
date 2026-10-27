"""Several Synapse AI chats, each its own, and all of them in History (TABS2, D327; F1061, F1062).

Synapse AI kept one conversation for the whole app: the panel and the chat page's one Synapse AI tab drew the same
state, and only a conversation still working or waiting came back (F214). Naveen, 2026-10-27: a new Synapse AI tab is
a new chat, History lists Synapse AI's chats so one can be picked up again, and the panel has a list of its own.

A chat is its client id: every turn is a builder run under `builder:<tenant>:panel:<id>`. These read those runs back
by id, list them, merge them into History and the tabs' status read, and delete one.
"""
from __future__ import annotations

import asyncio

from core.default_agent import DEFAULT_AGENT_ID
from core.session import _save_conversation_turn
from test_builder_comes_back import _park, _row, _set, builder  # noqa: F401 — fixture

ASSISTANT = DEFAULT_AGENT_ID


async def _chat(client, cid: str) -> dict | None:
    response = await client.get("/api/builder/conversation", params={"session": cid})
    assert response.status_code == 200, response.text
    return response.json()["conversation"]


async def _chats(client, **params) -> list[dict]:
    response = await client.get("/api/builder/conversations", params=params)
    assert response.status_code == 200, response.text
    return response.json()["conversations"]


async def _history(client, **params) -> list[dict]:
    response = await client.get("/api/sessions", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _finish(run_id: str, summary: str) -> None:
    """End a parked turn as a finished one that said `summary`."""
    await _set(run_id, status="completed", waiting_for_human=False,
               shared_state={**(await _row(run_id)).shared_state, "summary": summary})


class TestEachChatIsItsOwn:
    async def test_two_ids_are_two_chats(self, client, builder, fake_llm):
        await _park(client, fake_llm, message="first chat's ask", cid="chat-a")
        await _park(client, fake_llm, message="second chat's ask", cid="chat-b")

        a, b = await _chat(client, "chat-a"), await _chat(client, "chat-b")

        assert [t["user"] for t in a["turns"]] == ["first chat's ask"]
        assert [t["user"] for t in b["turns"]] == ["second chat's ask"]
        assert a["pending"]["run_id"] != b["pending"]["run_id"]

    async def test_each_chats_earlier_turn_is_its_own(self, client, builder, fake_llm):
        # The front door reads "the second one" against the conversation's previous turn (P4's F984): never another's.
        from core.native_builder.read_turn import _previous
        from core.tenancy import get_tenant

        a = await _park(client, fake_llm, message="in a", cid="turn-a")
        await asyncio.sleep(0.01)
        await _park(client, fake_llm, message="in b", cid="turn-b")

        run_id, state = await _previous(f"builder:{get_tenant()}:panel:turn-a", "a-run-not-yet-made")
        assert run_id == a["run_id"] and state.get("user_message") == "in a"

    async def test_a_finished_chat_comes_back_by_its_id(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, message="make a weekly digest", cid="chat-done")
        await _finish(asked["run_id"], "Built the weekly digest.")

        chat = await _chat(client, "chat-done")

        assert chat is not None, "a finished Synapse AI chat can't be picked up again"
        assert chat["client_session_id"] == "chat-done"
        assert chat["status"] == "completed"
        assert chat["pending"] is None
        assert chat["turns"] == [{"user": "make a weekly digest", "assistant": "Built the weekly digest."}]

    async def test_a_chat_nobody_started_is_nothing(self, client, builder):
        assert await _chat(client, "never-used") is None

    async def test_the_chat_pages_old_builder_conversation_is_not_a_panel_chat(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, cid="compat")
        from core.tenancy import get_tenant
        await _set(asked["run_id"], session_id=f"builder:{get_tenant()}:compat")
        assert await _chat(client, "compat") is None

    async def test_without_an_id_the_newest_open_chat_still_comes_back(self, client, builder, fake_llm):
        # The panel's reload (F214) is unchanged.
        await _park(client, fake_llm, cid="still-open")
        response = await client.get("/api/builder/conversation")
        assert response.json()["conversation"]["client_session_id"] == "still-open"


class TestThePanelsListIsSynapseAIsOnly:
    async def test_newest_first_with_the_first_message_as_its_title(self, client, builder, fake_llm):
        older = await _park(client, fake_llm, message="older chat", cid="list-old")
        await _finish(older["run_id"], "Done.")
        await asyncio.sleep(0.01)
        await _park(client, fake_llm, message="newer chat", cid="list-new")
        await _save_conversation_turn("agent-chat", "agent_support", "an agent chat", "an answer")

        rows = await _chats(client)

        assert [r["session_id"] for r in rows] == ["list-new", "list-old"]
        assert rows[0]["title"] == "newer chat"
        assert rows[0]["waiting"] is True and rows[0]["running"] is False
        assert rows[1]["waiting"] is False and rows[1]["turn_count"] == 1
        assert all(r["agent_id"] == ASSISTANT for r in rows)
        assert rows[0]["last_updated"]

    async def test_a_chat_is_one_row_however_many_turns(self, client, builder, fake_llm):
        first = await _park(client, fake_llm, message="first ask", cid="many")
        await _finish(first["run_id"], "Done.")
        await asyncio.sleep(0.01)
        await _park(client, fake_llm, message="second ask", cid="many")

        rows = await _chats(client)

        assert len(rows) == 1
        assert rows[0]["title"] == "first ask"
        assert rows[0]["turn_count"] == 2
        # What it said last: the question its newest turn is asking.
        assert rows[0]["last_response"] == "What should it do?"

    async def test_search_reads_every_turn_on_the_server(self, client, builder, fake_llm):
        first = await _park(client, fake_llm, message="Chase the Northwind invoices", cid="find-me")
        await _finish(first["run_id"], "Built it.")
        await _park(client, fake_llm, message="something else", cid="not-me")

        assert [r["session_id"] for r in await _chats(client, q="northwind")] == ["find-me"]
        assert [r["session_id"] for r in await _chats(client, q="BUILT IT")] == ["find-me"]

    async def test_another_tenants_chats_are_not_listed(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, cid="theirs")
        await _set(asked["run_id"], tenant_id="someone-else", session_id="builder:someone-else:panel:theirs")
        assert await _chats(client) == []


class TestHistoryHoldsThemToo:
    async def test_mixed_in_with_agent_chats_newest_first(self, client, builder, fake_llm):
        await _save_conversation_turn("agent-older", "agent_support", "an agent chat", "an answer")
        await asyncio.sleep(0.01)
        asked = await _park(client, fake_llm, message="build me a digest", cid="hist-sai")
        await _finish(asked["run_id"], "Built the digest.")

        rows = await _history(client)

        assert [(r["session_id"], r["agent_id"]) for r in rows][:2] == [
            ("hist-sai", ASSISTANT), ("agent-older", "agent_support")]
        sai = rows[0]
        assert sai["first_user_message"] == "build me a digest"
        assert sai["last_response"] == "Built the digest."
        assert sai["turn_count"] == 1

    async def test_the_agent_filter_and_search_reach_them(self, client, builder, fake_llm):
        await _park(client, fake_llm, message="Northwind digest", cid="hist-q")
        await _save_conversation_turn("agent-q", "agent_support", "Northwind refund", "ok")

        assert {r["session_id"] for r in await _history(client, agent_id=ASSISTANT)} == {"hist-q"}
        assert {r["session_id"] for r in await _history(client, agent_id="agent_support")} == {"agent-q"}
        assert {r["session_id"] for r in await _history(client, q="northwind")} == {"hist-q", "agent-q"}

    async def test_the_builders_own_sub_agent_sessions_stay_hidden(self, client, builder, fake_llm):
        from core.tenancy import get_tenant
        await _save_conversation_turn(f"builder:{get_tenant()}:panel:x", "agent_native_builder_planner", "p", "a")
        assert all(not r["session_id"].startswith("builder:") for r in await _history(client))

    async def test_deleting_one_from_history_takes_it_away(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, message="forget me", cid="hist-del")

        response = await client.delete("/api/sessions/hist-del", params={"agent_id": ASSISTANT})

        assert response.status_code == 200, response.text
        assert all(r["session_id"] != "hist-del" for r in await _history(client))
        assert await _chats(client) == []
        assert await _chat(client, "hist-del") is None
        # The question it was waiting on goes with it: nothing is left paused on no screen.
        from sqlalchemy import select

        from core.store import session
        from core.store.models import OrchestrationRunDB
        async with session() as s:
            left = (await s.execute(select(OrchestrationRunDB).where(
                OrchestrationRunDB.run_id == asked["run_id"]))).scalar_one_or_none()
        assert left is None or left.status == "cancelled"


class TestATabBehindReadsItsChat:
    async def _status(self, client, cid: str) -> dict:
        response = await client.post("/api/sessions/status", json={
            "sessions": [{"session_id": cid, "agent_id": ASSISTANT}]})
        assert response.status_code == 200, response.text
        return response.json()["sessions"][0]

    async def test_a_chat_waiting_on_its_card_needs_the_person(self, client, builder, fake_llm):
        await _park(client, fake_llm, cid="status-ask")
        row = await self._status(client, "status-ask")
        assert row["pending_approval"] is True and row["running"] is False
        assert row["turn_count"] == 1 and row["last_updated"]

    async def test_a_finished_chat_counts_its_turns(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, cid="status-done")
        await _finish(asked["run_id"], "Done.")
        row = await self._status(client, "status-done")
        assert row["pending_approval"] is False and row["running"] is False and row["turn_count"] == 1

    async def test_a_new_chat_with_no_turn_yet_is_answered(self, client, builder):
        row = await self._status(client, "status-new")
        assert row["turn_count"] == 0 and row["running"] is False and row["last_updated"] is None
