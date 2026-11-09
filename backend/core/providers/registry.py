"""
Every model provider the engine calls, in one table (PF1, D156).

A model id names its provider by its shape — `gpt-4o` is OpenAI's, `oaic.qwen3`
goes to the configured OpenAI-compatible server, a bare `mistral` is Ollama's.
That decision used to be made in six places, each with its own list: the mode
and provider detectors, the chat dispatch in `generate_response_ex`,
`embed_batch`, `model_readiness` and `images.model_can_see_images`. They fell out
of step — Settings offered OpenAI's and Gemini's embedding models, and
`embed_batch` sent them to Ollama (F322). Now they all read this table.

**Adding a provider** is one entry here plus its call functions: a chat adapter
(and an embed adapter, if it embeds) taking `(call, spec)` / `(texts, model,
settings, spec)`, named by `chat` / `embed`. A provider that embeds also names
the adapter that raises instead of answering zeros (`embed_ex`, which takes
`input_type=` and `dims=` too). Adapters live in `core.llm_providers` unless the
name says `module:function`.

The table is data and the standard library only, so `core.images` and
`core.model_readiness` read it without loading any provider SDK, and
`core.llm_providers` imports it with no cycle. Adapters are named rather than
held, and looked up when called, so a test that patches `llm.call_openai` or an
adapter still reaches the call.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Callable

#: Where an adapter named without a module lives.
ADAPTERS = "core.llm_providers"

#: The modes `generate_response_ex` dispatches through its cloud family — the
#: ones that log an empty answer's usage and say "Cloud API Error".
CLOUD_MODES = frozenset({"cloud", "bedrock"})


@dataclass(frozen=True)
class Capabilities:
    #: "native": the model is sent a `tools=` parameter. "prompt": it is not,
    #: so the agent loop describes the tools in the system prompt instead.
    tools: str
    #: Whether sending an image does anything. The blind ones drop it — some
    #: with a warning, the CLIs silently — and the person watched their
    #: attachment go in and be answered as if it were not there.
    images: bool
    #: Whether some of its models think on a channel of their own. Which models,
    #: and how they are asked, is `core.reasoning`'s to say.
    reasoning: bool
    #: Whether its server reranks (`/v1/rerank`): our gateway only. The
    #: rerank-only vendors (Cohere, Voyage, Jina, TEI) serve no chat, so they
    #: are `core.knowledge.rerankers`' table, not rows here (B2a, D163).
    rerank: bool = False


@dataclass(frozen=True)
class ProviderSpec:
    #: Written into every usage log. Never rename one.
    id: str
    label: str
    #: The branch family `generate_response_ex` dispatches it through, and what
    #: `detect_mode_from_model` answers: cloud | bedrock | cli | hf | local.
    mode: str
    #: Lower-case prefixes; an id matches on any of them.
    prefixes: tuple[str, ...] = ()
    #: Matched from the start of the lower-cased id, beside the prefixes.
    pattern: re.Pattern[str] | None = None
    #: The routing prefix the provider's server does not know, removed before
    #: the call (by length, so the rest keeps its case).
    strip: str | None = None
    #: Every secret setting it reads (each is in `core.config.SECRET_SETTINGS`).
    keys: tuple[str, ...] = ()
    #: The setting holding its server's address, if it has one.
    address: str | None = None
    #: What `/api/status` checks before anyone types: "key" (the first of
    #: `keys` is saved), "address" (`address` is saved), or None — nothing, or
    #: (Ollama) a look at its model list.
    readiness: str | None = None
    #: Adapter names. `embed` None: `embed_batch` has nothing to call.
    chat: str = ""
    embed: str | None = None
    #: The embed adapter that raises instead of answering zeros, and takes the
    #: query/document kind and a width (`embed_batch_ex`, B2a). Named exactly
    #: when `embed` is.
    embed_ex: str | None = None
    #: How a failure that is not already an `LLMError` is worded. None: the
    #: adapter words its own (Ollama's says where it looked).
    error: str | None = None
    caps: Capabilities = Capabilities(tools="native", images=True, reasoning=False)
    #: A hosted OpenAI-style API at a fixed address (PROV1, D245): chat is
    #: `{base_url}/chat/completions`, its model list `{base_url}/models`. None:
    #: the address is a setting (`address`) or the adapter knows its own.
    base_url: str | None = None
    #: Bare model ids offered when there is no key or its list cannot be read.
    fallback: tuple[str, ...] = ()

    @property
    def hosted(self) -> bool:
        """A hosted OpenAI-style API with its own key (PROV1): one adapter serves them all."""
        return self.base_url is not None

    @property
    def embeddings(self) -> bool:
        return self.embed is not None

    def matches(self, lowered: str) -> bool:
        return lowered.startswith(self.prefixes) or bool(self.pattern and self.pattern.match(lowered))

    def bare(self, model: str) -> str:
        """`model` without this provider's routing prefix."""
        if self.strip and model.lower().startswith(self.strip):
            return model[len(self.strip):]
        return model


_NATIVE = Capabilities(tools="native", images=True, reasoning=True)


def _cli(id: str, label: str, prefix: str, reasoning: bool = False) -> ProviderSpec:
    return ProviderSpec(
        id=id, label=label, mode="cli", prefixes=(prefix,), chat="_chat_cli", error="CLI Provider Error",
        caps=Capabilities(tools="prompt", images=False, reasoning=reasoning),
    )


#: In match order: the first entry an id matches is its provider.
PROVIDERS: tuple[ProviderSpec, ...] = (
    # CLI sessions, on the person's own CLI login. The specific ones first.
    _cli("anthropic_cli", "Claude CLI", "cli.claude", reasoning=True),  # takes --effort
    _cli("gemini_cli", "Gemini CLI", "cli.gemini"),
    _cli("codex_cli", "Codex CLI", "cli.codex"),
    _cli("github_copilot_cli", "GitHub Copilot CLI", "cli.copilot"),
    _cli("cli", "CLI", "cli."),
    ProviderSpec(
        id="openai", label="OpenAI", mode="cloud",
        # `gpt*`, `chatgpt*` and the o-series (`o1`, `o3-mini`, `o4-mini`…).
        # Only `gpt*` used to count, so the o-series fell through to Ollama
        # (F62). `o` + a digit, not a bare `o`, so `orca-mini` stays local.
        # And the embedding models Settings offers, which went to Ollama (F322):
        # `text-embedding-3-*` and `-ada-*` — Google's are `text-embedding-00N`.
        prefixes=("gpt", "chatgpt", "text-embedding-3-", "text-embedding-ada-"), pattern=re.compile(r"o\d"),
        keys=("openai_key",), readiness="key",
        chat="_chat_openai", embed="_embed_via_openai", embed_ex="_embed_ex_openai",
        error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        id="anthropic", label="Anthropic", mode="cloud", prefixes=("claude",),
        keys=("anthropic_key",), readiness="key",
        chat="_chat_anthropic", error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        id="gemini", label="Gemini", mode="cloud",
        # Gemini, Gemma and Lyria: the ids Google serves. And its embedding
        # models that are not `gemini-embedding-*`: `text-embedding-004`,
        # `embedding-001` — offered by Settings, and sent to Ollama (F322).
        prefixes=("gemini", "gemma", "lyria"), pattern=re.compile(r"(?:text-)?embedding-\d{3}(?!\d)"),
        keys=("gemini_key",), readiness="key",
        chat="_chat_gemini", embed="_embed_via_gemini", embed_ex="_embed_ex_gemini",
        error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        id="bedrock", label="Bedrock", mode="bedrock", prefixes=("bedrock",), strip="bedrock.",
        # Bedrock also authenticates through boto's own credential chain, so it
        # is never called "not ready" on a guess.
        keys=("bedrock_api_key", "aws_secret_access_key", "aws_session_token"),
        chat="_chat_bedrock", embed="_embed_via_bedrock", embed_ex="_embed_ex_bedrock", error="Cloud API Error",
        caps=Capabilities(tools="prompt", images=True, reasoning=True),
    ),
    ProviderSpec(
        id="grok", label="Grok", mode="cloud", prefixes=("grok",),
        keys=("grok_key",), readiness="key",
        chat="_chat_grok", error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        id="deepseek", label="DeepSeek", mode="cloud", prefixes=("deepseek",),
        keys=("deepseek_key",), readiness="key",
        chat="_chat_deepseek", error="Cloud API Error",
        caps=Capabilities(tools="native", images=False, reasoning=True),
    ),
    ProviderSpec(
        id="model_gateway", label="model gateway", mode="cloud", prefixes=("synapse.",), strip="synapse.",
        keys=("model_gateway_key",), address="model_gateway_base_url", readiness="address",
        chat="_chat_model_gateway", embed="_embed_via_v1", embed_ex="_embed_ex_v1",
        error="Cloud API Error", caps=Capabilities(tools="native", images=True, reasoning=True, rerank=True),
    ),
    ProviderSpec(
        id="openai_compatible", label="OpenAI-compatible", mode="cloud", prefixes=("oaic.",), strip="oaic.",
        keys=("openai_compatible_key",), address="openai_compatible_base_url", readiness="address",
        chat="_chat_v1_compatible", embed="_embed_via_v1", embed_ex="_embed_ex_v1",
        error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        # An OpenAI v1 server on this machine (LM Studio, vLLM). Its mode is
        # "cloud" because the cloud family dispatches it, as it does `oaic.`;
        # "local" is the Ollama branch (F114).
        id="local_compatible", label="local OpenAI-compatible", mode="cloud", prefixes=("locv1.",),
        strip="locv1.",
        keys=("local_compatible_key",), address="local_compatible_base_url", readiness="address",
        chat="_chat_v1_compatible", embed="_embed_via_v1", embed_ex="_embed_ex_v1",
        error="Cloud API Error", caps=_NATIVE,
    ),
    ProviderSpec(
        id="ollama", label="Ollama", mode="local", prefixes=("ollama.",), strip="ollama.",
        address="ollama_base_url",
        chat="_chat_ollama", embed="_embed_via_ollama", embed_ex="_embed_ex_ollama",
        caps=Capabilities(tools="native", images=True, reasoning=True),  # `think`
    ),
    ProviderSpec(
        id="huggingface", label="HuggingFace", mode="hf", prefixes=("hf.",), strip="hf.",
        keys=("huggingface_token",),
        chat="_chat_huggingface", error="HuggingFace Provider Error",
        caps=Capabilities(tools="prompt", images=False, reasoning=False),
    ),
    # Hosted OpenAI-style APIs, each with its own key (PROV1, D245). Every one
    # is named by a dotted prefix, stripped before the call: their own ids
    # (`mistral-large-latest`, `meta-llama/…`, `deepseek/…`) would otherwise
    # land on Ollama or on DeepSeek. One adapter serves them all.
    *(
        ProviderSpec(
            id=id, label=label, mode="cloud", prefixes=(f"{id}.",), strip=f"{id}.",
            keys=(f"{id}_key",), readiness="key", chat="_chat_hosted_v1", error="Cloud API Error",
            caps=Capabilities(tools="native", images=images, reasoning=True),
            base_url=base_url, fallback=fallback,
        )
        # Fallbacks are each provider's current chat models, from its own
        # documentation on 2026-10-10. Images: whether its flagship models see;
        # a model of theirs that does not is refused by the provider, once.
        for id, label, base_url, images, fallback in (
            ("moonshot", "Moonshot (Kimi)", "https://api.moonshot.ai/v1", True,
             ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code")),
            ("mistral", "Mistral", "https://api.mistral.ai/v1", True,
             ("mistral-large-latest", "mistral-medium-latest", "mistral-small-latest", "ministral-14b-latest")),
            ("nvidia", "NVIDIA", "https://integrate.api.nvidia.com/v1", True,
             ("moonshotai/kimi-k3", "z-ai/glm-5.3", "nvidia/nemotron-3-super-120b-a12b", "openai/gpt-oss-20b")),
            ("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", True,
             ("openai/gpt-oss-120b", "moonshotai/kimi-k3", "z-ai/glm-5.3")),
            ("qwen", "Qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", True,
             ("qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash")),
            ("zai", "Z.ai", "https://api.z.ai/api/paas/v4", False,
             ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-4.7-flash")),
            ("groq", "Groq", "https://api.groq.com/openai/v1", False,
             ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b", "llama-3.3-70b-versatile")),
            ("cerebras", "Cerebras", "https://api.cerebras.ai/v1", False,
             ("gpt-oss-120b", "qwen-3.8-27b")),
            ("together", "Together AI", "https://api.together.xyz/v1", True,
             ("openai/gpt-oss-120b", "moonshotai/Kimi-K3", "zai-org/GLM-5.3", "deepseek-ai/DeepSeek-V4.1-Flash")),
            ("fireworks", "Fireworks", "https://api.fireworks.ai/inference/v1", True,
             ("accounts/fireworks/models/gpt-oss-120b", "accounts/fireworks/models/kimi-k3",
              "accounts/fireworks/models/glm-5p3", "accounts/fireworks/models/deepseek-v4p1-flash")),
            ("minimax", "MiniMax", "https://api.minimax.io/v1", True,
             ("MiniMax-M3", "MiniMax-M2.7")),
        )
    ),
)

#: The hosted entries, in table order (PROV1): what `/api/models` lists and
#: cloud's discovery fetches, one loop for them all.
HOSTED: tuple[ProviderSpec, ...] = tuple(spec for spec in PROVIDERS if spec.hosted)

_BY_ID = {spec.id: spec for spec in PROVIDERS}

#: An id nothing else claims is an Ollama model named bare (`mistral`), as a
#: fresh install's default always was. Every reader of a provider's facts (its
#: cost, its images) still reads it for any id; one that names a provider no
#: entry is, by a word and a dot, is never called (`unknown_said`, F1491).
FALLBACK = _BY_ID["ollama"]

#: An id naming its provider by a word and a dot (`anthropic.claude-sonnet-4-6`, Bedrock's shape; `us.amazon.…`).
#: Not a version's dot (`llama3.2`, `qwen2.5:7b`: a digit follows), a tag's (`x:v1.2`) or a path's
#: (`registry.ollama.ai/library/llama3`), which are Ollama's.
_NAMED_BY_A_WORD = re.compile(r"([a-z][a-z0-9_-]*)\.[a-z]")

#: How `unknown_said` begins, so a reader can find its sentence inside a recorded reason (`model_failures`).
UNKNOWN_LEAD = "Unknown model "


def provider(provider_id: str) -> ProviderSpec:
    return _BY_ID[provider_id]


def unclaimed(model: str | None) -> str | None:
    """The word an id names its provider by, when no provider is that word (F1491): `anthropic` for
    `anthropic.claude-sonnet-4-6`.

    `FALLBACK` ran such an id as an Ollama model, and the person who named Anthropic's model was told "Ollama is not
    reachable" (TRI2's run B). None for an id a provider claims, and for a bare name (`mistral`, a fresh install's
    default), a version (`llama3.2`) or a path, which stay Ollama's."""
    lowered = (model or "").strip().lower()
    named = _NAMED_BY_A_WORD.match(lowered)
    if named is None or "/" in lowered or any(spec.matches(lowered) for spec in PROVIDERS):
        return None
    return named.group(1)


def nearest(model: str) -> str | None:
    """The id a provider claims that an `unclaimed` one most likely means: its words dropped from the front until a
    provider claims what is left (`us.anthropic.claude-sonnet-4-6` → `claude-sonnet-4-6`). None when what is left
    is a bare name (`amazon.nova-pro-v1:0` → `nova-pro-v1:0`, which only Ollama would take)."""
    whole = rest = (model or "").strip()
    while unclaimed(rest):
        rest = rest.split(".", 1)[1]
    return rest if rest != whole and provider_for(rest) is not FALLBACK else None


def unknown_said(model: str | None) -> str | None:
    """What is said of an `unclaimed` id instead of calling anything, naming the id it most likely means; None for
    any other id."""
    word = unclaimed(model)
    if word is None:
        return None
    model = (model or "").strip()
    said = f'{UNKNOWN_LEAD}{model}: no provider\'s model ids begin "{word}.".'
    near = nearest(model)
    if near:
        return f"{said} Did you mean {near} ({provider_for(near).label})? A Bedrock model is written bedrock.<its id>."
    return f"{said} A Bedrock model is written bedrock.{model}, an Ollama one ollama.{model}."


def provider_for(model: str | None) -> ProviderSpec:
    """The provider of a model id, whatever its case."""
    lowered = (model or "").lower()
    if lowered:
        for spec in PROVIDERS:
            if spec.matches(lowered):
                return spec
    return FALLBACK


def branch_for(mode: str | None, model: str) -> ProviderSpec | None:
    """The entry `generate_response_ex` dispatches `model` to under `mode`.

    The caller's mode picks the family, as it always has — every caller derives
    it from the id, but a test or a compaction may hand one over. Under a cloud
    mode an id whose provider is not a cloud one answers None ("Unknown cloud
    model"); under any mode that is not cli or hf, it is Ollama's.
    """
    spec = provider_for(model)
    if mode in CLOUD_MODES:
        return spec if spec.mode in CLOUD_MODES else None
    if mode == "cli":
        return spec if spec.mode == "cli" else _BY_ID["cli"]
    if mode == "hf":
        return _BY_ID["huggingface"]
    return FALLBACK


def resolve(name: str) -> Callable[..., Any]:
    """The adapter a table entry names, looked up now."""
    module, _, attr = name.rpartition(":")
    return getattr(importlib.import_module(module or ADAPTERS), attr)
