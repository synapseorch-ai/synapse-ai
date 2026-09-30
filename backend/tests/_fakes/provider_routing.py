"""
Where a model id goes: recorded with every provider call stubbed (PF1, D156).

The provider table (`core.providers.registry`) replaced four hand-kept prefix
chains, and it had to route every id exactly as they did. This records, for one
id, everything the chains decided — the provider and mode, which `call_*` the
chat path reached and with what, which `_embed_*` `embed_batch` reached and with
what, and the provider the usage log was given — so the recording taken from the
chains (`fixtures/providers/routing_golden.json`) can be compared with the
table's.

Nothing leaves the process: every `call_*` and `_embed_*` is replaced, Ollama's
inline HTTP goes through `install_httpx`, and `usage_tracker.log_usage` is
stubbed. Run it only under pytest, whose conftest points the store at the
test's own directory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from _fakes.fake_http import FakeResponse, install_httpx

GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "providers" / "routing_golden.json"

#: A distinct value per setting, so a recording shows which one a call was given.
SETTINGS: dict[str, Any] = {
    "openai_key": "k-openai",
    "anthropic_key": "k-anthropic",
    "gemini_key": "k-gemini",
    "grok_key": "k-grok",
    "deepseek_key": "k-deepseek",
    "openai_compatible_base_url": "http://oaic.test",
    "openai_compatible_key": "k-oaic",
    "local_compatible_base_url": "http://locv1.test",
    "local_compatible_key": "k-locv1",
    "model_gateway_base_url": "http://gw.test",
    "model_gateway_key": "k-gw",
    "aws_region": "us-west-2",
    "ollama_base_url": "http://ollama.test:11434",
    "huggingface_token": "k-hf",
}

TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Look a thing up.",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
}}]

#: Every chat call a model id can reach. `call_cli_provider` answers a 3-tuple.
CHAT_CALLS = (
    "call_openai", "call_anthropic", "call_gemini", "call_bedrock", "call_grok", "call_deepseek",
    "call_model_gateway", "call_v1_compatible", "call_cli_provider", "call_huggingface",
)
EMBED_CALLS = ("_embed_openai", "_embed_v1_compatible", "_embed_gemini", "_embed_bedrock", "_embed_ollama")

# ── the corpus ────────────────────────────────────────────────────────────────
# Every id Settings offers (`core/routes/data.py`: each provider's fallback, its
# no-key list and its error list), the discovery tests' catalogues, every id the
# routing tests pin, one id per prefix, the near-misses the routing rules were
# written to avoid, embedding ids, and ids in mixed case.

_OFFERED_CHAT = [
    # data.py fallbacks
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-pro-latest", "gemini-flash-latest",
    "claude-sonnet-4-20250514", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
    "gpt-4o", "gpt-4-turbo", "gpt-4o-mini",
    "bedrock.anthropic.claude-3-5-sonnet-20240620-v1:0", "bedrock.anthropic.claude-3-sonnet-20240229-v1:0",
    "grok-3", "grok-3-mini", "grok-2-1212", "grok-2-vision-1212",
    "deepseek-chat", "deepseek-reasoner",
    # the CLI lists
    "cli.claude.claude-sonnet-4-6", "cli.claude.claude-opus-4-6", "cli.claude.claude-haiku-4-5-20251001",
    "cli.claude.claude-sonnet-4-5-20250929", "cli.claude.claude-opus-4-5-20251101",
    "cli.copilot.claude-sonnet-4-5", "cli.copilot.gpt-4o", "cli.copilot.gpt-4.1", "cli.copilot.o3",
    "cli.copilot.o4-mini",
]

_OFFERED_EMBEDDING = [
    # OpenAI: no key, live list, error
    "text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002",
    # Gemini: no key, live list (anything with embedContent), error
    "text-embedding-004", "gemini-embedding-001", "embedding-001",
    # Bedrock: no key (unprefixed), live, error
    "amazon.titan-embed-text-v1", "amazon.titan-embed-text-v2:0",
    "bedrock.amazon.titan-embed-text-v1", "bedrock.amazon.titan-embed-text-v2:0",
    "bedrock.cohere.embed-english-v3", "bedrock.cohere.embed-multilingual-v3",
    # Ollama, OpenAI-compatible, local, gateway
    "ollama.nomic-embed-text", "ollama.mxbai-embed-large",
    "oaic.text-embedding-3-small", "locv1.nomic-embed-text", "synapse.titan-embed-v2",
    # probe_embedding_dim's own table
    "gemini-embedding-004", "nomic-embed-text", "mxbai-embed-large", "all-minilm",
]

_GEMINI_CATALOGUE = [
    "antigravity-preview-05-2026", "deep-research-preview-04-2026", "gemini-2.5-computer-use-preview-10-2025",
    "gemini-2.5-flash-image", "gemini-2.5-flash-preview-tts", "gemini-3-flash-preview", "gemini-3-pro-image",
    "gemini-3.1-flash-lite", "gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools", "gemini-3.5-flash",
    "gemini-3.5-transcribe", "gemini-3.8-flash", "gemini-3.8-live", "gemini-omni-1.1-flash",
    "gemini-robotics-er-2-preview", "gemma-4-26b-a4b-it", "gemma-4-31b-it", "lyria-3-clip-preview", "lyria-3.5",
    "nano-banana-pro-preview", "aqa", "veo-3.1-generate-preview",
]

_OPENAI_ACCOUNT = [
    "gpt-5", "gpt-5-mini", "gpt-5-chat-latest", "gpt-4.1", "gpt-3.5-turbo", "chatgpt-4o-latest",
    "o1", "o3", "o3-mini", "o4-mini", "gpt-4o-mini-tts", "gpt-realtime", "gpt-image-1", "o1-pro", "gpt-5-codex",
    "o3-deep-research", "whisper-1", "tts-1", "dall-e-3", "omni-moderation-latest", "babbage-002",
]

_PINNED_BY_TESTS = [
    "claude-opus-4-8", "gemini-2.0-flash", "gemini-2.0", "grok-2", "bedrock.anthropic.claude", "bedrock.x",
    "bedrock.claude", "synapse.nova-pro", "synapse.nova-micro", "oaic.some-model", "oaic.model", "locv1.model",
    "cli.claude.sonnet", "cli.codex.gpt", "hf.Qwen/Qwen2.5", "hf.model", "ollama.llama3", "mistral", "unknown",
    "claude-x",
]

_ONE_PER_PREFIX = [
    "cli.claude", "cli.claude.opus-thinking", "cli.gemini", "cli.gemini.pro", "cli.gemini.flash",
    "cli.codex", "cli.codex.gpt-5", "cli.copilot", "cli.copilot.x", "cli.other",
    "bedrock.us.anthropic.claude-sonnet-4-5-20250929-v1:0", "bedrock.amazon.nova-pro-v1:0", "bedrockish",
    "synapse.claude-sonnet", "oaic.Some-Model", "locv1.Qwen/Qwen3", "ollama.qwen3:8b", "hf.meta/Llama-3",
    "grok-4", "deepseek-r1", "chatgpt-5", "gemma-3-4b", "lyria-2", "claude-sonnet-4-5", "gemma-3",
]

_NEAR_MISSES = [
    "", "orca-mini", "openchat", "olmo2", "llama3", "qwen3:8b", "o", "o-mini", "gptx", "claud", "gem",
    "ollama", "oaic", "locv1", "synapse", "hf", "cli", "text-embed", "embedding", "nomic-embed-text:latest",
    "cli.claudex", "cli.x", "gpt-oss", "ollama.gpt-oss", "synapse.gpt-oss", "oaic.o3", "gemma3", "bedrockx",
    "deepseek-r1:7b", "hf.", " gpt-4o", "text-embedding-3", "embedding-gecko-001",
]

#: The table routes an id the same whatever its case; the chains did not. The
#: detectors lower-cased, but the chat dispatch matched case-sensitively (all but
#: OpenAI), so a cloud id in mixed case got mode "cloud" and then "Unknown cloud
#: model selected", and `embed_batch` kept its routing prefix. The one intended
#: change (D156): each now routes as its lower-case form does, keeping its case
#: after the prefix. Every lower-case form is in the corpus.
MIXED_CASE = [
    "Claude-Sonnet-4-5", "Gemini-2.5-Pro", "GEMMA-3", "Grok-3", "DeepSeek-Chat", "Bedrock.anthropic.claude",
    "Synapse.nova-pro", "OAIC.model", "LOCV1.model",
]

#: Mixed case that routes as it did: OpenAI's dispatch already ignored case,
#: and the Ollama, HuggingFace and CLI branches (moved verbatim) still hand
#: their id on as written.
_MIXED_CASE_AS_BEFORE = ["GPT-4o", "O3-mini", "Ollama.llama3", "HF.model", "CLI.claude.sonnet"]

CORPUS: list[str] = list(dict.fromkeys(
    _OFFERED_CHAT + _OFFERED_EMBEDDING + _GEMINI_CATALOGUE + _OPENAI_ACCOUNT + _PINNED_BY_TESTS
    + _ONE_PER_PREFIX + _NEAR_MISSES + MIXED_CASE + _MIXED_CASE_AS_BEFORE
))

#: `generate_response_ex` is handed a mode, and the mode picks the branch family
#: whatever the id says. Every caller derives it from the id, but tests (and
#: compaction, which passes its caller's) hand one over, so it stays honoured.
MODE_OVERRIDES: list[tuple[str, str]] = [
    ("cloud", "weirdmodel"), ("cloud", "llama3"), ("cloud", "cli.claude"), ("cloud", "hf.x"),
    ("cloud", "ollama.llama3"), ("cloud", "bedrock.x"), ("bedrock", "gpt-4o"), ("bedrock", "bedrock.claude"),
    ("local", "gpt-4o"), ("local", "claude-x"), ("local", "ollama.llama3"), ("cli", "gpt-4o"),
    ("cli", "cli.gemini.pro"), ("hf", "gpt-4o"), ("hf", "hf.model"), ("", "ollama.llama3"), ("", "gpt-4o"),
]


# ── recording ─────────────────────────────────────────────────────────────────

def _plain(value: Any, settings: dict) -> Any:
    """A JSON-stable picture of an argument."""
    if value is settings:
        return "<settings>"
    if isinstance(value, dict):
        if set(value) >= set(settings) and all(value.get(k) == v for k, v in settings.items()):
            extra = {k: _plain(v, settings) for k, v in value.items() if k not in settings}
            return {"<settings+>": extra}
        return {str(k): _plain(v, settings) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(t, dict) and t.get("type") == "function" for t in value):
            return "<tools>"
        return [_plain(v, settings) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _install(monkeypatch, llm, calls: list, settings: dict) -> None:
    from core import usage_tracker
    from core.llm_providers import LLMResult

    def _recorder(name):
        async def _call(*args, **kwargs):
            calls.append({
                "call": name,
                "args": [_plain(a, settings) for a in args],
                "kwargs": {k: _plain(v, settings) for k, v in sorted(kwargs.items())},
            })
            if name == "call_cli_provider":
                return ("ok", 1, 1)
            if name.startswith("_embed_"):
                return [[0.5, 0.5]]
            return LLMResult("ok", 1, 1)
        return _call

    for name in CHAT_CALLS + EMBED_CALLS:
        monkeypatch.setattr(llm, name, _recorder(name))
    # With no `ollama_base_url` setting Ollama's address comes from here; the
    # recording must not depend on the machine it was taken on.
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    async def _log_usage(**kwargs):
        calls.append({"log_usage": kwargs.get("provider")})

    monkeypatch.setattr(usage_tracker, "log_usage", _log_usage)

    def _ollama(url, kwargs):
        body = kwargs.get("json") or {}
        calls.append({"http": url, "model": body.get("model"), "body_keys": sorted(body),
                      "options": body.get("options"), "think": body.get("think")})
        if url.endswith("/api/chat"):
            return FakeResponse({"message": {"content": "ok"}, "done_reason": "stop",
                                 "prompt_eval_count": 1, "eval_count": 1})
        return FakeResponse({"response": "ok", "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1})

    install_httpx(monkeypatch, _ollama)


async def _chat(llm, model: str, settings: dict, calls: list, mode: str | None = None, **kw) -> dict:
    calls.clear()
    if mode is None:
        mode = llm.detect_mode_from_model(model)
    try:
        await llm.generate_response_ex("hi", "sys", mode, model, settings, **kw)
        outcome: Any = "ok"
    except Exception as e:  # the recording is what happened, failures included
        outcome = {"raised": type(e).__name__, "message": str(e)}
    return {"calls": list(calls), "outcome": outcome}


async def _embed(llm, model: str, settings: dict, calls: list) -> dict:
    calls.clear()
    try:
        vectors = await llm.embed_batch(["t"], model, settings)
        outcome: Any = {"vectors": len(vectors), "dims": len(vectors[0]) if vectors else 0}
    except Exception as e:
        outcome = {"raised": type(e).__name__, "message": str(e)}
    return {"calls": list(calls), "outcome": outcome}


async def record(model: str, monkeypatch) -> dict:
    """Everything the router decides for `model`, as plain JSON."""
    import core.llm_providers as llm
    from core.images import model_can_see_images

    calls: list = []
    settings = dict(SETTINGS)
    _install(monkeypatch, llm, calls, settings)
    return {
        "provider": llm.detect_provider_from_model(model),
        "mode": llm.detect_mode_from_model(model),
        "images": model_can_see_images(model),
        # No settings at all: pins what each branch reads a missing key as
        # (`None` from `.get(k)`, "" from `.get(k, "")`).
        "chat_bare": await _chat(llm, model, {}, calls),
        # With tools, prompt blocks and no cap: the tools path everywhere, and
        # the providers that are sent a cap only when one is set are sent none.
        "chat_tools": await _chat(llm, model, settings, calls, tools=TOOLS,
                                  prompt_blocks=["block"], volatile_tail="tail"),
        # No tools, an agent's own cap: Ollama's /api/generate path, and every
        # provider's cap handling (Bedrock's `bedrock_max_tokens` override).
        "chat_capped": await _chat(llm, model, settings, calls, max_output_tokens=1000),
        "embed": await _embed(llm, model, settings, calls),
        "embed_dims": await _embed(llm, model, {**settings, "__embed_output_dim": 256}, calls),
    }


async def record_forced(mode: str, model: str, monkeypatch) -> dict:
    """The chat path when the caller hands over `mode` rather than deriving it."""
    import core.llm_providers as llm

    calls: list = []
    settings = dict(SETTINGS)
    _install(monkeypatch, llm, calls, settings)
    return await _chat(llm, model, settings, calls, mode=mode, tools=TOOLS)


def forced_key(mode: str, model: str) -> str:
    return f"mode={mode}|{model}"


async def record_all(monkeypatch) -> dict:
    recordings = {model: await record(model, monkeypatch) for model in CORPUS}
    for mode, model in MODE_OVERRIDES:
        recordings[forced_key(mode, model)] = await record_forced(mode, model, monkeypatch)
    return recordings


def load_golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def write_golden(recordings: dict) -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(recordings, indent=1, sort_keys=True) + "\n", encoding="utf-8")
