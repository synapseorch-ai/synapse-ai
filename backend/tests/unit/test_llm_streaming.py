"""
An OpenAI-shaped call streams its answer when the engine asks, and reads the
same either way (U72).

`_post_chat` streams only inside a `ModelWait(deltas=True)`; it puts the stream
back together into the body a non-streamed call returns, so every extraction
after it (tool calls, reasoning, usage, cache tokens, the gateway's charge) is
untouched. These pin that equivalence, the content-only deltas, and the edges:
an endpoint that refuses streaming, a stream cut mid-answer, a line that is not
JSON.
"""
import asyncio
import json

import httpx
import pytest

import core.llm_providers as providers
import core.model_wait as mw

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def instant_backoff(monkeypatch):
    real_sleep = asyncio.sleep

    async def no_wait(_delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(providers.asyncio, "sleep", no_wait)


def _transport(monkeypatch, outcomes):
    """Each request takes the next outcome: an exception, a response, or a callable."""
    seen = []

    def handler(request):
        seen.append(request)
        outcome = outcomes[min(len(seen), len(outcomes)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome

    monkeypatch.setattr(
        providers.httpx, "AsyncClient",
        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def _sse(*chunks, done=True) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _delta(**delta):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _finish(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


_USAGE = {"prompt_tokens": 120, "completion_tokens": 9,
          "prompt_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 0},
          "charged_microusd": 431}


def _usage_chunk():
    return {"choices": [], "usage": _USAGE}


async def _call(*, deltas: bool, tools=None):
    wait = mw.ModelWait(
        providers.call_v1_compatible(
            "synapse.test", [{"role": "user", "content": "hi"}], "be brief", "http://gateway:8080", "tok",
            tools=tools,
        ),
        deltas=deltas,
    )
    events = [e async for e in wait.events()]
    return wait.result, [e for e in events if e["type"] in ("_model_delta", "_model_restart")]


async def test_without_a_streaming_waiter_the_request_is_unchanged(monkeypatch):
    body = {"choices": [{"message": {"content": "Paris."}, "finish_reason": "stop"}], "usage": _USAGE}
    seen = _transport(monkeypatch, [httpx.Response(200, json=body)])

    result, streamed = await _call(deltas=False)

    sent = json.loads(seen[0].content)
    assert "stream" not in sent and "stream_options" not in sent
    assert result.text == "Paris." and streamed == []


async def test_a_streamed_answer_reads_as_the_unstreamed_one(monkeypatch):
    unstreamed = {"choices": [{"message": {"content": "Paris is the capital."}, "finish_reason": "stop"}],
                  "usage": _USAGE}
    _transport(monkeypatch, [httpx.Response(200, json=unstreamed)])
    plain, _ = await _call(deltas=False)

    seen = _transport(monkeypatch, [httpx.Response(200, content=_sse(
        _delta(role="assistant", content=""), _delta(content="Paris is "), _delta(content="the capital."),
        _finish(), _usage_chunk()))])
    result, streamed = await _call(deltas=True)

    sent = json.loads(seen[0].content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert result == plain   # text, tokens, cache split, stop reason, the gateway's charge
    assert result.charged_microusd == 431 and result.cache_read_tokens == 100
    assert "".join(e["text"] for e in streamed) == "Paris is the capital."


async def test_streamed_tool_calls_and_reasoning_read_as_unstreamed(monkeypatch):
    message = {
        "content": "I'll look.", "reasoning_content": "They want weather.",
        "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "weather", "arguments": '{"city": "Pune"}'}},
            {"id": "b", "type": "function", "function": {"name": "time", "arguments": "{}"}},
        ],
    }
    _transport(monkeypatch, [httpx.Response(200, json={
        "choices": [{"message": message, "finish_reason": "tool_calls"}], "usage": _USAGE})])
    plain, _ = await _call(deltas=False, tools=[{"type": "function", "function": {"name": "weather"}}])

    _transport(monkeypatch, [httpx.Response(200, content=_sse(
        _delta(reasoning_content="They want "), _delta(reasoning_content="weather."),
        _delta(content="I'll look."),
        _delta(tool_calls=[{"index": 0, "id": "a", "type": "function",
                            "function": {"name": "weather", "arguments": '{"ci'}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": 'ty": "Pune"}'}}]),
        _delta(tool_calls=[{"index": 1, "id": "b", "type": "function",
                            "function": {"name": "time", "arguments": "{}"}}]),
        _finish("tool_calls"), _usage_chunk()))])
    result, streamed = await _call(deltas=True, tools=[{"type": "function", "function": {"name": "weather"}}])

    assert result == plain
    # Only `content` is handed on; reasoning and arguments never are.
    assert "".join(e["text"] for e in streamed) == "I'll look."


async def test_an_endpoint_that_refuses_streaming_is_asked_once_without_it(monkeypatch):
    seen = _transport(monkeypatch, [
        httpx.Response(400, json={"error": {"message": "streaming is not supported.",
                                            "code": "streaming_unsupported"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                  "usage": {}}),
    ])
    result, _ = await _call(deltas=True)
    assert result.text == "ok"
    assert len(seen) == 2 and "stream" not in json.loads(seen[1].content)


async def test_another_400_is_still_the_error_it_was(monkeypatch):
    seen = _transport(monkeypatch, [httpx.Response(400, json={"error": {"message": "bad model"}})])
    with pytest.raises(providers.LLMError) as err:
        await _call(deltas=True)
    assert "bad model" in str(err.value) and len(seen) == 1


async def test_a_line_that_is_not_json_fails_once_never_retried(monkeypatch, instant_backoff):
    seen = _transport(monkeypatch, [httpx.Response(200, content=b"data: {not json\n\ndata: [DONE]\n\n")])
    with pytest.raises(providers.LLMError):
        await _call(deltas=True)
    assert len(seen) == 1   # F138: an answer that arrived is never paid for twice


async def test_an_error_sent_mid_stream_fails_the_call(monkeypatch):
    _transport(monkeypatch, [httpx.Response(200, content=_sse(
        _delta(content="Par"), {"error": {"message": "overloaded"}}))])
    with pytest.raises(providers.LLMError, match="overloaded"):
        await _call(deltas=True)


async def test_a_stream_cut_mid_answer_is_retried_and_says_so(monkeypatch, instant_backoff):
    async def cut():
        yield _sse(_delta(content="Half an "), done=False)
        raise httpx.ReadError("connection reset")

    _transport(monkeypatch, [
        lambda request: httpx.Response(200, content=cut()),
        httpx.Response(200, content=_sse(_delta(content="Whole answer."), _finish(), _usage_chunk())),
    ])
    result, streamed = await _call(deltas=True)

    assert result.text == "Whole answer."
    kinds = [e["type"] for e in streamed]
    assert kinds == ["_model_delta", "_model_restart", "_model_delta"], streamed
    assert streamed[-1]["text"] == "Whole answer."


async def test_openai_itself_streams_through_the_same_door(monkeypatch):
    seen = _transport(monkeypatch, [httpx.Response(200, content=_sse(
        _delta(content="Hi"), _finish(), _usage_chunk()))])
    wait = mw.ModelWait(providers.call_openai("gpt-x", [{"role": "user", "content": "hi"}], "k"), deltas=True)
    events = [e async for e in wait.events()]
    assert wait.result.text == "Hi"
    assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
    assert [e["text"] for e in events if e["type"] == "_model_delta"] == ["Hi"]


# ── Anthropic ────────────────────────────────────────────────────────────────

import anthropic as _anthropic_sdk

_REAL_ANTHROPIC = _anthropic_sdk.AsyncAnthropic


def _anthropic(monkeypatch, handler):
    monkeypatch.setattr(_anthropic_sdk, "AsyncAnthropic", lambda **kw: _REAL_ANTHROPIC(
        http_client=_REAL_CLIENT(transport=httpx.MockTransport(handler)), **kw))


_MESSAGE = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-x",
    "content": [{"type": "text", "text": "Paris is the capital."}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 50, "output_tokens": 7,
              "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0},
}


def _anthropic_sse() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": dict(
            _MESSAGE, content=[], stop_reason=None,
            usage={"input_tokens": 50, "output_tokens": 1,
                   "cache_read_input_tokens": 40, "cache_creation_input_tokens": 0})}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Paris is "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "the capital."}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 7}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n" for name, body in events).encode()


async def _call_anthropic(deltas: bool):
    wait = mw.ModelWait(providers.call_anthropic(
        "claude-x", [{"role": "user", "content": "hi"}], "be brief", "k"), deltas=deltas)
    events = [e async for e in wait.events()]
    return wait.result, [e["text"] for e in events if e["type"] == "_model_delta"]


async def test_anthropic_streams_and_reads_as_unstreamed(monkeypatch):
    _anthropic(monkeypatch, lambda request: httpx.Response(200, json=_MESSAGE))
    plain, none = await _call_anthropic(False)
    assert none == []

    bodies = []

    def streamed(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_anthropic_sse(), headers={"content-type": "text/event-stream"})

    _anthropic(monkeypatch, streamed)
    result, deltas = await _call_anthropic(True)

    assert bodies[0].get("stream") is True
    assert result == plain
    assert "".join(deltas) == "Paris is the capital."
