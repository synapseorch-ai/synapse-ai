"""A resumed turn saved from outside the engine keeps the calls it made before the pause (F311).

The fleet's chat job and a stopped in-process turn save a turn's calls from its
last `_step_progress` snapshot and the events since. On a resume the snapshot
this request has seen starts empty, so a resumed turn that answered (fleet) or
was stopped (both) before its first turn boundary kept only what it did after
the person answered: the calls before the pause were in `resume_state["calls"]`
and nowhere else. (The engine's own save was right: it seeds its record from
the same place.)
"""
from __future__ import annotations

import asyncio
import contextlib

from core import chat_turns
from core.store import sessions as store

EARLIER = {"tool": "read_file", "args": {"path": "notes.txt"}, "result": "old notes"}


async def _messages(session_id: str) -> list[dict]:
    for row in await store.load():
        if row.get("session_id") == session_id:
            return row.get("messages") or []
    return []


class TestTheFleet:
    async def test_a_resumed_turn_that_answers_at_once_keeps_its_earlier_calls(self, monkeypatch):
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

        async def loop(request, server_module, should_stop=None, **_kw):
            # The approved call replayed, then the answer — no turn boundary.
            yield {"type": "tool_execution", "tool_name": "vault_delete", "args": {"path": "old.txt"}}
            yield {"type": "tool_result", "tool_name": "vault_delete", "preview": "deleted"}
            yield {"type": "final", "response": "Deleted it."}

        monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
        monkeypatch.setattr(worker_mod, "_load_chat_history", history)
        monkeypatch.setattr(worker_mod, "_upsert_chat_session", upsert)
        monkeypatch.setattr(mcp_pool, "acquire", acquire)
        monkeypatch.setattr(mcp_pool, "release", nothing)
        monkeypatch.setattr(re_mod, "run_react_loop", loop)
        monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

        import fakeredis.aioredis
        redis = fakeredis.aioredis.FakeRedis()
        resume_state = {"turn": 1, "context_text": "User Request: tidy up\n", "calls": [EARLIER],
                        "approved_calls": ["x"],
                        "pending_call": {"approval_id": "x", "tool": "vault_delete",
                                         "arguments": {"path": "old.txt"}, "approved": True, "reason": ""}}
        result = await worker_mod.run_agent_chat_job(
            {"redis": redis, "session_factory": None, "job_try": 1, "job_id": "j1"},
            "f311-fleet", "agent_x", "tidy up", resume_state=resume_state)

        assert result["status"] == "completed"
        _status, messages = upserts[-1]
        answer = messages[-1]
        assert answer["content"] == "Deleted it."
        assert [c["tool"] for c in answer["calls"]] == ["read_file", "vault_delete"]
        assert answer["calls"][0] == EARLIER


class TestAStoppedResume:
    async def test_it_keeps_the_calls_from_before_the_pause(self, client, live_server, seed_agent,
                                                           monkeypatch):
        started, release = asyncio.Event(), asyncio.Event()

        async def model(*_a, **_kw):
            started.set()
            await release.wait()
            return "never seen"

        import core.llm_providers as llm
        import core.react_engine as re_mod
        monkeypatch.setattr(llm, "generate_response", model, raising=False)
        monkeypatch.setattr(re_mod, "llm_generate_response", model, raising=False)
        agent = await seed_agent(tools=[])
        await store.set_pending_approval("f311s", agent["id"], {
            "tool_name": "vault_delete", "tool_args": {"path": "old.txt"}, "approval_id": "x",
            "category": "destructive", "message": "tidy up",
            "progress": {"turn": 1, "context_text": "User Request: tidy up\n", "calls": [EARLIER]},
        })

        try:
            async with live_server.client() as c:
                async with c.stream("POST", "/chat/f311s/approve", json={
                        "approve": False, "agent_id": agent["id"]}) as response:
                    reader = asyncio.create_task(response.aread())
                    await asyncio.wait_for(started.wait(), 10)
                    turn = chat_turns.live("f311s")
                    assert (await client.post("/chat/f311s/cancel")).json()["stopped"] is True
                    await asyncio.wait_for(turn.wait(), 10)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(reader, 10)
        finally:
            release.set()

        _user, answer = (await _messages("f311s"))[-2:]
        assert answer["content"] == chat_turns.STOPPED_ANSWER
        assert [c["tool"] for c in answer["calls"]] == ["read_file", "vault_delete"]
        assert answer["calls"][1]["refused"] is True, "the refused call as F306 records it"
