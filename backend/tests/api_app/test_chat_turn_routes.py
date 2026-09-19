"""What a chat turn that outlives its reader needs around it (F49).

One turn per conversation at a time, a way back in (`GET /chat/{id}/events`),
a conversation that says what is still open in it, and Stop — which is now the
only thing that ends a turn early, so it must end exactly the turn it names.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from core import chat_turns
from core.store import sessions as store


def _data(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def _ids(text: str) -> list[str]:
    return [line[4:] for line in text.splitlines() if line.startswith("id: ")]


async def _messages(session_id: str) -> list[dict]:
    for row in await store.load():
        if row.get("session_id") == session_id:
            return row.get("messages") or []
    return []


@pytest.fixture
async def chat_agent(seed_agent, monkeypatch):
    agent = await seed_agent(tools=[], skip_default_tools=True)
    import core.server as server
    monkeypatch.setitem(server._session_tools, "_test", [])
    return agent


@contextlib.asynccontextmanager
async def _turn_in_a_model_call(live_server, gated_llm, sid, agent_id, message="q"):
    """A turn started over a real connection and held inside its model call."""
    async with live_server.client() as c:
        async with c.stream("POST", "/chat/stream", json={
            "message": message, "session_id": sid, "agent_id": agent_id,
        }) as response:
            assert response.status_code == 200

            async def drain():
                async for _ in response.aiter_lines():
                    pass

            reader = asyncio.create_task(drain())
            try:
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                yield chat_turns.live(sid)
            finally:
                gated_llm.release.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader, 10)


class TestOneTurnPerConversation:
    async def test_a_second_message_while_one_is_answered_is_refused(
        self, client, live_server, gated_llm, chat_agent
    ):
        async with _turn_in_a_model_call(live_server, gated_llm, "one", chat_agent["id"]):
            for route in ("/chat/stream", "/chat"):
                second = await client.post(route, json={
                    "message": "and another thing", "session_id": "one",
                    "agent_id": chat_agent["id"],
                })
                assert second.status_code == 409, (route, second.text)
                assert "still being written" in second.json()["detail"]

    async def test_answering_a_question_while_a_turn_runs_leaves_it_open(
        self, client, live_server, gated_llm, chat_agent
    ):
        pending = {"tool_name": "run_sql_query", "tool_args": {}, "approval_id": "a1",
                   "category": "destructive", "message": "m", "progress": {}}
        await store.set_pending_approval("one", chat_agent["id"], pending)
        async with _turn_in_a_model_call(live_server, gated_llm, "one", chat_agent["id"]):
            answer = await client.post("/chat/one/approve", json={
                "approve": True, "agent_id": chat_agent["id"],
            })
            assert answer.status_code == 409
            assert await store.get_pending_approval("one", chat_agent["id"]) is not None, (
                "the question was spent before the answer was refused"
            )

    async def test_retry_cannot_take_back_a_turn_while_the_next_is_being_written(
        self, client, live_server, gated_llm, chat_agent
    ):
        async with _turn_in_a_model_call(live_server, gated_llm, "one", chat_agent["id"]):
            popped = await client.post("/api/sessions/one/last-turn/pop", json={})
            assert popped.status_code == 409


class TestStop:
    async def test_a_stop_that_finds_nothing_does_not_end_the_next_turn(
        self, client, fake_llm, chat_agent
    ):
        """A Stop pressed just after a turn ended used to leave the session
        flagged, and the next turn on that conversation ended at its first
        boundary with no answer and no word about why."""
        fake_llm.set_default("Here is your answer.")
        stopped = await client.post("/chat/two/cancel")
        assert stopped.json()["stopped"] is False

        reply = await client.post("/chat/stream", json={
            "message": "hello", "session_id": "two", "agent_id": chat_agent["id"],
        })
        types = [e["type"] for e in _data(reply.text)]
        assert "response" in types, types

    async def test_a_stopped_turn_keeps_its_question(
        self, client, live_server, gated_llm, chat_agent
    ):
        async with _turn_in_a_model_call(live_server, gated_llm, "three", chat_agent["id"],
                                         message="what did I ask?") as turn:
            stopped = await client.post("/chat/three/cancel")
            assert stopped.json()["stopped"] is True
            await asyncio.wait_for(turn.wait(), 10)

        saved = await _messages("three")
        assert [m["role"] for m in saved] == ["user", "assistant"]
        assert saved[0]["content"] == "what did I ask?"
        assert saved[1]["content"] == chat_turns.STOPPED_ANSWER
        assert [e.event["type"] for e in turn.entries][-2:] == ["cancelled", "done"]

    async def test_a_v1_caller_can_stop_its_turn(
        self, client, api_key, live_server, gated_llm, chat_agent
    ):
        async with live_server.client() as c:
            async with c.stream("POST", "/api/v1/chat/stream", json={
                "message": "q", "agent": chat_agent["id"], "session_id": "api_four",
            }, headers=api_key["headers"]) as response:
                assert response.status_code == 200
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                stopped = await client.post("/api/v1/chat/api_four/cancel",
                                            headers=api_key["headers"])
                assert stopped.status_code == 200
                assert stopped.json() == {"status": "cancellation_requested",
                                          "session_id": "api_four", "stopped": True}
                body = (await response.aread()).decode()
        assert [e["type"] for e in _data(body)][-2:] == ["cancelled", "done"]

        again = await client.post("/api/v1/chat/api_four/cancel", headers=api_key["headers"])
        assert again.json()["stopped"] is False


class TestStopReachesTheRunAnAgentStarted:
    async def test_stopping_an_orchestrator_agents_turn_stops_its_run(
        self, client, live_server, gated_llm, seed_orchestration
    ):
        """An orchestrator agent's turn starts a run that belongs to no reader.
        Stopping the turn used to stop only the turn: the run went on, unwatched,
        spending, until it finished by itself."""
        from _fakes import seed as S
        from core.orchestration.runner import _active_tasks
        from core.orchestration.state import SharedState

        orch = await seed_orchestration(entry_step_id="s", steps=[
            {"id": "s", "name": "Think", "type": "llm", "prompt_template": "hi",
             "output_key": "out", "next_step_id": None},
        ])
        agent = S.make_orchestrator_agent(orch["id"])
        await S.seed_agents([agent])

        async with live_server.client() as c:
            async with c.stream("POST", "/chat/stream", json={
                "message": "go", "session_id": "ten", "agent_id": agent["id"],
            }) as response:
                assert response.status_code == 200
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                run_id = next(r for r in _active_tasks if r.startswith(f"run_{orch['id']}")
                              or orch["id"] in r)

                stopped = await client.post("/chat/ten/cancel")
                assert stopped.json()["stopped"] is True
                await response.aread()

        deadline = asyncio.get_running_loop().time() + 5
        while run_id in _active_tasks and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert run_id not in _active_tasks, "the run the agent started is still running"
        assert (await SharedState.restore(run_id)).run.status == "cancelled"


class TestReadingATurnAgain:
    async def test_a_finished_turn_replays_with_cursors(self, client, fake_llm, chat_agent):
        fake_llm.set_default("Forty-two.")
        first = await client.post("/chat/stream", json={
            "message": "q", "session_id": "five", "agent_id": chat_agent["id"],
        })
        ids = _ids(first.text)
        assert ids, "the stream carries no cursor to come back with"

        again = await client.get("/chat/five/events")
        assert [e["type"] for e in _data(again.text)] == [e["type"] for e in _data(first.text)]

        tail = await client.get(f"/chat/five/events?after={ids[1]}")
        assert [e["type"] for e in _data(tail.text)] == [e["type"] for e in _data(first.text)][2:]
        by_header = await client.get("/chat/five/events", headers={"Last-Event-ID": ids[1]})
        assert _data(by_header.text) == _data(tail.text)

    async def test_nothing_to_read_is_a_404_and_another_turns_cursor_a_410(
        self, client, fake_llm, chat_agent
    ):
        assert (await client.get("/chat/never/events")).status_code == 404
        await client.post("/chat/stream", json={
            "message": "q", "session_id": "six", "agent_id": chat_agent["id"],
        })
        stale = await client.get("/chat/six/events?after=0123456789ab-3")
        assert stale.status_code == 410

    async def test_a_reader_joining_mid_turn_follows_it_to_the_end(
        self, live_server, gated_llm, chat_agent
    ):
        async with _turn_in_a_model_call(live_server, gated_llm, "seven", chat_agent["id"]):
            async with live_server.client() as c:
                async with c.stream("GET", "/chat/seven/events") as again:
                    assert again.status_code == 200
                    gated_llm.release.set()
                    body = (await again.aread()).decode()
        types = [e["type"] for e in _data(body)]
        assert types[0] == "status" and types[-2:] == ["response", "done"], types


class TestTheConversationSaysWhatIsOpen:
    async def test_a_running_turn_is_named_in_the_history(
        self, client, live_server, gated_llm, chat_agent
    ):
        async with _turn_in_a_model_call(live_server, gated_llm, "eight", chat_agent["id"],
                                         message="still thinking?"):
            history = (await client.get("/api/sessions/eight/history")).json()
            assert history["running"]["message"] == "still thinking?"
            assert history["running"]["cursor"].endswith("-0")

    async def test_a_parked_question_comes_back_without_the_loops_state(self, client):
        await store.set_pending_approval("nine", "default", {
            "tool_name": "run_sql_query", "tool_args": {"query": "DROP TABLE users"},
            "approval_id": "abc", "category": "destructive", "message": "clean up",
            "progress": {"context_text": "private engine state"},
        })
        history = (await client.get("/api/sessions/nine/history")).json()
        card = history["pending_approval"]
        assert card["tool_name"] == "run_sql_query"
        assert card["message"] == "clean up"
        assert card["prompt"].startswith("The agent wants to run **run_sql_query**")
        assert "progress" not in card
        assert history["running"] is None


class TestTheWidgetReadsThroughTheSameFilter:
    async def test_a_widget_turn_read_again_carries_only_what_a_visitor_may_see(
        self, client, fake_llm, chat_agent
    ):
        fake_llm.set_default("We ship to Canada.")
        made = await client.post("/api/widgets", json={
            "name": "Support", "agent_id": chat_agent["id"],
            "allowed_origins": ["https://acme.com"],
        })
        minted = (await client.post("/api/widget/session", json={
            "public_key": made.json()["public_key"], "origin": "https://acme.com",
        })).json()
        auth = {"Authorization": f"Bearer {minted['token']}"}
        await client.post("/api/widget/chat", json={"message": "Canada?"}, headers=auth)

        again = await client.get("/api/widget/stream?cursor=0", headers=auth)
        assert again.status_code == 200
        events = _data(again.text)
        assert {e["type"] for e in events} <= {"status", "thinking", "response", "done"}, events
        assert events[-1] == {"type": "done"}
        assert _ids(again.text), "a widget reader gets no cursor to reconnect with"
        assert "llm_thought" not in again.text

        history = (await client.get("/api/widget/history", headers=auth)).json()
        assert history["running"] is None
