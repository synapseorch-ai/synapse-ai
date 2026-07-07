"""
Fake Redis Stream helpers for V2 SSE tests.

The V2 API streams events out of Redis Streams via
``core.scale.event_bridge.stream_run_events`` / ``stream_chat_events``, keyed:
  runs : synapse:run:{run_id}:events
  chat : synapse:chat:{session_id}:events

Each stream entry stores the SSE payload under a ``data`` field. These helpers
pre-load a fakeredis instance so the bridge can be exercised — including
Last-Event-ID replay — with no real Redis.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator


def new_fake_redis():
    """A fresh in-memory async Redis (bytes responses, like the real client)."""
    import fakeredis.aioredis
    return fakeredis.aioredis.FakeRedis()


def run_stream_key(run_id: str) -> str:
    return f"synapse:run:{run_id}:events"


def chat_stream_key(session_id: str) -> str:
    return f"synapse:chat:{session_id}:events"


async def load_events(redis, key: str, events: list[dict]) -> list[str]:
    """XADD each event (as a ``data`` field) and return the assigned message IDs."""
    ids: list[str] = []
    for ev in events:
        msg_id = await redis.xadd(key, {"data": json.dumps(ev)})
        ids.append(msg_id.decode() if isinstance(msg_id, bytes) else msg_id)
    return ids


async def load_run_events(redis, run_id: str, events: list[dict]) -> list[str]:
    return await load_events(redis, run_stream_key(run_id), events)


async def load_chat_events(redis, session_id: str, events: list[dict]) -> list[str]:
    return await load_events(redis, chat_stream_key(session_id), events)


async def collect_sse(agen: AsyncGenerator[str, None], *, max_items: int = 200,
                      timeout: float = 5.0) -> list[str]:
    """Drain an SSE async generator until it returns, hits ``max_items``, or a
    'done'/'stream_complete' terminal event is seen. Keepalive comments are kept
    so callers can assert on them if needed."""
    out: list[str] = []

    async def _drain():
        async for chunk in agen:
            out.append(chunk)
            if '"type": "done"' in chunk or '"type": "stream_complete"' in chunk:
                break
            if len(out) >= max_items:
                break

    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return out


def data_events(sse_chunks: list[str]) -> list[dict]:
    """Parse the JSON payloads out of collected SSE ``data:`` lines."""
    events: list[dict] = []
    for chunk in sse_chunks:
        for line in chunk.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                try:
                    events.append(json.loads(payload))
                except (json.JSONDecodeError, ValueError):
                    pass
    return events
