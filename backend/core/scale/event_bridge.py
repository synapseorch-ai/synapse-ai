"""
EventBridge — bridges Redis Streams to FastAPI SSE endpoints.

Each API server instance subscribes directly to the Redis Stream for a
specific run_id when a client opens an SSE connection. No shared state
is maintained across API server instances — each server reads the Stream
independently. This makes API servers truly stateless with respect to SSE.

The SSE response includes an "id:" line set to the Redis Stream message ID
so clients automatically send Last-Event-ID on reconnect, enabling replay.

Sentinel event types:
  done   — run has truly finished (success, failure, or cancellation).
           The SSE stream closes after this event.
  paused — run is waiting for human input. The SSE stream stays open;
           more events will follow once the run is resumed.
           (Old runs published before this convention used "done" as the
           pause sentinel — the backward-compat logic below handles them.)
"""
import asyncio
import json
from typing import AsyncGenerator


KEEPALIVE_INTERVAL = 15   # seconds between keep-alive comments when idle
XREAD_BLOCK_MS = 1000     # milliseconds to block per XREAD call


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


async def _is_stream_done(redis_client, key: str) -> bool:
    """Return True if the stream's last event is a 'done' sentinel."""
    try:
        tail = await redis_client.xrevrange(key, "+", "-", count=1)
        if not tail:
            return False
        _, fields = tail[0]
        data_raw = fields.get(b"data") or fields.get("data", b"{}")
        parsed = json.loads(_decode(data_raw))
        return parsed.get("type") == "done"
    except Exception:
        return False


async def stream_run_events(
    redis_client,
    run_id: str,
    last_event_id: str = "0",
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields raw SSE lines for a given run_id.
    Reads from Redis Stream `synapse:run:{run_id}:events`.

    `last_event_id` is the Redis Stream message ID to resume from.
    Pass "0" to replay from the beginning, or the client's Last-Event-ID
    header value to replay missed events after a reconnect.

    Sentinel handling:
      - "done"   → stream closes (run complete).
      - "paused" → stream stays open (human input step, more events later).
      - Old-style pause: if a "done" event has messages after it in the stream
        (from a resumed run published before the paused sentinel existed),
        the generator continues instead of closing.

    Yields strings like:
      "id: 1234-0\ndata: {...}\n\n"
      ": keepalive\n\n"
    """
    key = f"synapse:run:{run_id}:events"
    current_id = last_event_id or "0"

    while True:
        try:
            results = await redis_client.xread(
                {key: current_id},
                count=500,
                block=XREAD_BLOCK_MS,
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            yield f": error reading stream: {e}\n\n"
            await asyncio.sleep(1)
            continue

        if results:
            for _stream_key, messages in results:
                for msg_id, fields in messages:
                    msg_id_str = _decode(msg_id)
                    data_raw = fields.get(b"data") or fields.get("data", b"{}")
                    data_str = _decode(data_raw)

                    current_id = msg_id_str

                    try:
                        parsed = json.loads(data_str)
                    except Exception:
                        parsed = {}

                    yield f"id: {msg_id_str}\ndata: {data_str}\n\n"

                    if parsed.get("type") == "done":
                        # Backward compat: if this "done" was a pause sentinel
                        # from old code, events may exist after it in the stream.
                        try:
                            lookahead = await redis_client.xrange(
                                key, f"({msg_id_str}", "+", count=1
                            )
                        except Exception:
                            lookahead = []
                        if lookahead:
                            # Events exist after this done — old-style pause sentinel.
                            # Continue reading; current_id is already past this event.
                            continue
                        return
        else:
            # XREAD timed out with no messages — check if stream is already done
            # to avoid leaving reconnected clients in keepalive limbo.
            if await _is_stream_done(redis_client, key):
                yield f"data: {json.dumps({'type': 'stream_complete'})}\n\n"
                return
            yield ": keepalive\n\n"


async def stream_chat_events(
    redis_client,
    session_id: str,
    last_event_id: str = "0",
) -> AsyncGenerator[str, None]:
    """
    Same as stream_run_events but for chat session streams.
    Reads from Redis Stream `synapse:chat:{session_id}:events`.
    """
    key = f"synapse:chat:{session_id}:events"
    current_id = last_event_id or "0"

    while True:
        try:
            results = await redis_client.xread(
                {key: current_id},
                count=500,
                block=XREAD_BLOCK_MS,
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            yield f": error reading stream: {e}\n\n"
            await asyncio.sleep(1)
            continue

        if results:
            for _stream_key, messages in results:
                for msg_id, fields in messages:
                    msg_id_str = _decode(msg_id)
                    data_raw = fields.get(b"data") or fields.get("data", b"{}")
                    data_str = _decode(data_raw)

                    current_id = msg_id_str

                    try:
                        parsed = json.loads(data_str)
                    except Exception:
                        parsed = {}

                    yield f"id: {msg_id_str}\ndata: {data_str}\n\n"

                    if parsed.get("type") == "done":
                        try:
                            lookahead = await redis_client.xrange(
                                key, f"({msg_id_str}", "+", count=1
                            )
                        except Exception:
                            lookahead = []
                        if lookahead:
                            continue
                        return
        else:
            if await _is_stream_done(redis_client, key):
                yield f"data: {json.dumps({'type': 'stream_complete'})}\n\n"
                return
            yield ": keepalive\n\n"


async def get_run_events(
    redis_client,
    run_id: str,
    start: str = "-",
    end: str = "+",
) -> list[dict]:
    """Return all stored events for a run as a list of {id, event} dicts."""
    key = f"synapse:run:{run_id}:events"
    try:
        messages = await redis_client.xrange(key, start, end)
    except Exception:
        return []
    result = []
    for msg_id, fields in messages:
        data_raw = fields.get(b"data") or fields.get("data", b"{}")
        try:
            event = json.loads(_decode(data_raw))
        except Exception:
            event = {}
        result.append({"id": _decode(msg_id), "event": event})
    return result


async def get_chat_events(
    redis_client,
    session_id: str,
    start: str = "-",
    end: str = "+",
) -> list[dict]:
    """Return all stored events for a chat session as a list of {id, event} dicts."""
    key = f"synapse:chat:{session_id}:events"
    try:
        messages = await redis_client.xrange(key, start, end)
    except Exception:
        return []
    result = []
    for msg_id, fields in messages:
        data_raw = fields.get(b"data") or fields.get("data", b"{}")
        try:
            event = json.loads(_decode(data_raw))
        except Exception:
            event = {}
        result.append({"id": _decode(msg_id), "event": event})
    return result
