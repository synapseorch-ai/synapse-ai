"""
Whether the chat page's model can answer at all, asked before anyone types (F182).

A fresh install's default model is `ollama.mistral`. With nothing listening, the
chat page greeted the person with "Ready" and answered their first message with
an HTTP library's error text; nothing had checked anything before saying Ready.
`/api/status` now answers that question, and the page says what is missing and
where to fix it.

Only what can be proven is reported, and nothing is spent finding out:

* a key provider with no key saved (keys come only from settings; nothing reads
  them from the environment);
* an OpenAI-compatible server, a local one or the model gateway with no address;
* Ollama not reachable, refusing its model list, or without the model pulled —
  one local call to `/api/tags` with a short timeout, since a page load waits on
  it (a refused port fails at once).

A saved key is not tried: trying it is a paid call. Bedrock (boto's default
credential chain) and the CLI providers (their own login) can authenticate
outside settings, so they are never called "not ready" on a guess.
"""
from __future__ import annotations

import httpx

from core.llm_providers import _ollama_base_url, detect_provider_from_model

_KEYED = {
    "openai": ("openai_key", "OpenAI"),
    "anthropic": ("anthropic_key", "Anthropic"),
    "gemini": ("gemini_key", "Gemini"),
    "grok": ("grok_key", "Grok"),
    "deepseek": ("deepseek_key", "DeepSeek"),
}

_ADDRESSED = {
    "openai_compatible": ("openai_compatible_base_url", "OpenAI-compatible"),
    "local_compatible": ("local_compatible_base_url", "local OpenAI-compatible"),
    "model_gateway": ("model_gateway_base_url", "model gateway"),
}

#: A page load waits on the model list, so it is short.
TAGS_TIMEOUT = 2.0


async def _ollama_models(base: str) -> list[str]:
    """The names Ollama has pulled, from its own list."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{base}/api/tags", timeout=TAGS_TIMEOUT)
        response.raise_for_status()
        models = (response.json() or {}).get("models") or []
        return [str(m.get("name") or "") for m in models if isinstance(m, dict)]


def _pulled(model: str, names: list[str]) -> bool:
    # Ollama reads a bare name as its `latest` tag, and nothing else.
    return model in names or (":" not in model and f"{model}:latest" in names)


async def model_problem(settings: dict, model: str) -> str | None:
    """Why `model` cannot answer, in a sentence; None when nothing is known to stop it."""
    provider = detect_provider_from_model(model)
    if provider in _KEYED:
        field, label = _KEYED[provider]
        return None if (settings.get(field) or "").strip() else f"No {label} key is saved."
    if provider in _ADDRESSED:
        field, label = _ADDRESSED[provider]
        return None if (settings.get(field) or "").strip() else f"No {label} server address is saved."
    if provider != "ollama":
        return None

    base = _ollama_base_url(settings)
    bare = model[len("ollama."):] if model.startswith("ollama.") else model
    try:
        names = await _ollama_models(base)
    except httpx.HTTPStatusError as e:
        return f"Ollama at {base} answered {e.response.status_code}."
    except Exception:
        return f"Ollama is not reachable at {base}."
    if _pulled(bare, names):
        return None
    return f"Ollama has no model named {bare}. Pull it with `ollama pull {bare}`, or choose another model."
