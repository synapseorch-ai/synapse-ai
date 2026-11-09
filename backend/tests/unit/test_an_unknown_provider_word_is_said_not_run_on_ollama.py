"""An id that names a provider no entry is, by a word and a dot, is said as unknown, never run as Ollama's (F1491).

TRI2's run B (11-09) gave an agent `anthropic.claude-sonnet-4-6`: Bedrock's shape for Anthropic's model, and no id
this install routes to Anthropic (its ids begin `claude`). The table's fallback sent it to Ollama, and the person who
named Anthropic's model was told "Local Agent Error: Ollama is not reachable". Now the id is said as unknown, with the
nearest id a provider claims, wherever a model id is read to be called: the chat dispatch, embeddings, the readiness
check before anyone types, and a failed build's line. A bare name (`mistral`, a fresh install's default) and a version
(`llama3.2`) stay Ollama's.
"""
import pytest

import core.llm_providers as llm
from core import model_readiness
from core.model_failures import MODEL_FAILURE, failure_sentence, recorded_sentence
from core.orchestration import triage
from core.providers import registry

from _fakes import provider_routing

_REAL_GENERATE_EX = llm.generate_response_ex

TRI2 = "anthropic.claude-sonnet-4-6"


@pytest.mark.parametrize("model, word, near", [
    (TRI2, "anthropic", "claude-sonnet-4-6"),
    ("Anthropic.Claude-Sonnet-4-6", "anthropic", "Claude-Sonnet-4-6"),
    # Bedrock's cross-region form: its region word, then its vendor's.
    ("us.anthropic.claude-sonnet-4-6", "us", "claude-sonnet-4-6"),
    ("openai.gpt-4o", "openai", "gpt-4o"),
    ("xai.grok-4", "xai", "grok-4"),
    # Nothing a provider claims is left once the words are gone: no nearest id, only how to write it.
    ("amazon.nova-pro-v1:0", "amazon", None),
    ("amazon.titan-embed-text-v1", "amazon", None),
    ("meta.llama3-70b-instruct-v1:0", "meta", None),
])
def test_a_provider_word_no_entry_is_said_as_unknown(model, word, near):
    assert registry.unclaimed(model) == word
    assert registry.nearest(model) == near
    said = registry.unknown_said(model)
    assert said.startswith(f"Unknown model {model}: ")
    assert f'"{word}."' in said
    if near:
        assert f"Did you mean {near} ({registry.provider_for(near).label})?" in said
    else:
        assert f"bedrock.{model}" in said and f"ollama.{model}" in said
    # Within what a step's history keeps of an error (`run_error.reason_of`, 300 characters, after "LLMError: ").
    assert len(said) <= 280


@pytest.mark.parametrize("model", [
    # Bare names, versions, tags and paths: Ollama's, as a fresh install's `mistral` always was.
    "mistral", "llama3", "llama3.2", "llama3.2-vision", "qwen2.5:7b", "qwen2.5-coder:1.5b", "phi3.5",
    "mistral-small3.1", "nomic-embed-text:latest", "mymodel:v1.beta", "registry.ollama.ai/library/llama3",
    "openai/gpt-oss-120b", "", None,
    # A provider's own dotted prefix: claimed, so never unknown.
    "ollama.my.model", "bedrock.anthropic.claude-sonnet-4-6", "bedrock.us.amazon.nova-pro-v1:0",
    "synapse.claude-sonnet-4-6", "oaic.some.model", "locv1.qwen3.5", "hf.co/bartowski/x", "cli.claude.sonnet",
    "mistral.mistral-large-latest", "moonshot.kimi-k2.6", "claude-sonnet-4-6", "gpt-4.1", "gemini-2.5-pro",
])
def test_any_other_id_is_not_unknown(model):
    assert registry.unclaimed(model) is None
    assert registry.unknown_said(model) is None


def test_only_the_offered_ids_written_without_their_prefix_are_unknown_in_the_routing_corpus():
    """Of every id the routing record covers, only Bedrock's embedding ids named without `bedrock.` are unknown; they
    went to Ollama, which has neither."""
    assert {m for m in provider_routing.CORPUS if registry.unclaimed(m)} == {
        "amazon.titan-embed-text-v1", "amazon.titan-embed-text-v2:0"}


def test_the_fallback_is_still_ollama_for_every_other_reader():
    # Cost, images and the usage log's provider read `provider_for`, which still answers Ollama for an unknown id;
    # only a call is refused.
    assert registry.provider_for(TRI2) is registry.FALLBACK


async def test_the_chat_dispatch_says_it_and_calls_nothing(monkeypatch):
    called = []

    async def ollama(call, spec):
        called.append(call.model)
        raise AssertionError("Ollama was called")

    monkeypatch.setattr(llm, "_chat_ollama", ollama)
    for mode in (llm.detect_mode_from_model(TRI2), "cloud", "local", ""):
        with pytest.raises(llm.LLMError) as raised:
            await _REAL_GENERATE_EX("hi", "sys", mode, TRI2, {"anthropic_key": "k"})
        assert raised.value.code == llm.UNKNOWN_MODEL
        assert not raised.value.retryable
        assert str(raised.value) == registry.unknown_said(TRI2)
        assert "Ollama" not in str(raised.value) or "ollama." in str(raised.value)
    assert called == []


async def test_a_bare_name_still_runs_on_ollama(monkeypatch):
    called = []

    async def ollama(call, spec):
        called.append(call.model)
        return llm.LLMResult("hi")

    monkeypatch.setattr(llm, "_chat_ollama", ollama)
    for model in ("mistral", "llama3.2"):
        assert (await _REAL_GENERATE_EX("hi", "sys", llm.detect_mode_from_model(model), model, {})).text == "hi"
    assert called == ["mistral", "llama3.2"]


async def test_embeddings_say_it_and_call_nothing(monkeypatch):
    async def ollama(*a, **k):
        raise AssertionError("Ollama was called")

    monkeypatch.setattr(llm, "_embed_via_ollama", ollama)
    monkeypatch.setattr(llm, "_embed_ex_ollama", ollama)
    with pytest.raises(llm.EmbeddingFailed) as raised:
        await llm.embed_batch_ex(["t"], "amazon.titan-embed-text-v1", {})
    assert raised.value.code == llm.UNKNOWN_MODEL
    assert str(raised.value) == registry.unknown_said("amazon.titan-embed-text-v1")
    # The zero-answering embedder keeps its contract: zeros, and nothing called.
    assert await llm.embed_batch(["t", "u"], "amazon.titan-embed-text-v1", {}) == [[0.0] * 768] * 2


async def test_readiness_says_it_before_anyone_types(monkeypatch):
    async def tags(base):
        raise AssertionError("Ollama's list was read")

    monkeypatch.setattr(model_readiness, "_ollama_models", tags)
    assert await model_readiness.model_problem({}, TRI2) == registry.unknown_said(TRI2)


def test_a_failed_call_and_a_failed_build_say_it_in_our_words():
    said = registry.unknown_said(TRI2)
    assert failure_sentence(llm.LLMError(said, code=llm.UNKNOWN_MODEL)) == said
    # A step's history keeps its reason ("LLMError: …", cleaned) and its code (F1483).
    assert recorded_sentence(f"LLMError: {said}", llm.UNKNOWN_MODEL) == said
    assert recorded_sentence("LLMError: something else", llm.UNKNOWN_MODEL) == MODEL_FAILURE[llm.UNKNOWN_MODEL]


def test_triage_classes_it_as_an_unknown_model_whose_fix_is_the_agent_s_model():
    kind, _ = triage.classify(f"LLMError: {registry.unknown_said(TRI2)}",
                              {"model_failure": {"code": llm.UNKNOWN_MODEL}}, status="failed")
    assert kind == "model_unknown"
    assert triage.FIXES[kind] == ("set_agent_model",) and kind in triage.MEANS
