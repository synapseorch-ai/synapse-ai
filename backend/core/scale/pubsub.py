"""
Redis Streams + key-based pub/sub for the scale layer.

Channel/key conventions:
  synapse:run:{run_id}:events     Redis Stream — worker publishes SSE events; API server reads
  synapse:cancel:{run_id}         Redis SET key (1h TTL) — API server sets; worker polls
  synapse:chat:{session_id}:events Redis Stream — worker publishes chat SSE events
"""
import json
import time
from typing import AsyncGenerator


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

class RunEventPublisher:
    """Publishes SSE events for an orchestration run to a Redis Stream."""

    STREAM_MAXLEN = 10_000  # cap stream length to avoid unbounded memory

    def __init__(self, redis_client, run_id: str, ttl: int = 3600):
        self._redis = redis_client
        self._run_id = run_id
        self._key = f"synapse:run:{run_id}:events"
        self._ttl = ttl

    async def publish(self, event: dict) -> None:
        payload = json.dumps(event, default=str)
        await self._redis.xadd(
            self._key,
            {"data": payload},
            maxlen=self.STREAM_MAXLEN,
            approximate=True,
        )
        await self._redis.expire(self._key, self._ttl)

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
    """Publishes SSE events for a chat session to a Redis Stream."""

    STREAM_MAXLEN = 5_000

    def __init__(self, redis_client, session_id: str, ttl: int = 3600):
        self._redis = redis_client
        self._session_id = session_id
        self._key = f"synapse:chat:{session_id}:events"
        self._ttl = ttl

    async def publish(self, event: dict) -> None:
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
