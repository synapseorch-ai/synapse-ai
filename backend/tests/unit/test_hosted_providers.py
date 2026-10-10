"""
The hosted OpenAI-style providers (PROV1, D245): Moonshot, Mistral, NVIDIA,
OpenRouter, Qwen, Z.ai, Groq, Cerebras, Together, Fireworks and MiniMax.

One table entry each and one adapter for all of them. What a call sends is
pinned here against a fake server: the provider's own address, its key, the
model without its routing prefix, the response cap, and reasoning in the
spelling that provider takes — `reasoning_effort`, `thinking: {type}` or
`enable_thinking` — with Off said, never left unsaid (F269). Then how each
provider's model list is read, and what `/api/models` serves from it.
"""
import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx
from core import model_readiness
from core.providers import discovery
from core.providers.registry import HOSTED, provider

KEYS = {f"{spec.id}_key": f"k-{spec.id}" for spec in HOSTED}


def _ok(text="ok"):
    return FakeResponse({"usage": {"prompt_tokens": 3, "completion_tokens": 2},
                         "choices": [{"message": {"content": text}, "finish_reason": "stop"}]})


@pytest.fixture
def no_usage(monkeypatch):
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)


@pytest.fixture
def sent(monkeypatch, no_usage):
    """Every POST: (url, headers, body)."""
    posts = []

    def handler(url, kwargs):
        posts.append((url, kwargs.get("headers") or {}, kwargs.get("json") or {}))
        return _ok()

    install_httpx(monkeypatch, handler)
    return posts


async def _ask(model, effort=None, **kw):
    return await llm.generate_response_ex("hi", "sys", llm.detect_mode_from_model(model), model, dict(KEYS),
                                          reasoning_effort=effort, **kw)


# ── the call ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", HOSTED, ids=lambda s: s.id)
async def test_each_provider_is_called_at_its_own_address_with_its_own_key(spec, sent):
    model = f"{spec.id}.{spec.fallback[0]}"
    result = await _ask(model)

    assert result.text == "ok"
    url, headers, body = sent[-1]
    assert url == f"{spec.base_url}/chat/completions"
    assert headers["Authorization"] == f"Bearer k-{spec.id}"
    # The routing prefix is ours; the provider never sees it, and the rest keeps its case.
    assert body["model"] == spec.fallback[0]
    # Sent the response cap, as Grok and DeepSeek are (F263): a reasoning plan may raise it.
    assert body["max_tokens"] >= llm.DEFAULT_MAX_OUTPUT_TOKENS


async def test_tools_are_sent_as_functions(sent):
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    await _ask("groq.llama-3.3-70b-versatile", tools=tools)
    body = sent[-1][2]
    assert body["tools"] == tools and body["tool_choice"] == "auto"


async def test_a_tool_call_comes_back_as_the_loop_reads_it(monkeypatch, no_usage):
    install_httpx(monkeypatch, FakeResponse({
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}}]},
            "finish_reason": "tool_calls"}]}))
    result = await _ask("moonshot.kimi-k2.6")
    assert '"tool": "lookup"' in result.text and '"q": "x"' in result.text


# ── reasoning, in each provider's spelling ───────────────────────────────────

@pytest.mark.parametrize("model,effort,expected,absent", [
    # A switch: Moonshot's K2.x and Z.ai's GLM take `thinking`, Qwen `enable_thinking`.
    ("moonshot.kimi-k2.6", "off", {"thinking": {"type": "disabled"}}, "reasoning_effort"),
    ("moonshot.kimi-k2.6", "high", {"thinking": {"type": "enabled"}}, "reasoning_effort"),
    ("zai.glm-5.2", "off", {"thinking": {"type": "disabled"}}, "reasoning_effort"),
    ("zai.glm-4.7-flash", "low", {"thinking": {"type": "enabled"}}, "reasoning_effort"),
    ("qwen.qwen3.8-max", "off", {"enable_thinking": False}, "reasoning_effort"),
    ("qwen.qwen3.7-plus", "medium", {"enable_thinking": True}, "reasoning_effort"),
    # Kimi K3 and GLM 5.3 always think: low / high / max, and their Off is their Low.
    ("moonshot.kimi-k3", "off", {"reasoning_effort": "low"}, "thinking"),
    ("moonshot.kimi-k3", "high", {"reasoning_effort": "max"}, "thinking"),
    ("zai.glm-5.3", "medium", {"reasoning_effort": "high"}, "thinking"),
    ("fireworks.accounts/fireworks/models/glm-5p3", "low", {"reasoning_effort": "low"}, "thinking"),
    # MiniMax M3: adaptive or disabled. Together's hybrid models: `reasoning: {enabled}`.
    ("minimax.MiniMax-M3", "off", {"thinking": {"type": "disabled"}}, "reasoning_effort"),
    ("minimax.MiniMax-M3", "high", {"thinking": {"type": "adaptive"}}, "reasoning_effort"),
    ("together.moonshotai/Kimi-K3", "off", {"reasoning_effort": "low"}, "reasoning"),
    ("together.zai-org/GLM-5.2", "off", {"reasoning": {"enabled": False}}, "reasoning_effort"),
    ("together.deepseek-ai/DeepSeek-V4.1-Flash", "high", {"reasoning": {"enabled": True}}, "reasoning_effort"),
    # An effort: gpt-oss cannot stop, so its Off is its Low, wherever it is hosted.
    ("groq.openai/gpt-oss-120b", "off", {"reasoning_effort": "low"}, "thinking"),
    ("together.openai/gpt-oss-120b", "medium", {"reasoning_effort": "medium"}, "reasoning"),
    ("cerebras.gpt-oss-120b", "high", {"reasoning_effort": "high"}, "thinking"),
    ("groq.qwen/qwen3.8-27b", "off", {"reasoning_effort": "none"}, "thinking"),
    ("cerebras.qwen-3.8-27b", "low", {"reasoning_effort": "low"}, "thinking"),
    ("fireworks.accounts/fireworks/models/deepseek-v4p1-flash", "off", {"reasoning_effort": "none"}, "thinking"),
    ("openrouter.z-ai/glm-5.2", "medium", {"reasoning_effort": "medium"}, "thinking"),
    ("mistral.mistral-small-latest", "high", {"reasoning_effort": "high"}, "thinking"),
    ("mistral.mistral-small-latest", "off", {"reasoning_effort": "none"}, "thinking"),
])
async def test_reasoning_is_asked_for_as_the_provider_spells_it(model, effort, expected, absent, sent):
    await _ask(model, effort)
    body = sent[-1][2]
    for key, value in expected.items():
        assert body[key] == value, (model, effort, body)
    assert absent not in body


@pytest.mark.parametrize("model", [
    "minimax.MiniMax-M2.7", "moonshot.kimi-k2.7-code",       # always think, take no knob
    "moonshot.kimi-k2-thinking", "mistral.magistral-medium-latest",
    "together.meta-llama/Llama-3.3-70B-Instruct-Turbo",      # no channel at all
    "nvidia.qwen/qwen3-235b-a22b-instruct-2507",             # an instruct build of a thinking family
])
@pytest.mark.parametrize("effort", ["off", "high"])
async def test_a_model_without_a_knob_is_sent_none(model, effort, sent):
    await _ask(model, effort)
    body = sent[-1][2]
    assert not {"reasoning_effort", "thinking", "enable_thinking", "reasoning"} & set(body), body


async def test_a_model_that_always_thinks_has_room_to(sent):
    """Its thinking counts against the cap at every one of these providers."""
    from core.reasoning import HOSTED_CEILING

    await _ask("minimax.MiniMax-M2.7")
    assert sent[-1][2]["max_tokens"] >= HOSTED_CEILING
    await _ask("groq.llama-3.3-70b-versatile")
    assert sent[-1][2]["max_tokens"] == llm.DEFAULT_MAX_OUTPUT_TOKENS


# ── Mistral reasons in chunks ────────────────────────────────────────────────

_MISTRAL_THINKING = [{"type": "thinking", "thinking": [{"type": "text", "text": "Two and two."}]},
                     {"type": "text", "text": "Four."}]


async def test_mistrals_thinking_chunks_are_split_from_its_answer(monkeypatch, no_usage):
    install_httpx(monkeypatch, FakeResponse({
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        "choices": [{"message": {"content": _MISTRAL_THINKING}, "finish_reason": "stop"}]}))
    result = await _ask("mistral.mistral-small-latest", "high")
    assert result.text == "Four."
    assert result.reasoning == "Two and two."


def test_mistrals_streamed_chunks_are_put_back_together():
    chunks = [
        {"choices": [{"delta": {"content": _MISTRAL_THINKING[:1]}}]},
        {"choices": [{"delta": {"content": [{"type": "text", "text": "Fo"}]}}]},
        {"choices": [{"delta": {"content": "ur."}, "finish_reason": "stop"}]},
    ]
    body = llm._StreamedChat(type("R", (), {"headers": {}, "request": None})(), chunks).json()
    message = body["choices"][0]["message"]
    assert message["content"] == "Four." and message["reasoning_content"] == "Two and two."
    assert [llm._content_of(c) for c in chunks] == ["", "Fo", "ur."]


async def test_a_provider_that_refuses_its_thinking_field_is_asked_again_without_it(monkeypatch, no_usage):
    bodies = []

    def handler(url, kwargs):
        body = dict(kwargs.get("json") or {})
        bodies.append(body)
        if "thinking" in body:
            return FakeResponse(status_code=400, text='{"error": "thinking is not supported for this model"}')
        return _ok("answered")

    install_httpx(monkeypatch, handler)
    result = await _ask("zai.glm-5.2", "high")
    assert result.text == "answered"
    assert len(bodies) == 2 and "thinking" in bodies[0] and "thinking" not in bodies[1]


async def test_a_400_about_something_else_is_not_retried(monkeypatch, no_usage):
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return FakeResponse(status_code=400, text='{"error": "messages: too long"}')

    install_httpx(monkeypatch, handler)
    with pytest.raises(llm.LLMError):
        await _ask("zai.glm-5.2", "high")
    assert len(calls) == 1


async def test_a_failure_names_the_provider(monkeypatch, no_usage):
    install_httpx(monkeypatch, FakeResponse(status_code=401, text='{"error": "invalid api key"}'))
    with pytest.raises(llm.LLMError) as caught:
        await _ask("groq.llama-3.3-70b-versatile")
    assert "Groq" in str(caught.value) and "V1-compatible" not in str(caught.value)


async def test_an_out_of_credit_refusal_is_not_retried(monkeypatch, no_usage):
    """F275 holds for every hosted provider: no retry clears a spent balance."""
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return FakeResponse(status_code=429, text='{"error": {"code": "insufficient_quota"}}')

    install_httpx(monkeypatch, handler)
    with pytest.raises(llm.LLMError):
        await _ask("together.openai/gpt-oss-120b")
    assert len(calls) == 1


# ── readiness and images ─────────────────────────────────────────────────────

async def test_a_missing_key_is_said_in_the_providers_name():
    assert await model_readiness.model_problem({}, "groq.openai/gpt-oss-120b") == "No Groq key is saved."
    assert await model_readiness.model_problem({"groq_key": "k"}, "groq.openai/gpt-oss-120b") is None


def test_which_hosts_take_an_image():
    from core.images import model_can_see_images

    assert model_can_see_images("moonshot.kimi-k3")
    assert model_can_see_images("openrouter.openai/gpt-4o")
    assert model_can_see_images("minimax.MiniMax-M3")
    assert not model_can_see_images("groq.llama-3.3-70b-versatile")
    assert not model_can_see_images("zai.glm-5.3")


# ── the model lists ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("provider_id,payload,expected", [
    ("groq", {"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "whisper-large-v3"},
                       {"id": "meta-llama/llama-guard-4-12b"}, {"id": "playai-tts"},
                       {"id": "openai/gpt-oss-120b"}]},
     ["llama-3.3-70b-versatile", "openai/gpt-oss-120b"]),
    ("mistral", {"data": [{"id": "mistral-large-latest", "capabilities": {"completion_chat": True}},
                          {"id": "mistral-embed", "capabilities": {"completion_chat": False}},
                          {"id": "mistral-moderation-latest", "capabilities": {"completion_chat": False}}]},
     ["mistral-large-latest"]),
    ("together", {"data": [{"id": "openai/gpt-oss-120b", "type": "chat"},
                           {"id": "BAAI/bge-large-en-v1.5", "type": "embedding"},
                           {"id": "black-forest-labs/FLUX.1-schnell", "type": "image"}]},
     ["openai/gpt-oss-120b"]),
    ("openrouter", {"data": [{"id": "moonshotai/kimi-k2.5", "architecture": {"output_modalities": ["text"]}},
                             {"id": "google/gemini-image", "architecture": {"output_modalities": ["image"]}}]},
     ["moonshotai/kimi-k2.5"]),
    ("nvidia", {"data": [{"id": "nvidia/nv-embedqa-e5-v5"}, {"id": "nvidia/llama-3.2-nv-rerankqa-1b-v2"},
                         {"id": "moonshotai/kimi-k2-instruct"}]},
     ["moonshotai/kimi-k2-instruct"]),
])
def test_a_providers_list_is_cut_to_chat_models(provider_id, payload, expected):
    assert discovery.chat_models(provider(provider_id), payload) == expected


def test_a_list_that_is_not_one_is_an_error():
    with pytest.raises(ValueError):
        discovery.chat_models(provider("groq"), {"data": "nope"})


async def test_the_model_list_offers_a_keyed_provider_its_own_models(client, monkeypatch):
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {"groq_key": "k-groq"})
    seen = []

    def handler(url, kwargs):
        seen.append((url, (kwargs.get("headers") or {}).get("Authorization")))
        if url == "https://api.groq.com/openai/v1/models":
            return FakeResponse({"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "whisper-large-v3"}]})
        return FakeResponse({}, status_code=404)

    install_httpx(monkeypatch, handler)
    body = (await client.get("/api/models")).json()

    assert body["providers"]["groq"] == {"available": True, "models": ["groq.llama-3.3-70b-versatile"],
                                         "embedding_models": []}
    assert ("https://api.groq.com/openai/v1/models", "Bearer k-groq") in seen
    assert "groq.llama-3.3-70b-versatile" in body["all_available"]
    # No key, no call: the others offer their fallbacks and say they are not set up.
    assert body["providers"]["mistral"]["available"] is False
    assert body["providers"]["mistral"]["models"] == [f"mistral.{m}" for m in provider("mistral").fallback]
    assert not [u for u, _ in seen if "mistral" in u]


async def test_a_list_that_cannot_be_read_falls_back(client, monkeypatch):
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {"zai_key": "k-zai"})
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({}, status_code=404))
    zai = (await client.get("/api/models")).json()["providers"]["zai"]
    assert zai["available"] is True
    assert zai["models"] == [f"zai.{m}" for m in provider("zai").fallback]


async def test_the_reasoning_lists_cover_hosted_models(client, monkeypatch):
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {"groq_key": "k", "minimax_key": "k"})

    def handler(url, kwargs):
        if "groq" in url:
            return FakeResponse({"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"}]})
        if "minimax" in url:
            return FakeResponse({"data": [{"id": "MiniMax-M2.7"}]})
        return FakeResponse({}, status_code=404)

    install_httpx(monkeypatch, handler)
    body = (await client.get("/api/models")).json()
    assert {"groq.openai/gpt-oss-120b", "groq.qwen/qwen3.8-27b"} <= set(body["reasoning_capable"])
    # gpt-oss's Off would be its Low: the screens offer no Off for it.
    assert "groq.openai/gpt-oss-120b" in body["reasoning_without_off"]
    assert "groq.qwen/qwen3.8-27b" not in body["reasoning_without_off"]
    # MiniMax always thinks and takes no knob: no control at all.
    assert "minimax.MiniMax-M2.7" not in body["reasoning_capable"]


# ── prices ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,rates", [
    ("groq.openai/gpt-oss-120b", (0.15, 0.60)),
    ("zai.glm-5.3", (1.40, 4.40)),
    ("zai.glm-5.3-flash", (0.15, 0.50)),        # the longest key wins, not `zai.glm-5.3`
    ("moonshot.kimi-k3", (3.00, 15.00)),
    ("fireworks.accounts/fireworks/models/glm-5p3", (1.40, 4.40)),
])
def test_a_hosted_model_is_priced_by_its_routed_id(model, rates):
    from core.model_pricing import DEFAULT_MODEL_PRICING
    from core.usage_tracker import _resolve_pricing_entry

    entry = _resolve_pricing_entry(model, DEFAULT_MODEL_PRICING)
    assert entry is not None and (entry["input_per_1m"], entry["output_per_1m"]) == rates
    assert entry["provider"] == model.split(".", 1)[0]


@pytest.mark.parametrize("model", ["nvidia.moonshotai/kimi-k3", "openrouter.openai/gpt-oss-120b", "kimi-k3"])
def test_a_host_that_sets_no_price_of_ours_is_unpriced(model):
    """NVIDIA's catalogue is a free trial; OpenRouter prices each model itself. Never a guess (F63)."""
    from core.model_pricing import DEFAULT_MODEL_PRICING
    from core.usage_tracker import _resolve_pricing_entry

    assert _resolve_pricing_entry(model, DEFAULT_MODEL_PRICING) is None


# ── an account out of money (F442) ───────────────────────────────────────────

_MOONSHOT_BROKE = ('{"error":{"message":"Your account org-x <ak-y> is suspended due to insufficient balance, '
                   'please recharge your account or check your plan and billing details","type":"exceeded_current_quota_error"}}')


@pytest.mark.parametrize("status,body", [
    (429, _MOONSHOT_BROKE),                                                         # Moonshot, measured live
    (429, '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'),
    (400, '{"code":"Arrearage","message":"Access denied, please make sure your account is in good standing."}'),
])
def test_a_balance_refusal_is_a_quota_one(status, body):
    assert llm.is_quota_refusal(status, body)
    assert llm.classify_failure(status, body) == llm.QUOTA_EXHAUSTED


def test_a_rate_limit_is_still_a_rate_limit():
    body = '{"error":{"message":"Rate limit reached for requests per minute. Please try again in 2s."}}'
    assert not llm.is_quota_refusal(429, body)
    assert llm.classify_failure(429, body) == llm.RATE_LIMITED


async def test_a_provider_out_of_balance_is_asked_once(monkeypatch, no_usage):
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        return FakeResponse(status_code=429, text=_MOONSHOT_BROKE)

    install_httpx(monkeypatch, handler)
    with pytest.raises(llm.LLMError) as caught:
        await _ask("moonshot.kimi-k2.6")
    assert len(calls) == 1
    assert caught.value.code == llm.QUOTA_EXHAUSTED and not caught.value.retryable


async def test_a_refused_key_is_said_on_its_card(client, monkeypatch):
    """A 401 is not hidden behind the fallback list: the card says the key was refused (cloud's card does)."""
    import core.routes.data as data

    monkeypatch.setattr(data, "load_settings", lambda: {"groq_key": "k-bad"})
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({"error": "invalid api key"}, status_code=401))
    groq = (await client.get("/api/models")).json()["providers"]["groq"]
    assert groq == {"available": False, "models": [], "embedding_models": [], "error": "Groq refused this key (401)"}
