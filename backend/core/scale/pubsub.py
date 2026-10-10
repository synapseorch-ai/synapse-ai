"""
Redis Streams + key-based pub/sub for the scale layer.

Channel/key conventions:
  synapse:run:{run_id}:events     Redis Stream — worker publishes SSE events; API server reads
  synapse:cancel:{run_id}         Redis SET key (1h TTL) — API server sets; worker polls
  synapse:chat:{session_id}:events Redis Stream — worker publishes chat SSE events
"""
import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import AsyncGenerator

from core.log import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Redis client factory (cluster or single-node)
# ---------------------------------------------------------------------------

def get_redis_client(redis_url: str):
    """Return an async Redis client appropriate for the URL scheme."""
    import redis.asyncio as aioredis

    if redis_url.startswith("redis+cluster://"):
        from redis.asyncio.cluster import RedisCluster
        # Strip scheme prefix and build list of startup nodes
        stripped = redis_url.replace("redis+cluster://", "")
        nodes = [
            {"host": part.split(":")[0], "port": int(part.split(":")[1])}
            for part in stripped.split(",")
        ]
        return RedisCluster(startup_nodes=nodes, decode_responses=False)

    return aioredis.from_url(redis_url, decode_responses=False)


# ---------------------------------------------------------------------------
# Orchestration run event publisher (worker → Redis Stream)
# ---------------------------------------------------------------------------

#: Nudged with a run's id as its events arrive (F377). The stream keeps only about `STREAM_MAXLEN` of them, so an
#: embedder that keeps a run's whole log (cloud's archive) copies them out from its own cursor before they go.
RunSliceHook = Callable[[str], Awaitable[None]]

_slice_hook: RunSliceHook | None = None
_slicing: dict[str, asyncio.Task] = {}
_again: set[str] = set()


def set_run_slice_hook(hook: RunSliceHook | None) -> None:
    """Install the hook every run's publisher nudges (None removes it)."""
    global _slice_hook
    _slice_hook = hook


def _nudge(run_id: str) -> None:
    """Start the hook for `run_id` without waiting for it — or, while one runs, ask it for one more pass."""
    if _slice_hook is None:
        return
    running = _slicing.get(run_id)
    if running is not None and not running.done():
        _again.add(run_id)
        return
    _slicing[run_id] = asyncio.get_running_loop().create_task(_slice(run_id, _slice_hook))


async def _slice(run_id: str, hook: RunSliceHook) -> None:
    try:
        while True:
            _again.discard(run_id)
            try:
                await hook(run_id)
            except Exception as e:  # noqa: BLE001 — publishing goes on; the next nudge tries again
                _log.warning(f"[pubsub] slicing run {run_id}'s events failed: {e}")
            if run_id not in _again:
                return
    finally:
        _slicing.pop(run_id, None)


async def drain_run_slices(run_id: str) -> None:
    """Wait for `run_id`'s slicing in flight in this process, if any (before its log is archived)."""
    running = _slicing.get(run_id)
    if running is not None:
        await running


class RunEventPublisher:
    """Publishes SSE events for an orchestration run to a Redis Stream."""

    STREAM_MAXLEN = 10_000  # cap stream length to avoid unbounded memory
    #: Every so many events, the slice hook (if one is installed) is nudged (F377).
    SLICE_EVERY = 1_000

    def __init__(self, redis_client, run_id: str, ttl: int = 3600):
        self._redis = redis_client
        self._run_id = run_id
        self._key = f"synapse:run:{run_id}:events"
        self._ttl = ttl
        self._published = 0

    async def publish(self, event: dict) -> None:
        payload = json.dumps(event, default=str)
        await self._redis.xadd(
            self._key,
            {"data": payload},
            maxlen=self.STREAM_MAXLEN,
            approximate=True,
        )
        await self._redis.expire(self._key, self._ttl)
        self._published += 1
        if self._published % self.SLICE_EVERY == 0:
            _nudge(self._run_id)

    async def publish_done(self) -> None:
        """Publish a sentinel 'done' event so subscribers know the stream ended."""
        await self.publish({"type": "done"})

    async def publish_paused(self) -> None:
        """Publish a 'paused' sentinel — run is waiting for human input.
        Unlike 'done', this does NOT close the SSE stream on the client side."""
        await self.publish({"type": "paused"})


# ---------------------------------------------------------------------------
# Chat session event publisher
# ---------------------------------------------------------------------------

class ChatEventPublisher:
    """Publishes SSE events for a chat session to a Redis Stream.

    The answer's text (`answer_delta`, U72) is merged: held for up to
    `DELTA_WINDOW` seconds and sent as one entry, so a long answer is a few
    hundred stream entries rather than one per model chunk — the stream is
    capped at `STREAM_MAXLEN`, and a reader that reattaches replays it. Any
    other event sends what is held first, so the order is the engine's.
    """

    STREAM_MAXLEN = 5_000
    DELTA_WINDOW = 0.1

    def __init__(self, redis_client, session_id: str, ttl: int = 3600):
        self._redis = redis_client
        self._session_id = session_id
        self._key = f"synapse:chat:{session_id}:events"
        self._ttl = ttl
        self._held: dict | None = None          # a merged answer_delta not yet sent
        self._flusher: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def publish(self, event: dict) -> None:
        if event.get("type") == "answer_delta":
            async with self._lock:
                held = self._held
                if held is not None and held.get("turn") == event.get("turn") \
                        and held.get("delegate_from") == event.get("delegate_from"):
                    held["text"] += event.get("text") or ""
                    return
                if held is not None:
                    await self._send(held)
                self._held = dict(event)
                if self._flusher is None or self._flusher.done():
                    self._flusher = asyncio.ensure_future(self._flush_later())
            return
        async with self._lock:
            await self._send_held()
            await self._send(event)

    async def _flush_later(self) -> None:
        await asyncio.sleep(self.DELTA_WINDOW)
        async with self._lock:
            await self._send_held()

    async def _send_held(self) -> None:
        held, self._held = self._held, None
        if held is not None:
            await self._send(held)

    async def _send(self, event: dict) -> None:
        payload = json.dumps(event, default=str)
        await self._redis.xadd(
            self._key,
            {"data": payload},
            maxlen=self.STREAM_MAXLEN,
            approximate=True,
        )
        await self._redis.expire(self._key, self._ttl)

    async def publish_done(self) -> None:
        await self.publish({"type": "done"})

    async def publish_paused(self) -> None:
        """Publish a 'paused' sentinel — session is waiting for human input."""
        await self.publish({"type": "paused"})


# ---------------------------------------------------------------------------
# Cancellation helpers (API server → worker via Redis key)
# ---------------------------------------------------------------------------

async def publish_cancellation(redis_client, run_id: str, ttl: int = 3600) -> None:
    """Signal a worker to cancel the given run."""
    key = f"synapse:cancel:{run_id}"
    await redis_client.set(key, "1", ex=ttl)


async def is_cancelled(redis_client, run_id: str) -> bool:
    """Check whether a cancellation signal exists for run_id."""
    key = f"synapse:cancel:{run_id}"
    val = await redis_client.get(key)
    return bool(val)


async def clear_cancellation(redis_client, run_id: str) -> None:
    await redis_client.delete(f"synapse:cancel:{run_id}")


#: A chat turn is the one job type with no `orchestration_runs` row and no run
#: id — see `run_agent_chat_job`. So it is keyed on the session instead, in its
#: own namespace: a chat session id and a run id are both opaque strings, and
#: sharing a key space between them is a collision waiting for the day someone
#: names a session after a run.
def _chat_cancel_key(session_id: str) -> str:
    return f"synapse:cancel:chat:{session_id}"


async def publish_chat_cancellation(redis_client, session_id: str, ttl: int = 3600) -> None:
    """Signal a worker to cancel the turn running for this chat session.

    The key this writes has existed since `/api/v2/chat/{id}/cancel` was added.
    Nothing read it, so that endpoint returned 200 and did nothing at all; the
    reader is `run_agent_chat_job`, and it arrives with this commit.
    """
    await redis_client.set(_chat_cancel_key(session_id), "1", ex=ttl)


async def is_chat_cancelled(redis_client, session_id: str) -> bool:
    return bool(await redis_client.get(_chat_cancel_key(session_id)))


async def clear_chat_cancellation(redis_client, session_id: str) -> None:
    await redis_client.delete(_chat_cancel_key(session_id))
