"""
Stress the SSE paths — the area this branch (SSE reconnection/heartbeat) touches.

  * Many concurrent /chat/stream connections through the REAL engine under the
    5-90s fake-LLM latency profile: the streaming plumbing + heartbeat must keep
    every connection alive to a clean 'done' with no errors.
  * Many concurrent V2 Redis-stream readers, including Last-Event-ID reconnects,
    against fakeredis.
"""
import asyncio
import types

import pytest

from _fakes import fake_redis_stream as FRS
from load_harness import run_load

pytestmark = pytest.mark.stress


class TestConcurrentChatStreams:
    async def test_many_chat_streams_complete(self, client, fake_llm, seed_agent,
                                              stress_params, monkeypatch):
        agent = seed_agent(tools=[], skip_default_tools=True)
        import core.tools as tools
        monkeypatch.setitem(tools._session_tools_cache, "_test", [])
        fake_llm.set_default("done under load")

        async def task(i: int):
            resp = await client.post("/chat/stream",
                                     json={"message": f"req {i}", "agent_id": agent["id"]})
            assert resp.status_code == 200
            assert '"type": "done"' in resp.text  # streamed cleanly to completion

        metrics = await run_load("sse_chat_stream", task,
                                 total=stress_params["total"], concurrency=stress_params["concurrency"])
        assert metrics["failed"] == 0, metrics["sample_errors"]


class TestConcurrentV2StreamReconnect:
    async def test_many_readers_with_reconnect(self, fake_redis, stress_params):
        from core.scale.event_bridge import stream_run_events
        n = stress_params["total"]
        # Pre-load a stream per run, each ending with 'done'.
        ids_by_run = {}
        for i in range(n):
            ids_by_run[i] = await FRS.load_run_events(fake_redis, f"run_{i}", [
                {"type": "orchestration_start"}, {"type": "step_complete"}, {"type": "done"}])

        async def task(i: int):
            # Initial read from the start.
            full = FRS.data_events(await FRS.collect_sse(stream_run_events(fake_redis, f"run_{i}", "0")))
            assert full[-1]["type"] == "done"
            # Reconnect after the first event — only newer events replay.
            replay = FRS.data_events(await FRS.collect_sse(
                stream_run_events(fake_redis, f"run_{i}", ids_by_run[i][0])))
            assert "orchestration_start" not in [e["type"] for e in replay]

        metrics = await run_load("sse_v2_reconnect", task,
                                 total=n, concurrency=stress_params["concurrency"])
        assert metrics["failed"] == 0, metrics["sample_errors"]
