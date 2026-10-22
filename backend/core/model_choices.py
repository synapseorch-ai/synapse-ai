"""The models this workspace can pick, by provider: what the agent editor's model picker offers (SAI2-P2, D308).

Synapse AI's change planner was shown no models: asked to "make the Front desk agent use nova pro", it offered models
from its own memory, and the old builder wrote one this install could not call. `choices()` is the list both read: the
`models` topic shows it, and the change check refuses a model none of them is, before the review. Self-hosted's is
`/api/models` (each provider that answers, with its models); a host sets its own (`set_model_choices`; cloud: its
catalog and its people's own keys). A list that cannot be read is None, and then nothing is refused for it.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from core.log import get_logger

_log = get_logger(__name__)

#: `fn() -> {provider: [model id, …]}`, only providers this workspace can call.
Choices = Callable[[], Awaitable[dict[str, list[str]]]]
_host: Choices | None = None


def set_model_choices(fn: Choices | None) -> None:
    global _host
    _host = fn


async def _own() -> dict[str, list[str]]:
    from core.routes.data import get_models

    listed = await get_models()
    return {name: list(p.get("models") or []) for name, p in (listed.get("providers") or {}).items()
            if isinstance(p, dict) and p.get("available") and p.get("models")}


async def choices() -> dict[str, list[str]] | None:
    """{provider: models} this workspace can call; None when the list cannot be read."""
    try:
        found = await (_host or _own)()
    except Exception:  # noqa: BLE001 — a list that cannot be read refuses nothing and is not shown
        _log.warning("model choices not read", exc_info=True)
        return None
    return {k: v for k, v in (found or {}).items() if v} or None


def offers(found: dict[str, list[str]] | None, model: str) -> bool:
    """Whether `model` is one of them; True when there is no list to go by."""
    return found is None or any(model in models for models in found.values())
