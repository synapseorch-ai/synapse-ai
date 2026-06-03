"""
Redis Streams + key-based pub/sub for the scale layer.

Channel/key conventions:
  synapse:run:{run_id}:events     Redis Stream — worker publishes SSE events; API server reads
  synapse:cancel:{run_id}         Redis SET key (1h TTL) — API server sets; worker polls
  synapse:human_input:{run_id}    Redis SET key (1h TTL) — API server sets; worker polls
  synapse:chat:{session_id}:events Redis Stream — worker publishes chat SSE events
  synapse:workers:heartbeat       Pub/Sub channel — workers publish heartbeat blobs
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


# ---------------------------------------------------------------------------
# Human-in-the-loop helpers (API server → worker via Redis key)
# ---------------------------------------------------------------------------

async def publish_human_input(
    redis_client,
    run_id: str,
    response: dict,
    ttl: int = 3600,
) -> None:
    key = f"synapse:human_input:{run_id}"
    await redis_client.set(key, json.dumps(response, default=str), ex=ttl)


async def get_human_input(
    redis_client,
    run_id: str,
    poll_interval: float = 0.5,
    timeout: float = 3600.0,
) -> dict | None:
    """Poll for human input. Returns the response dict or None on timeout."""
    key = f"synapse:human_input:{run_id}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        import asyncio
        val = await redis_client.get(key)
        if val:
            await redis_client.delete(key)
            return json.loads(val)
        await asyncio.sleep(poll_interval)
    return None


# ---------------------------------------------------------------------------
# Worker heartbeat
# ---------------------------------------------------------------------------

async def publish_worker_heartbeat(
    redis_client,
    worker_id: str,
    active_jobs: int,
    max_jobs: int,
) -> None:
    payload = json.dumps({
        "worker_id": worker_id,
        "active_jobs": active_jobs,
        "max_jobs": max_jobs,
        "ts": time.time(),
    })
    await redis_client.publish("synapse:workers:heartbeat", payload)
