"""A host can put a provider it serves on an answer rung of its own, and the call then sends what that rung sends —
F1618 (e).

A model step that declares its answer is held by one rung per model (`core.answer_shape`): the provider's schema mode,
a forced tool, or the shape in the prompt's words. The table is the product's own. A host that serves a provider
differently can say so: a model gateway that takes no schema field (it would drop it silently) is put on the prompt
rung, so it is sent the shape in words and no schema. `answer_shape.set_rung_for(provider, rung)` is read first by
`rung()`; it takes one of the three rungs, or None to drop what was set.

Whatever the rung, the step reads the answer against its declared shape, asks once more, then fails by name
(test_a_model_step_answers_its_declared_fields.py's TestAnLlmStep and TestAnAgentStep, on a schema-rung model answering
prose). Without a host rung, nothing changes: the gateway holds a schema as before, and each provider keeps its own.
"""
from __future__ import annotations

import json

import pytest

import core.llm_providers as llm

LEADS = {"type": "object", "properties": {"leads": {"type": "array", "items": {"type": "string"}}},
         "required": ["leads"], "additionalProperties": False}
GATEWAY = {"model_gateway_base_url": "http://gw.example.com", "model_gateway_key": "k"}
MODEL = "synapse.claude-sonnet-4-6"


@pytest.fixture
def shape():
    from core import answer_shape

    yield answer_shape
    if hasattr(answer_shape, "set_rung_for"):
        for provider in ("model_gateway", "anthropic"):
            answer_shape.set_rung_for(provider, None)


async def _sent_to_the_gateway(monkeypatch) -> dict:
    sent: dict = {}

    async def v1(model, messages, system, base_url, api_key, **kw):
        sent.update(kw, prompt=messages[-1]["content"] if messages else None)
        return llm.LLMResult(json.dumps({"leads": ["Harbor & Pine"]}), 1, 1, 0, 0, "stop", (), "")

    async def no_usage(**kwargs):
        return None

    from core import usage_tracker

    monkeypatch.setattr(llm, "call_v1_compatible", v1)
    monkeypatch.setattr(usage_tracker, "log_usage", no_usage)
    await llm.generate_response_ex("Find the leads.", "sys", llm.detect_mode_from_model(MODEL), MODEL, GATEWAY,
                                   response_schema=LEADS)
    return sent


class TestAHostRung:
    def test_a_host_puts_its_gateway_on_the_prompt_rung(self, shape):
        shape.set_rung_for("model_gateway", shape.PROMPT)
        assert shape.rung("model_gateway", MODEL) == shape.PROMPT

    async def test_the_gateway_is_then_sent_no_schema_and_the_prompt_as_it_was(self, shape, monkeypatch):
        shape.set_rung_for("model_gateway", shape.PROMPT)
        sent = await _sent_to_the_gateway(monkeypatch)
        assert not sent.get("response_schema"), sent
        assert sent["prompt"] and "Find the leads." in str(sent["prompt"])

    def test_a_host_rung_is_one_of_the_three_and_none_drops_it(self, shape):
        with pytest.raises(ValueError):
            shape.set_rung_for("model_gateway", "free_text")
        shape.set_rung_for("model_gateway", shape.PROMPT)
        shape.set_rung_for("model_gateway", None)
        assert shape.rung("model_gateway", MODEL) == shape.SCHEMA


class TestNearMisses:
    def test_without_a_host_rung_the_gateway_holds_a_schema_as_before(self, shape):
        assert shape.rung("model_gateway", MODEL) == shape.SCHEMA

    async def test_without_a_host_rung_the_gateway_is_sent_the_schema(self, shape, monkeypatch):
        assert (await _sent_to_the_gateway(monkeypatch)).get("response_schema") == LEADS

    def test_a_claude_on_its_own_key_keeps_its_rung_when_the_gateway_has_one(self, shape):
        before = shape.rung("anthropic", "claude-sonnet-4-6")
        shape.set_rung_for("model_gateway", shape.PROMPT)
        assert shape.rung("anthropic", "claude-sonnet-4-6") == before == shape.SCHEMA
