"""
What chat models a hosted provider offers a key (PROV1, D245).

One reading of each provider's `GET {base_url}/models` for both products: OSS's
`/api/models` and cloud's model discovery call `list_chat_models`, so a filter
fixed here is fixed in both. A list holds more than chat models — Groq's has
Whisper and its guards, NVIDIA's every embedder and reranker it hosts — so each
is cut to what an agent can talk to: the provider's own marker where it gives
one, and a word list where it does not.
"""
from __future__ import annotations

import httpx

from core.providers.registry import ProviderSpec

#: Words in an id that name a model an agent cannot chat with.
NOT_CHAT = (
    "embed", "rerank", "whisper", "tts", "speech", "transcribe", "audio", "orpheus", "playai", "guard", "safety",
    "moderation", "ocr", "image", "flux", "stable-diffusion", "reward", "parse", "clip",
)


def _named_like_chat(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(word in lowered for word in NOT_CHAT)


def _is_chat(spec: ProviderSpec, entry: dict) -> bool:
    """Whether one entry of a provider's model list is a chat model."""
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    if spec.id == "mistral":
        capabilities = entry.get("capabilities")
        if isinstance(capabilities, dict) and not capabilities.get("completion_chat"):
            return False
    if spec.id == "together" and entry.get("type") not in (None, "chat"):
        return False
    if spec.id == "openrouter":
        architecture = entry.get("architecture")
        if isinstance(architecture, dict) and "text" not in (architecture.get("output_modalities") or ["text"]):
            return False
    return _named_like_chat(model_id)


def chat_models(spec: ProviderSpec, payload) -> list[str]:
    """The chat models in a provider's `/models` answer, bare and sorted."""
    entries = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError(f"{spec.label}'s model list is not a list")
    return sorted({e["id"].strip() for e in entries if isinstance(e, dict) and _is_chat(spec, e)})


def routed(spec: ProviderSpec, models) -> list[str]:
    """Bare ids as the engine names them: behind the provider's routing prefix."""
    return [f"{spec.strip}{m}" for m in models]


async def list_chat_models(spec: ProviderSpec, key: str, *, timeout: float = 5.0) -> list[str]:
    """The chat models this key may use, bare. Raises `httpx.HTTPStatusError` on a refusal."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{spec.base_url}/models", headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        return chat_models(spec, r.json())
