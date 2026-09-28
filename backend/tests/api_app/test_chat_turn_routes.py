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
        async with _turn_in_a_model_call(live_server, gated_llm, "one", chat_agent["id"]):
            # Parked once the turn is under way: a question parked before it
            # would be settled by that new message itself (F247).
            await store.set_pending_approval("one", chat_agent["id"], pending)
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

    async def test_a_stopped_turn_is_logged_as_stopped(
        self, client, live_server, gated_llm, chat_agent, monkeypatch
    ):
        """The agent's run log said `completed` for a turn that was stopped:
        `except Exception` does not catch a cancellation."""
        from core.agent_logger import AgentLogger
        ended: list[str] = []
        original = AgentLogger.run_end
        monkeypatch.setattr(AgentLogger, "run_end",
                            lambda self, status: (ended.append(status), original(self, status))[1])

        async with _turn_in_a_model_call(live_server, gated_llm, "eleven", chat_agent["id"]) as turn:
            await client.post("/chat/eleven/cancel")
            await asyncio.wait_for(turn.wait(), 10)
        assert ended == ["cancelled"]

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


class TestEveryAgentTypeKeepsItsTurns:
    """F115: an orchestrator- or builder-type agent's chat turn saved nothing —
    `run_react_loop` hands an orchestrator off before the save, and a builder
    never reaches it — so a reopened conversation (F49) showed such a turn only
    while its event log was held, then not at all. The fleet's chat job has
    saved every agent type all along; the two paths disagreed about the same
    table. What is saved is what the page was shown."""

    @staticmethod
    def _shown(text: str) -> str:
        return [e for e in _data(text) if e["type"] == "response"][-1]["content"]

    async def test_an_orchestrator_agents_turn_is_saved(self, client, seed_orchestration, seed_agent):
        orch = await seed_orchestration(id="orch_f115")
        agent = await seed_agent(type="orchestrator", orchestration_id=orch["id"])

        reply = await client.post("/chat/stream", json={
            "message": "run it", "session_id": "f115", "agent_id": agent["id"]})

        shown = self._shown(reply.text)
        assert shown
        saved = await _messages("f115")
        assert [(m["role"], m["content"]) for m in saved] == [("user", "run it"), ("assistant", shown)]

    @pytest.mark.parametrize("ending,expected", [
        ({"type": "orchestration_complete", "final_state": {"summary": "Built the thing."}},
         "Built the thing."),
        ({"type": "human_input_required", "prompt": "Which of the two?"}, "Which of the two?"),
    ], ids=["finished", "asked"])
    async def test_a_builder_agents_turn_is_saved(self, client, seed_agent, monkeypatch, ending, expected):
        import core.routes.builder as builder

        async def _builder_run(req, server_module):
            yield {"type": "step_start", "step_name": "Plan"}
            yield ending

        monkeypatch.setattr(builder, "run_builder_stream", _builder_run)
        agent = await seed_agent(type="builder")

        reply = await client.post("/chat/stream", json={
            "message": "build me a digest", "session_id": "f115b", "agent_id": agent["id"]})

        assert self._shown(reply.text) == expected
        saved = await _messages("f115b")
        assert [(m["role"], m["content"]) for m in saved] == [
            ("user", "build me a digest"), ("assistant", expected)]

    async def test_a_builder_agent_is_given_the_conversation_so_far(
        self, client, seed_agent, monkeypatch
    ):
        """F134: the chat page's builder path read `request.history_messages`,
        a field `ChatRequest` has never had — so every turn reached the builder
        with no history, and a reply to its question ("the second one") arrived
        with nothing before it. The conversation is what F115 saves."""
        import core.routes.builder as builder

        seen: list[list[dict]] = []
        endings = iter([
            {"type": "human_input_required", "prompt": "Which of the two?"},
            {"type": "orchestration_complete", "final_state": {"summary": "Changed it."}},
        ])

        async def _builder_run(req, server_module):
            seen.append(list(req.history))
            yield next(endings)

        monkeypatch.setattr(builder, "run_builder_stream", _builder_run)
        agent = await seed_agent(type="builder")

        for message in ("change the digest", "the second one"):
            reply = await client.post("/chat/stream", json={
                "message": message, "session_id": "f134", "agent_id": agent["id"]})
            assert reply.status_code == 200, reply.text

        assert seen[0] == [], "a conversation's first turn has nothing before it"
        assert [(m["role"], m["content"]) for m in seen[1]] == [
            ("user", "change the digest"), ("assistant", "Which of the two?")]

    async def test_a_builder_turn_that_cannot_be_saved_is_still_answered(
        self, client, seed_agent, monkeypatch
    ):
        import core.routes.builder as builder
        import core.session as session

        async def _builder_run(req, server_module):
            yield {"type": "orchestration_complete", "final_state": {"summary": "Built it."}}

        async def _store_down(**kwargs):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(builder, "run_builder_stream", _builder_run)
        monkeypatch.setattr(session, "_save_conversation_turn", _store_down)
        agent = await seed_agent(type="builder")

        reply = await client.post("/chat/stream", json={
            "message": "build", "session_id": "f115c", "agent_id": agent["id"]})

        assert self._shown(reply.text) == "Built it."

    async def test_a_stopped_orchestrator_turn_keeps_its_question(
        self, client, live_server, gated_llm, seed_orchestration, seed_agent
    ):
        orch = await seed_orchestration(id="orch_f115s", entry_step_id="think", steps=[
            {"id": "think", "name": "Think", "type": "llm", "prompt_template": "go",
             "output_key": "thought", "next_step_id": None}])
        agent = await seed_agent(type="orchestrator", orchestration_id=orch["id"])

        async with _turn_in_a_model_call(live_server, gated_llm, "f115s", agent["id"],
                                         message="and stop") as turn:
            assert (await client.post("/chat/f115s/cancel")).json()["stopped"] is True
            await asyncio.wait_for(turn.wait(), 10)

        saved = await _messages("f115s")
        assert [(m["role"], m["content"]) for m in saved] == [
            ("user", "and stop"), ("assistant", chat_turns.STOPPED_ANSWER)]

    async def test_the_fleet_keeps_one_pair_and_the_same_answer(
        self, client, fake_redis, seed_orchestration, seed_agent, monkeypatch
    ):
        """The fleet's chat job saved an orchestrator's turn all along, by
        rewriting the conversation after the loop. The loop now saves it as
        well, so this pins that the two writes still leave one pair — and that
        it is the answer the in-process page shows."""
        import types

        from core.scale import mcp_pool, worker
        from core.store import get_store

        server = types.SimpleNamespace(agent_sessions={"_test": object()}, memory_store=None,
                                       tool_router={})

        async def acquire():
            return server

        async def nothing():
            return None

        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(worker, "_load_job_settings", nothing)
        orch = await seed_orchestration(id="orch_f115f")
        agent = await seed_agent(type="orchestrator", orchestration_id=orch["id"])
        shown = self._shown((await client.post("/chat/stream", json={
            "message": "run it", "session_id": "f115-app", "agent_id": agent["id"]})).text)

        ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1,
               "job_id": "job-1"}
        result = await worker.run_agent_chat_job(ctx, "f115-fleet", agent["id"], "run it")

        assert result["status"] == "completed"
        saved = await _messages("f115-fleet")
        assert [(m["role"], m["content"]) for m in saved] == [("user", "run it"), ("assistant", shown)]
