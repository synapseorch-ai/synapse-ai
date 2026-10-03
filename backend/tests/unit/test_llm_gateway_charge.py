"""What the model gateway charged for a chat call reaches its caller (CR1, D180).

Cloud's gateway settles each `synapse.*` call before it answers and says what it
charged in `usage.charged_microusd`, as its embeddings and rerank already do.
`LLMResult.charged_microusd` carries it, so a Knowledge enrich row records the
settle rather than a $0 estimate. Every other endpoint sends no such field, and
every adapter's result still unpacks where `generate_response_ex` reads it.
"""
from __future__ import annotations

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx


def _answer(**usage_extra):
    return FakeResponse({
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, **usage_extra},
        "choices": [{"message": {"content": "a context"}, "finish_reason": "stop"}],
    })


class TestTheV1Body:
    async def test_a_charge_in_the_usage_is_carried(self, monkeypatch):
        install_httpx(monkeypatch, _answer(charged_microusd=1234))
        result = await llm.call_v1_compatible("nova-lite", [{"role": "user", "content": "q"}], "sys",
                                              "http://gateway.test", "tok")
        assert result.charged_microusd == 1234
        assert (result.text, result.input_tokens, result.output_tokens) == ("a context", 12, 3)

    async def test_no_charge_is_none(self, monkeypatch):
        install_httpx(monkeypatch, _answer())
        result = await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys",
                                              "https://openrouter.ai/api", "key")
        assert result.charged_microusd is None

    @pytest.mark.parametrize("odd", ["1234", -5, True, 1.5, None, {"x": 1}])
    async def test_a_charge_that_is_not_a_count_is_none(self, monkeypatch, odd):
        install_httpx(monkeypatch, _answer(charged_microusd=odd))
        result = await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys",
                                              "http://gateway.test", "tok")
        assert result.charged_microusd is None

    async def test_a_zero_charge_is_kept(self, monkeypatch):
        """A free-grant call settles at 0: that is a charge, not a missing one."""
        install_httpx(monkeypatch, _answer(charged_microusd=0))
        result = await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys",
                                              "http://gateway.test", "tok")
        assert result.charged_microusd == 0


class TestThroughGenerateResponse:
    async def test_a_platform_calls_charge_reaches_the_caller(self, monkeypatch):
        install_httpx(monkeypatch, _answer(charged_microusd=77))
        result = await llm.generate_response_ex(
            prompt_msg="q", sys_prompt="sys", mode="cloud", current_model="synapse.nova-lite",
            current_settings={"model_gateway_base_url": "http://gateway.test", "model_gateway_key": "tok"},
            reasoning_effort="off")
        assert result.text == "a context"
        assert result.charged_microusd == 77

    async def test_a_bedrock_answer_still_unpacks(self, monkeypatch):
        """`_chat_bedrock` and the dispatcher read the first eight fields by name; a ninth must not break them."""
        async def fake_bedrock(*a, **k):
            return llm.LLMResult("from bedrock", 5, 2, 0, 0, "end_turn", (), "")

        monkeypatch.setattr(llm, "call_bedrock", fake_bedrock)
        result = await llm.generate_response_ex(
            prompt_msg="q", sys_prompt="sys", mode="bedrock", current_model="bedrock.amazon.nova-lite-v1:0",
            current_settings={"aws_region": "us-east-1"}, reasoning_effort="off")
        assert result.text == "from bedrock"
        assert result.charged_microusd is None

    async def test_an_adapter_answering_a_plain_tuple_still_unpacks(self, monkeypatch):
        """A provider adapter (or a test double) may still answer the old eight-field tuple."""
        async def fake_openai(*a, **k):
            return ("plain", 4, 1, 0, 0, "stop", (), "")

        monkeypatch.setattr(llm, "call_openai", fake_openai)
        result = await llm.generate_response_ex(
            prompt_msg="q", sys_prompt="sys", mode="cloud", current_model="gpt-4o-mini",
            current_settings={"openai_key": "k"}, reasoning_effort="off")
        assert result.text == "plain"
        assert result.charged_microusd is None
