"""A turn keeps the calls it made — stopped, and on the fleet too (F242).

A stopped turn was saved with `tools: []`, so the calls it had already made (two
in the stress pass) were lost to the next turn as well; and the fleet's chat job
rewrote each turn as bare role/content, so a fleet conversation kept no calls
at all. Both now save what the turn did, in the shape the in-process path saves.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from core import chat_turns
from core.store import sessions as store


async def _messages(session_id: str) -> list[dict]:
    for row in await store.load():
        if row.get("session_id") == session_id:
            return row.get("messages") or []
    return []


TASKS = {"tool": "update_tasks", "arguments": {"tasks": [
    {"content": "read the pricing module", "status": "in_progress"}]}}


class TestAStoppedTurn:
    async def test_it_keeps_the_calls_it_made(self, client, live_server, seed_agent, monkeypatch):
        started, release = asyncio.Event(), asyncio.Event()
        calls = {"n": 0}

        async def model(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps(TASKS)
            started.set()
            await release.wait()
            return "never seen"

        import core.llm_providers as llm
        import core.react_engine as re_mod
        monkeypatch.setattr(llm, "generate_response", model, raising=False)
        monkeypatch.setattr(re_mod, "llm_generate_response", model, raising=False)
        agent = await seed_agent(tools=[])

        try:
            async with live_server.client() as c:
                async with c.stream("POST", "/chat/stream", json={
                    "message": "plan it", "session_id": "f242s", "agent_id": agent["id"]}) as response:
                    reader = asyncio.create_task(response.aread())
                    await asyncio.wait_for(started.wait(), 10)
                    turn = chat_turns.live("f242s")
                    assert (await client.post("/chat/f242s/cancel")).json()["stopped"] is True
                    await asyncio.wait_for(turn.wait(), 10)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(reader, 10)
        finally:
            release.set()

        user, answer = await _messages("f242s")
        assert (user["content"], answer["content"]) == ("plan it", chat_turns.STOPPED_ANSWER)
        assert [c["tool"] for c in answer["calls"]] == ["update_tasks"]
        assert "read the pricing module" in json.dumps(answer["calls"][0]["args"])
        assert answer["tools"], "the older `tools` field is written too"


class TestTheFleet:
    async def test_its_saved_turn_keeps_the_calls(self, monkeypatch):
        from core.scale import mcp_pool
        from core.scale import worker as worker_mod
        import core.react_engine as re_mod
        upserts = []

        async def history(*_a):
            return []

        async def upsert(_sf, _sid, _aid, status, messages):
            upserts.append((status, [dict(m) for m in messages]))

        async def nothing(*_a, **_kw):
            return None

        async def acquire(*_a, **_kw):
            return object()

        call = {"tool": "list_dir", "args": {"path": "work"}, "result": "inventory/inventory/pricing.py"}

        async def loop(request, server_module, should_stop=None, **_kw):
            yield {"type": "tool_execution", "tool_name": "list_dir", "args": {"path": "work"}}
            yield {"type": "tool_result", "tool_name": "list_dir", "preview": "inventory/inventory/pricing.py"}
            yield {"type": "_step_progress", "turn": 1, "context_text": "x", "calls": [call]}
            yield {"type": "final", "response": "It is in pricing.py."}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        redis = fakeredis.aioredis.FakeRedis()
        result = await worker_mod.run_agent_chat_job(
            {"redis": redis, "session_factory": None, "job_try": 1, "job_id": "j1"},
            "f242-fleet", "agent_x", "where is pricing?")

        assert result["status"] == "completed"
        status, messages = upserts[-1]
        assert status == "completed"
        user, answer = messages[-2:]
        assert (user["content"], answer["content"]) == ("where is pricing?", "It is in pricing.py.")
        assert answer["calls"] == [call]
        assert answer["tools"] == ["list_dir: inventory/inventory/pricing.py"]
