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


# ── what a door says of the model it sets (F1762) ───────────────────────────────────────────────────────────────────

#: {provider id: the name the person knows it by}: the providers whose list is a host's whole catalogue (cloud: its
#: platform models). A model of one that the list does not offer is one the host does not serve, and nothing the person
#: sets up changes that; an open provider's model (a key, a pulled model, a CLI's sign-in) can come later.
_closed: dict[str, str] = {}

#: Whether the person's own doors refuse a closed catalogue's unserved model at save. Off: those doors save what they
#: name (FF14's gate) and say why it cannot answer; the builder's run refuses as before (F1250). Turning it on changes
#: no door: each already asks `save_check`, which returns a refusal or a note.
REFUSE_UNSERVED = False

#: How many of a catalogue's models a note names before "and N more".
_NAMED = 6


def set_closed_providers(names: dict[str, str] | None) -> None:
    global _closed
    _closed = dict(names or {})


async def _names(models: list[str]) -> dict[str, str]:
    """Each model as a person reads it: "Claude Haiku 5.5 (synapse.claude-haiku-5-5)" where its name is known, else
    its id."""
    try:
        from core.native_builder.models_menu import facts_of

        known = {e.id: e.name for e in await facts_of(models)}
    except Exception:  # noqa: BLE001 — no names read: each is said by its id
        _log.warning("model names not read", exc_info=True)
        known = {}
    return {m: f"{known[m]} ({m})" if known.get(m) and known[m] != m else m for m in models}


async def notes(models: list[str]) -> dict[str, str | None]:
    """Why each of `models` cannot answer here now, in a sentence the person can act on, or None (F1762)."""
    return {m: said for m, (said, _unserved) in (await _said(models)).items()}


async def _said(models: list[str]) -> dict[str, tuple[str | None, bool]]:
    """`notes`, each with whether it is a closed catalogue's model the host does not serve.

    A closed catalogue's model it does not offer is said by the host's name, with what it does offer; a model its
    provider or the gateway refused, or the product knows retired, by that reason; an open provider's model by what
    is missing for it (`model_readiness`: no key, no address, not pulled, not signed in). A list that cannot be read
    says nothing of a closed catalogue. Never raises: what cannot be read is not said."""
    from core import model_availability
    from core.llm_providers import detect_provider_from_model
    from core.model_failures import GATEWAY_REFUSED

    wanted = [m for m in dict.fromkeys(str(x or "").strip() for x in models) if m]
    if not wanted:
        return {}
    found = await choices()
    try:
        gone = await model_availability.unavailable()
    except Exception:  # noqa: BLE001
        gone = {}
    names = await _names(wanted + [m for ms in (found or {}).values() for m in ms
                                   if detect_provider_from_model(m) in _closed])
    out: dict[str, tuple[str | None, bool]] = {}
    open_ones: list[str] = []
    for model in wanted:
        provider = detect_provider_from_model(model)
        if provider in _closed:
            if found is None or offers(found, model):
                out[model] = (None, False)
                continue
            served = [m for ms in found.values() for m in ms if detect_provider_from_model(m) == provider]
            # Every offered model said the same way: by its display name with its id, or, when any of them has no
            # display name, all by their ids, never one form beside the other. The rows with none are logged.
            unnamed = [m for m in served[:_NAMED] if names.get(m, m) == m]
            if unnamed:
                _log.warning("catalogue model has no display name: %s", ", ".join(unnamed))
            listed = ", ".join(m if unnamed else names[m] for m in served[:_NAMED])
            more = f" and {len(served) - _NAMED} more" if len(served) > _NAMED else ""
            pick = f" You can pick {listed}{more}, or" if listed else " Pick another model, or"
            out[model] = (f"{names.get(model, model)} isn't available on {_closed[provider]} yet, so it can't answer "
                          f"here.{pick} leave the model out for the workspace's default.", True)
            continue
        retired = model_availability.retired_for(provider)
        _kept, left = model_availability.gone_from([model], {**retired, **gone})
        if left:
            reason = str(left[0].get("reason") or "").strip()
            reason = reason.removeprefix(GATEWAY_REFUSED).strip() or "its provider no longer serves it."
            out[model] = (f"{names.get(model, model)} can't answer here: {reason}", False)
            continue
        open_ones.append(model)
    if open_ones:
        try:
            from core.config import load_settings
            from core.model_readiness import model_problems

            problems = await model_problems(dict(load_settings()), open_ones)
        except Exception:  # noqa: BLE001 — not read: nothing is said of them
            _log.warning("model readiness not read", exc_info=True)
            problems = {}
        for model in open_ones:
            problem = problems.get(model)
            out[model] = (f"{names.get(model, model)} can't answer here yet: {problem}" if problem else None, False)
    return out


async def save_check(model: object, before: object = None) -> tuple[str | None, str | None]:
    """At a door that sets an agent's or a step's model (F1762): `(refusal, note)`. The refusal, only with
    `REFUSE_UNSERVED` on, is for a closed catalogue's unserved model the agent or step did not have before; otherwise
    the same sentence is the note, saved beside the model. A model it already had is never refused."""
    said = str(model or "").strip()
    if not said:
        return None, None
    note, unserved = (await _said([said])).get(said, (None, False))
    if note is None:
        return None, None
    if REFUSE_UNSERVED and unserved and said != str(before or "").strip():
        return note, None
    return None, note


async def note_for(model: object) -> str | None:
    """`notes` for one model; None for none."""
    said = str(model or "").strip()
    return (await notes([said])).get(said) if said else None


async def step_refusal(steps: list, before: list | None = None) -> str | None:
    """The first of a workflow's steps whose own model a door refuses (`save_check`, only with `REFUSE_UNSERVED` on),
    in words that name the step; None. A model the step already had (`before`, by step id) is never refused."""
    if not REFUSE_UNSERVED:
        return None
    had = {s.get("id"): s.get("model") for s in before or [] if isinstance(s, dict)}
    for step in steps or []:
        if not isinstance(step, dict) or not str(step.get("model") or "").strip():
            continue
        refused, _note = await save_check(step["model"], had.get(step.get("id")))
        if refused is not None:
            return f"Step {step.get('name') or step.get('id')}: {refused}"
    return None


async def step_notes(steps: list) -> list[dict]:
    """Why each step's own model cannot answer here yet: `[{step_id, model, note}]`, only the steps that have one."""
    with_models = [s for s in steps or [] if isinstance(s, dict) and str(s.get("model") or "").strip()]
    if not with_models:
        return []
    said = await notes([s["model"] for s in with_models])
    return [{"step_id": s.get("id"), "model": s["model"], "note": n} for s in with_models
            if (n := said.get(str(s["model"]).strip()))]
