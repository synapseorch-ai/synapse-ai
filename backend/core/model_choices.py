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
    # A model its provider said is gone (F1213), or the product knows it retired (F1299), is offered nowhere,
    # whatever a host's or the provider's list says.
    from core import model_availability

    gone = await model_availability.unavailable()
    found = {k: model_availability.gone_from(v, {**model_availability.retired_for(k), **gone})[0]
             for k, v in (found or {}).items()}
    return {k: v for k, v in (found or {}).items() if v} or None


def named(found: dict[str, list[str]] | None, said: str) -> str | None:
    """The one model whose own id (after its provider's prefix) is `said`, case, spaces and hyphens aside; None for no
    list, none, or several."""
    def plain(text: str) -> str:
        return "".join(c for c in text.casefold() if c.isalnum())

    def suffixes(model: str) -> set[str]:
        # The id and each part after a provider's dot or slash: `us.anthropic.claude-haiku-4-5` is also
        # `anthropic.claude-haiku-4-5` and `claude-haiku-4-5` (SAI2's review).
        return {plain(model)} | {plain(model[i + 1:]) for i, c in enumerate(model) if c in "./"}

    wanted = plain(said)
    matches = {m for models in (found or {}).values() for m in models if wanted and wanted in suffixes(m)}
    return matches.pop() if len(matches) == 1 else None


def offers(found: dict[str, list[str]] | None, model: str) -> bool:
    """Whether `model` is one of them; True when there is no list to go by."""
    return found is None or any(model in models for models in found.values())
