"""
Provider call_* bodies in core.llm_providers, exercised with mocked transport:
response parsing, token/cache accounting, tool-call extraction, and the
non-retryable error path (which raises LLMError without hitting the backoff
sleeps).
"""
from types import SimpleNamespace

import httpx
import pytest

import core.llm_providers as llm
from _fakes.fake_http import install_httpx, FakeResponse


def _openai_ok(text="hello", tool_calls=None):
    msg = {"content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return FakeResponse({
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "prompt_tokens_details": {"cached_tokens": 4}},
        "choices": [{"message": msg}],
    })


class TestOpenAIBody:
    async def test_success_and_cache_accounting(self, monkeypatch):
        install_httpx(monkeypatch, _openai_ok("hi there"))
        text, it, ot, cr, cw = await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")
        assert text == "hi there"
        assert cr == 4              # cached tokens surfaced
        assert it == 6             # prompt_tokens (10) minus cached (4)
        assert ot == 5

    async def test_tool_call_response(self, monkeypatch):
        install_httpx(monkeypatch, _openai_ok("", tool_calls=[
            {"function": {"name": "get_x", "arguments": "{}"}}]))
        text, *_ = await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key",
                                         tools=[{"type": "function", "function": {"name": "get_x"}}])
        assert isinstance(text, str)

    async def test_non_retryable_error_raises_llmerror(self, monkeypatch):
        install_httpx(monkeypatch, FakeResponse(status_code=400, text="bad request"))
        with pytest.raises(llm.LLMError):
            await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")


class TestGrokDeepSeekBodies:
    async def test_grok_success(self, monkeypatch):
        install_httpx(monkeypatch, _openai_ok("grok says hi"))
        text, *_ = await llm.call_grok("grok-2", [{"role": "user", "content": "q"}], "sys", "key")
        assert "grok" in text

    async def test_deepseek_success(self, monkeypatch):
        install_httpx(monkeypatch, _openai_ok("deepseek answer"))
        text, *_ = await llm.call_deepseek("deepseek-chat", [{"role": "user", "content": "q"}], "sys", "key")
        assert "deepseek" in text


class TestV1CompatibleBody:
    async def test_success(self, monkeypatch):
        install_httpx(monkeypatch, _openai_ok("compat reply"))
        result = await llm.call_v1_compatible(
            "some-model", [{"role": "user", "content": "q"}], "sys",
            "https://openrouter.ai/api", "key")
        assert result[0] == "compat reply"

    async def test_missing_base_url_raises(self):
        with pytest.raises(llm.LLMError):
            await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys", "", "key")


class TestRetryBranch:
    async def test_openai_retries_then_succeeds(self, monkeypatch):
        # First a retryable 503, then a 200 — exercises the backoff/continue path.
        from _fakes.fake_http import sequence_handler
        monkeypatch.setattr(llm.asyncio, "sleep", lambda *a, **k: _noop())  # skip real backoff
        install_httpx(monkeypatch, sequence_handler([
            FakeResponse(status_code=503, text="overloaded"),
            _openai_ok("recovered"),
        ]))
        text, *_ = await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")
        assert text == "recovered"


async def _noop():
    return None


class TestAnthropicBody:
    def _fake_anthropic(self, blocks):
        usage = SimpleNamespace(input_tokens=12, output_tokens=7,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        resp = SimpleNamespace(content=blocks, usage=usage)

        class _Messages:
            async def create(self, **kwargs):
                return resp

        class _FakeAnthropic:
            def __init__(self, *a, **k):
                self.messages = _Messages()

        return _FakeAnthropic

    async def test_text_response(self, monkeypatch):
        import anthropic
        blocks = [SimpleNamespace(type="text", text="claude here", name=None, input=None)]
        monkeypatch.setattr(anthropic, "AsyncAnthropic", self._fake_anthropic(blocks))
        text, it, ot, cr, cw = await llm.call_anthropic(
            "claude-x", [{"role": "user", "content": "q"}], "sys", "key")
        assert text == "claude here"
        assert it == 12 and ot == 7

    async def test_tool_use_response(self, monkeypatch):
        import anthropic
        blocks = [
            SimpleNamespace(type="text", text="let me think", name=None, input=None),
            SimpleNamespace(type="tool_use", text=None, name="lookup", input={"q": "x"}),
        ]
        monkeypatch.setattr(anthropic, "AsyncAnthropic", self._fake_anthropic(blocks))
        text, *_ = await llm.call_anthropic("claude-x", [{"role": "user", "content": "q"}], "sys", "key")
        assert '"tool": "lookup"' in text
        assert "REASONING" in text  # preceding text preserved as reasoning

    async def test_non_retryable_api_error_raises_llmerror(self, monkeypatch):
        import anthropic
        req = httpx.Request("POST", "https://api.anthropic.com")
        resp = httpx.Response(400, request=req)

        class _Messages:
            async def create(self, **kwargs):
                raise anthropic.APIStatusError("bad", response=resp, body=None)

        class _FakeAnthropic:
            def __init__(self, *a, **k):
                self.messages = _Messages()

        monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
        with pytest.raises(llm.LLMError):
            await llm.call_anthropic("claude-x", [{"role": "user", "content": "q"}], "sys", "key")
