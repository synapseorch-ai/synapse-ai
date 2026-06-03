"""
EventBridge — bridges Redis Streams to FastAPI SSE endpoints.

Each API server instance subscribes directly to the Redis Stream for a
specific run_id when a client opens an SSE connection. No shared state
is maintained across API server instances — each server reads the Stream
independently. This makes API servers truly stateless with respect to SSE.

The SSE response includes an "id:" line set to the Redis Stream message ID
so clients automatically send Last-Event-ID on reconnect, enabling replay.
"""
import asyncio
import json
from typing import AsyncGenerator


KEEPALIVE_INTERVAL = 15   # seconds between keep-alive comments when idle
XREAD_BLOCK_MS = 1000     # milliseconds to block per XREAD call


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
                count=100,
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
                    msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    data_raw = fields.get(b"data") or fields.get("data", b"{}")
                    data_str = data_raw.decode() if isinstance(data_raw, bytes) else data_raw

                    current_id = msg_id_str

                    # Parse to check for the 'done' sentinel
                    try:
                        parsed = json.loads(data_str)
                    except Exception:
                        parsed = {}

                    yield f"id: {msg_id_str}\ndata: {data_str}\n\n"

                    if parsed.get("type") == "done":
                        return
        else:
            # XREAD timed out with no messages — send keepalive
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
                count=100,
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
                    msg_id_str = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    data_raw = fields.get(b"data") or fields.get("data", b"{}")
                    data_str = data_raw.decode() if isinstance(data_raw, bytes) else data_raw

                    current_id = msg_id_str

                    try:
                        parsed = json.loads(data_str)
                    except Exception:
                        parsed = {}

                    yield f"id: {msg_id_str}\ndata: {data_str}\n\n"

                    if parsed.get("type") == "done":
                        return
        else:
            yield ": keepalive\n\n"
