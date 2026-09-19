"""Work a client started carries on when the client hangs up (F49).

A closed tab, a reload, a laptop lid, an API caller that stops reading: none of
them is a request to stop. Stop is its own route. So a chat turn or a run that
was started by a streaming request has to finish, and leave behind what it
would have left had someone been watching — the answer in the conversation, the
run's final status.

Every test here uses ``live_server``, a real uvicorn server on the test's loop:
httpx's ASGITransport never tells the app its client went away, so against it a
turn that dies with its reader passes (the reason D99's builder fix could only
be pinned by reading the source).

Each test holds the work inside a model call (``gated_llm``), hangs up, lets
the call answer, and reads the store.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest

from core.store import sessions as store


async def _until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.05)
    return False


async def _messages(session_id: str) -> list[dict]:
    for row in await store.load():
        if row.get("session_id") == session_id:
            return row.get("messages") or []
    return []


async def _hang_up_mid_call(live_server, gated_llm, method, url, **kwargs) -> dict:
    """Read the stream until the work is inside a model call, then hang up.

    Read in a task and keep reading: leaving ``aiter_lines()`` early finalises
    httpx's iterator, which closes the connection there and then — a hang-up
    before the call rather than during it.
    """
    events: list[dict] = []

    async with live_server.client() as c:
        async with c.stream(method, url, **kwargs) as response:
            assert response.status_code == 200, await response.aread()

            async def read():
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

            reader = asyncio.create_task(read())
            try:
                await asyncio.wait_for(gated_llm.started.wait(), 10)
                # The work can reach its model call before the first frame is
                # parsed here; the caller needs that frame (a run id).
                assert await _until(lambda: bool(events), timeout=5)
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
    await live_server.wait_disconnected()
    assert events, "the stream carried no event before the model call"
    return events[0]


@pytest.fixture
async def chat_agent(seed_agent, monkeypatch):
    agent = await seed_agent(tools=[], skip_default_tools=True)
    import core.server as server
    monkeypatch.setitem(server._session_tools, "_test", [])
    return agent


class TestAChatTurnOutlivesItsReader:
    async def test_a_turn_on_the_chat_page(self, live_server, gated_llm, chat_agent):
        sid = "f49-chat-stream"
        await _hang_up_mid_call(
            live_server, gated_llm, "POST", "/chat/stream",
            json={"message": "what is the answer?", "session_id": sid,
                  "agent_id": chat_agent["id"]},
        )
        gated_llm.release.set()
        assert await _until(lambda: _messages(sid)), (
            "the person left mid-turn and the turn was never saved — it died "
            "with the connection it was streaming to"
        )
        saved = await _messages(sid)
        assert [m["role"] for m in saved] == ["user", "assistant"]
        assert saved[1]["content"] == gated_llm.answer

    async def test_a_turn_resumed_after_a_refused_tool_call(
        self, live_server, gated_llm, chat_agent
    ):
        sid = "f49-approve"
        await store.set_pending_approval(sid, chat_agent["id"], {
            "tool_name": "run_sql_query",
            "tool_args": {"query": "DROP TABLE users"},
            "approval_id": "abc123",
            "category": "destructive",
            "message": "clean up the database",
            "progress": None,
        })
        await _hang_up_mid_call(
            live_server, gated_llm, "POST", f"/chat/{sid}/approve",
            json={"approve": False, "reason": "not today", "agent_id": chat_agent["id"]},
        )
        gated_llm.release.set()
        assert await _until(lambda: _messages(sid)), (
            "the resumed turn died with the connection that answered the question"
        )

    async def test_a_widget_visitors_turn(self, client, live_server, gated_llm, chat_agent):
        made = await client.post("/api/widgets", json={
            "name": "Support", "agent_id": chat_agent["id"],
            "allowed_origins": ["https://acme.com"],
        })
        assert made.status_code == 200, made.text
        minted = await client.post("/api/widget/session", json={
            "public_key": made.json()["public_key"], "origin": "https://acme.com",
        })
        assert minted.status_code == 200, minted.text
        token, sid = minted.json()["token"], minted.json()["session_id"]

        await _hang_up_mid_call(
            live_server, gated_llm, "POST", "/api/widget/chat",
            json={"message": "do you ship to Canada?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        gated_llm.release.set()
        assert await _until(lambda: _messages(sid)), (
            "the visitor clicked to another page on the host site and their "
            "question was lost with the turn"
        )

    async def test_a_v1_api_chat_turn(self, api_key, live_server, gated_llm, chat_agent):
        sid = "f49-v1-chat"
        first = await _hang_up_mid_call(
            live_server, gated_llm, "POST", "/api/v1/chat/stream",
            json={"message": "q", "agent": chat_agent["id"], "session_id": sid},
            headers=api_key["headers"],
        )
        assert first["type"] == "session"
        gated_llm.release.set()
        assert await _until(lambda: _messages(sid)), (
            "an API caller that stopped reading lost the turn it had paid for"
        )


class TestAV1RunOutlivesItsReader:
    async def _status(self, run_id: str) -> str | None:
        from core.orchestration.state import SharedState
        try:
            return (await SharedState.restore(run_id)).run.status
        except FileNotFoundError:
            return None

    async def test_a_run_started_by_run_stream(
        self, api_key, live_server, gated_llm, seed_orchestration
    ):
        orch = await seed_orchestration(
            entry_step_id="s",
            steps=[{"id": "s", "name": "Think", "type": "llm",
                    "prompt_template": "hi", "output_key": "out",
                    "next_step_id": None}],
        )
        first = await _hang_up_mid_call(
            live_server, gated_llm, "POST", f"/api/v1/orchestrations/{orch['id']}/run/stream",
            json={"message": "go"}, headers=api_key["headers"],
        )
        run_id = first.get("run_id")
        assert run_id, first
        gated_llm.release.set()
        assert await _until(lambda: self._is(run_id, "completed")), (
            f"the run is {await self._status(run_id)!r} after its caller hung up"
        )

    async def test_a_run_resumed_by_resume_stream(
        self, client, api_key, live_server, gated_llm, seed_orchestration
    ):
        orch = await seed_orchestration(
            entry_step_id="ask",
            steps=[
                {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
                 "output_key": "answer", "next_step_id": "s"},
                {"id": "s", "name": "Think", "type": "llm",
                 "prompt_template": "hi", "output_key": "out", "next_step_id": None},
            ],
        )
        started = await client.post(
            f"/api/v1/orchestrations/{orch['id']}/run",
            json={"message": "go"}, headers=api_key["headers"],
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        assert await _until(lambda: self._is(run_id, "paused")), await self._status(run_id)

        await _hang_up_mid_call(
            live_server, gated_llm, "POST",
            f"/api/v1/orchestrations/runs/{run_id}/resume/stream",
            json={"response": "yes"}, headers=api_key["headers"],
        )
        gated_llm.release.set()
        assert await _until(lambda: self._is(run_id, "completed")), (
            f"the resumed run is {await self._status(run_id)!r} after its caller hung up"
        )

    async def _is(self, run_id: str, status: str) -> bool:
        return await self._status(run_id) == status
