"""
"Which AI should Synapse use?" — the one question an install cannot skip past.

Returns the settings rows `synapse setup --apply` writes: the model, its key,
and `mode`, named exactly as the engine reads them (`backend/core/config.py`).
A key is checked the only way that proves it works — by listing that
provider's models with it — and the list it returns is the model question.

Ported from setup.py's `ask_llm`, with four of its bugs left behind: Grok's key
is `grok_key` (it wrote `xai_key`, which nothing reads); an Ollama model is
stored as `ollama.<name>` (bare, `gemma3` routed to Gemini); the OpenAI list
keeps every chat model rather than only `gpt-4*`/`gpt-3.5*`; and an
OpenAI-compatible pick is added to `openai_compatible_models`, so it is in the
app's model list afterwards.
"""
from __future__ import annotations

import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import net
from .widgets import BACK, Asker, Option

# ── what is on this computer already ─────────────────────────────────────────

CLAUDE_CLI_MODELS = [
    "cli.claude.claude-sonnet-4-6", "cli.claude.claude-opus-4-6",
    "cli.claude.claude-haiku-4-5-20251001", "cli.claude",
]
GEMINI_CLI_MODELS = ["cli.gemini.pro", "cli.gemini.flash", "cli.gemini"]
CODEX_CLI_MODELS = ["cli.codex"]


def ollama_models(base_url: str = "http://127.0.0.1:11434") -> list[str]:
    try:
        data = net.get_json(f"{base_url.rstrip('/')}/api/tags", timeout=3)
    except net.HttpError:
        return []
    return [m["name"] for m in data.get("models", []) if m.get("name")]


@dataclass
class Found:
    ollama: list[str]
    claude: bool
    gemini: bool
    codex: bool


def detect() -> Found:
    return Found(
        ollama=ollama_models(),
        claude=bool(shutil.which("claude")),
        gemini=bool(shutil.which("gemini")),
        codex=bool(shutil.which("codex")),
    )


# ── listing a provider's models (which is also checking its key) ─────────────

_OPENAI_FAMILY = re.compile(r"^(gpt|chatgpt|o\d)")
_OPENAI_NOT_CHAT = re.compile(
    r"tts|transcribe|whisper|realtime|audio|image|dall-e|embedding|moderation|search"
    r"|instruct|codex|deep-research|(?:^|-)pro(?:-|$)"
)


def _ids(data: dict) -> list[str]:
    return sorted({m["id"] for m in data.get("data", []) if m.get("id")}, reverse=True)


def list_anthropic(key: str) -> list[str]:
    return _ids(net.get_json("https://api.anthropic.com/v1/models?limit=100",
                             {"x-api-key": key, "anthropic-version": "2023-06-01"}))


def list_openai(key: str) -> list[str]:
    ids = _ids(net.get_json("https://api.openai.com/v1/models",
                            {"Authorization": f"Bearer {key}"}))
    # The engine's own test (llm_providers.is_openai_chat_model), so a listed
    # model is one it routes to OpenAI and can chat with.
    return [m for m in ids if _OPENAI_FAMILY.match(m.lower()) and not _OPENAI_NOT_CHAT.search(m.lower())]


def list_gemini(key: str) -> list[str]:
    data = net.get_json("https://generativelanguage.googleapis.com/v1beta/models?"
                        + urllib.parse.urlencode({"key": key, "pageSize": 200}))
    return sorted({
        m["name"].removeprefix("models/") for m in data.get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
        and m.get("name", "").startswith("models/")
    }, reverse=True)


def list_bearer(url: str) -> Callable[[str], list[str]]:
    return lambda key: _ids(net.get_json(url, {"Authorization": f"Bearer {key}"} if key else {}))


def list_bedrock(key: str, region: str) -> list[str]:
    data = net.get_json(f"https://bedrock.{region}.amazonaws.com/foundation-models",
                        {"Authorization": f"Bearer {key}"}, timeout=30)
    return sorted({s["modelId"] for s in data.get("modelSummaries", []) if s.get("modelId")})


# A sensible first choice in each list: the model most people would pick.
_PREFERRED = {
    "anthropic": ("claude-sonnet-4", "claude-sonnet", "claude-opus"),
    "openai": ("gpt-5", "gpt-4.1", "gpt-4o"),
    "gemini": ("gemini-2.5-flash", "gemini-2.5-pro", "flash"),
    "deepseek": ("deepseek-chat",),
    "grok": ("grok-4", "grok-3"),
}
_NOT_FIRST = re.compile(r"lite|preview|exp|image|tts|audio|mini|nano|vision|embedding")


def preferred_index(provider: str, models: list[str]) -> int:
    for wanted in _PREFERRED.get(provider, ()):
        for i, m in enumerate(models):
            if wanted in m and not _NOT_FIRST.search(m):
                return i
    return 0


@dataclass
class Cloud:
    key: str                     # provider key, and its settings key prefix
    label: str
    key_setting: str
    lister: Callable[[str], list[str]]
    fallback_model: str


CLOUDS = {
    "anthropic": Cloud("anthropic", "Anthropic", "anthropic_key", list_anthropic, "claude-sonnet-4-6"),
    "openai": Cloud("openai", "OpenAI", "openai_key", list_openai, "gpt-4o"),
    "gemini": Cloud("gemini", "Google Gemini", "gemini_key", list_gemini, "gemini-2.5-flash"),
    "deepseek": Cloud("deepseek", "DeepSeek", "deepseek_key",
                      list_bearer("https://api.deepseek.com/v1/models"), "deepseek-chat"),
    "grok": Cloud("grok", "xAI Grok", "grok_key",
                  list_bearer("https://api.x.ai/v1/models"), "grok-3"),
}


# ── the question ─────────────────────────────────────────────────────────────

def ask(asker: Asker, found: Found | None = None) -> dict:
    """Ask until there is an answer. {} means "skip — choose later in Settings"."""
    found = found or detect()
    while True:
        options: list[Option] = []
        if found.ollama:
            n = len(found.ollama)
            options.append(Option("Ollama", "ollama", f"{n} model{'s' if n != 1 else ''} on this computer"))
        if found.claude:
            options.append(Option("Claude Code", "cli:claude", "found on this computer"))
        if found.gemini:
            options.append(Option("Gemini CLI", "cli:gemini", "found on this computer"))
        if found.codex:
            options.append(Option("Codex CLI", "cli:codex", "found on this computer"))
        options += [
            Option("Anthropic (Claude)", "anthropic"),
            Option("OpenAI", "openai"),
            Option("Google Gemini", "gemini"),
            Option("Other…", "other", "DeepSeek, Grok, OpenRouter, AWS Bedrock, a local server"),
            Option("Skip for now", "skip", "choose one later in Settings"),
        ]
        choice = asker.select("Which AI should Synapse use?", options,
                              default=len(options) - 1 if not asker.interactive else 0)
        answer = _answer(asker, choice, found)
        if answer is not BACK:
            return answer


def _answer(asker: Asker, choice, found: Found):
    if choice == "skip":
        return {}
    if choice == "ollama":
        return _pick_ollama(asker, found.ollama)
    if isinstance(choice, str) and choice.startswith("cli:"):
        models = {"claude": CLAUDE_CLI_MODELS, "gemini": GEMINI_CLI_MODELS,
                  "codex": CODEX_CLI_MODELS}[choice[4:]]
        model = models[0] if len(models) == 1 else asker.select(
            "Which model?", [Option(m, m) for m in models], back=True)
        return BACK if model is BACK else {"model": model, "mode": "cli"}
    if choice in CLOUDS:
        return _cloud(asker, CLOUDS[choice])
    if choice == "other":
        other = asker.select("Which one?", [
            Option("DeepSeek", "deepseek"), Option("xAI Grok", "grok"),
            Option("OpenRouter, or another OpenAI-compatible API", "oaic"),
            Option("A server on this computer or network", "locv1", "vLLM, LM Studio, llama.cpp…"),
            Option("Ollama at another address", "ollama-remote"),
            Option("AWS Bedrock", "bedrock"),
        ], back=True)
        if other is BACK:
            return BACK
        if other in CLOUDS:
            return _cloud(asker, CLOUDS[other])
        return {"oaic": _compatible_cloud, "locv1": _compatible_local,
                "ollama-remote": _ollama_remote, "bedrock": _bedrock}[other](asker)
    return BACK


def _check(asker: Asker, lister, key: str, name: str):
    """(models, None) when the key works; (None, message) when it does not."""
    try:
        with asker.busy(f"Checking your {name} key…"):
            return lister(key), None
    except net.HttpError as exc:
        if exc.status in (400, 401, 403):
            return None, f"{name} did not accept that key ({exc.status}). Try again, or esc to go back."
        return [], None      # could not check: offer the fallback model instead


def _key_and_models(asker: Asker, name: str, lister, optional: bool = False):
    note = "It stays on this computer."
    while True:
        key = asker.text(f"{name} API key" + (" (leave empty if none)" if optional else ""),
                         secret=True, note=note)
        if key is BACK:
            return BACK, None
        if not key and not optional:
            note = "A key is needed for this one. Esc goes back."
            continue
        models, problem = _check(asker, lister, key, name)
        if problem:
            note = problem
            continue
        return key, models


def _choose_model(asker: Asker, models: list[str], fallback: str, provider: str = ""):
    if models:
        return asker.select("Which model?", [Option(m, m) for m in models],
                            default=preferred_index(provider, models),
                            filterable=len(models) > 8, back=True)
    return asker.text("Which model?", default=fallback,
                      note="Could not fetch the list — type a model name.")


def _cloud(asker: Asker, cloud: Cloud):
    while True:
        key, models = _key_and_models(asker, cloud.label, cloud.lister)
        if key is BACK:
            return BACK
        model = _choose_model(asker, models, cloud.fallback_model, cloud.key)
        if model is BACK:
            continue
        return {cloud.key_setting: key, "model": model, "mode": "cloud"}


def _base_url(asker: Asker, question: str, default: str):
    url = asker.text(question, default=default, note="Without /v1 at the end.")
    return url if url is BACK else url.rstrip("/").removesuffix("/v1")


def _compatible_cloud(asker: Asker):
    base = _base_url(asker, "API address", "https://openrouter.ai/api")
    if base is BACK:
        return BACK
    key, models = _key_and_models(asker, "API", list_bearer(f"{base}/v1/models"))
    if key is BACK:
        return BACK
    model = _choose_model(asker, models, "")
    if model is BACK or not model:
        return BACK
    return {"openai_compatible_base_url": base, "openai_compatible_key": key,
            "openai_compatible_models": model, "model": f"oaic.{model}", "mode": "cloud"}


def _compatible_local(asker: Asker):
    base = _base_url(asker, "Server address", "http://localhost:8000")
    if base is BACK:
        return BACK
    key, models = _key_and_models(asker, "Server", list_bearer(f"{base}/v1/models"), optional=True)
    if key is BACK:
        return BACK
    model = _choose_model(asker, models, "")
    if model is BACK or not model:
        return BACK
    answer = {"local_compatible_base_url": base, "local_compatible_models": model,
              "model": f"locv1.{model}", "mode": "cloud"}
    if key:
        answer["local_compatible_key"] = key
    return answer


def _pick_ollama(asker: Asker, models: list[str], base_url: str = ""):
    if models:
        name = asker.select("Which Ollama model?", [Option(m, m) for m in models],
                            filterable=len(models) > 8, back=True)
    else:
        name = asker.text("Which Ollama model?", default="llama3",
                          note="No models found there — type one you will pull.")
    if name is BACK:
        return BACK
    answer = {"model": f"ollama.{name}", "mode": "local"}
    if base_url:
        answer["ollama_base_url"] = base_url
    return answer


def _ollama_remote(asker: Asker):
    base = asker.text("Ollama address", default="http://127.0.0.1:11434")
    if base is BACK:
        return BACK
    base = base.rstrip("/")
    with asker.busy("Looking for models there…"):
        models = ollama_models(base)
    return _pick_ollama(asker, models, base)


def _bedrock(asker: Asker):
    region = asker.text("AWS region", default="us-east-1")
    if region is BACK:
        return BACK
    key, models = _key_and_models(asker, "Bedrock", lambda k: list_bedrock(k, region))
    if key is BACK:
        return BACK
    model = _choose_model(asker, models, "")
    if model is BACK or not model:
        return BACK
    return {"bedrock_api_key": key, "aws_region": region,
            "model": f"bedrock.{model.removeprefix('bedrock.')}", "mode": "cloud"}


def describe(answer: dict) -> str:
    """"claude-sonnet-4-6" for a done line; "" when the question was skipped."""
    model = answer.get("model", "")
    for prefix in ("ollama.", "oaic.", "locv1.", "bedrock.", "cli."):
        if model.startswith(prefix):
            return model[len(prefix):] or model
    return model
