"""A run's or a chat's event stream that cannot read Redis says so in fixed words, never Redis's own (F1478).

CodeQL py/stack-trace-exposure on PR #379 (alerts 9143, 9855, 9941, 9942, 9944, 9945; D3ASM's CODEQL-C): a failed
`XREAD` sent its exception's text to the stream's reader as an SSE comment, ": error reading stream: Error 111
connecting to redis:6379. Connection refused." Every stream route returns these frames, v1's and v2's to an API
key's caller among them. A browser drops comment lines; a raw reader saw Redis's address and its client's words.

The stream still says it could not read (a comment, which also keeps the connection alive through the outage) and
still retries from where it was. The error is logged by its type alone: an exception's words can carry a value.
"""

from __future__ import annotations

import json
import logging

import pytest

#: What a refused connection says, with an address that must reach nobody.
REDIS_SAYS = "Error 111 connecting to redis-internal.example:6379. Connection refused."


class _RedisThatFailsOnce:
    """A Redis whose first XREAD fails as an unreachable server does, then hands the stream's end."""

    def __init__(self) -> None:
        self.reads = 0

    async def xread(self, streams, count=None, block=None):
        self.reads += 1
        if self.reads == 1:
            raise ConnectionError(REDIS_SAYS)
        key = next(iter(streams))
        return [(key, [(b"1-0", {b"data": json.dumps({"type": "done"}).encode()})])]

    async def xrange(self, *_a, **_k):
        return []


async def _frames(kind: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from core.scale import event_bridge

    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(event_bridge.asyncio, "sleep", _no_wait)
    redis = _RedisThatFailsOnce()
    stream = (event_bridge.stream_run_events(redis, "run-1478") if kind == "run"
              else event_bridge.stream_chat_events(redis, "chat-1478"))
    frames = [frame async for frame in stream]
    assert redis.reads == 2, "it retried from where it was"
    return frames


@pytest.mark.parametrize("kind", ["run", "chat"])
async def test_the_reader_gets_fixed_words_and_the_stream_carries_on(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = await _frames(kind, monkeypatch)
    said = "".join(frames)

    assert "redis-internal.example" not in said and "6379" not in said and "Connection refused" not in said, frames
    assert frames[0] == ": error reading stream\n\n", frames
    assert any('"type": "done"' in f for f in frames[1:]), frames


@pytest.mark.parametrize("kind", ["run", "chat"])
async def test_the_error_is_logged_by_its_type_alone(
    kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.scale.event_bridge"):
        await _frames(kind, monkeypatch)

    assert "ConnectionError" in caplog.text, caplog.text
    assert "redis-internal.example" not in caplog.text, caplog.text
