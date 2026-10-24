"""Which model each agent gets, picked in code from what this workspace can call (F966, SAI2-P2; D300).

Every new agent got the workspace's default model. Naveen's decisions (10-23, F966-PLAN.md): the planner states what
an agent needs (`simple`, `reasoning`, `vision`), never an id; code picks from a typed menu, with hard filters (tools,
vision, a key or a wallet that pays; a CLI or a local model only when the person asks) and a ceiling on what an
automatic pick may cost, above which Synapse AI only offers a stronger model (`stronger_than`). A person's own wish
("use Claude for the writer") is theirs: resolved without the ceiling, and never guessed at (`resolve_wish`).

The menu is `model_choices.choices()` with what is known of each model. Self-hosted knows it from the provider table
(tools, images), `core.reasoning` and the rate card; a host says its own per model (`set_model_facts`: cloud, its
platform rows and its wallet). Only the facts differ between the two products.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from core.log import get_logger

_log = get_logger(__name__)

#: What an automatic pick may cost, in USD per 1M output tokens (Naveen, 10-23): above it, only offered.
AUTO_PICK_CEILING_USD_PER_MTOK_OUT = 15

NEEDS = ("simple", "reasoning", "vision")


@dataclass(frozen=True)
class ModelEntry:
    id: str
    name: str
    provider: str
    #: managed (the platform's own), key (the workspace's own key), cli (a subscription CLI), local (its own machine).
    source: str
    #: native: called with tools; prompt: tools described in its prompt; none: it calls no tools.
    tools: str
    vision: bool
    reasoning: bool
    #: USD per 1M tokens; None when not known (a CLI's subscription, an unlisted model); 0 for a local one.
    price_in: float | None
    price_out: float | None
    #: Whether this workspace can pay for a call now (cloud: its wallet). Self-hosted: always.
    can_pay: bool


@dataclass(frozen=True)
class Picked:
    id: str
    #: Code's reason, said on the card: "a stronger model, for reasoning".
    reason: str


@dataclass(frozen=True)
class NoModel:
    #: Said, never a silent default: "No model this workspace can use calls tools."
    issue: str


@dataclass(frozen=True)
class Ask:
    question: str
    options: list[str]


#: `fn(ids) -> {id: {field: value}}`: what a host knows of each model, over self-hosted's own facts.
Facts = Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
_facts: Facts | None = None


def set_model_facts(fn: Facts | None) -> None:
    global _facts
    _facts = fn


def _source(model_id: str, mode: str) -> str:
    low = model_id.lower()
    if low.startswith("synapse."):
        return "managed"
    if mode == "cli" or low.startswith("cli."):
        return "cli"
    if low.startswith(("ollama.", "locv1.")) or mode == "local":
        return "local"
    return "key"


async def _own(model_id: str, pricing: dict) -> ModelEntry:
    from core.images import model_can_see_images
    from core.providers.registry import provider_for
    from core.reasoning import supports_native
    from core.usage_tracker import _resolve_pricing_entry

    spec = provider_for(model_id)
    source = _source(model_id, spec.mode)
    priced = _resolve_pricing_entry(model_id, pricing) if source not in ("cli", "local") else None
    return ModelEntry(
        id=model_id, name=model_id, provider=spec.id, source=source, tools=spec.caps.tools,
        vision=model_can_see_images(model_id), reasoning=supports_native(model_id),
        price_in=0 if source == "local" else (priced or {}).get("input_per_1m"),
        price_out=0 if source == "local" else (priced or {}).get("output_per_1m"),
        can_pay=True)


async def menu() -> list[ModelEntry]:
    """Every model this workspace can call, with what is known of each; [] when the list cannot be read."""
    from core import model_choices

    try:
        listed = await model_choices.choices()
        if not listed:
            return []
        ids = list(dict.fromkeys(m for models in listed.values() for m in models))
        try:
            from core.store.usage import load_pricing

            pricing = await load_pricing() or {}
        except Exception:  # noqa: BLE001 — no rate card: the seeded one, below
            pricing = {}
        from core.model_pricing import DEFAULT_MODEL_PRICING

        pricing = {**DEFAULT_MODEL_PRICING, **pricing}  # the store's rows over the seed it started from
        entries = [await _own(i, pricing) for i in ids]
        if _facts is not None:
            said = await _facts(ids) or {}
            entries = [replace(e, **{k: v for k, v in (said.get(e.id) or {}).items()
                                     if k in ModelEntry.__dataclass_fields__ and k != "id"}) for e in entries]
        return entries
    except Exception:  # noqa: BLE001 — a menu that cannot be read picks nothing: the workspace default, as before
        _log.warning("model menu not read", exc_info=True)
        return []


# ── pick ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_REASONS = {"simple": "the lowest-cost model that fits", "reasoning": "a stronger model, for reasoning",
            "vision": "it can read images"}
_ONLY = "the only model here that fits"


def _fits(e: ModelEntry, needs: str, has_tools: bool) -> bool:
    return e.can_pay and (not has_tools or e.tools in ("native", "prompt")) and (needs != "vision" or e.vision)


def _cheapest_first(e: ModelEntry, has_tools: bool = False) -> tuple:
    # For an agent with tools, native tools before prompt-described ones; then by price, unknown after known.
    return (has_tools and e.tools != "native", e.price_out is None, e.price_out or 0, e.id)


def _strongest_first(e: ModelEntry, has_tools: bool = False) -> tuple:
    return (not e.reasoning, has_tools and e.tools != "native", e.price_out is None, -(e.price_out or 0), e.id)


def _no_model(needs: str, has_tools: bool) -> NoModel:
    what = "calls tools" if has_tools else ("reads images" if needs == "vision" else "it can pay for")
    if has_tools and needs == "vision":
        what = "calls tools and reads images"
    return NoModel(f"No model this workspace can use {what}.")


def _by_need(fitting: list[ModelEntry], needs: str, *, ceiling: bool, has_tools: bool) -> ModelEntry:
    if needs == "reasoning":
        within = [e for e in fitting
                  if not ceiling or e.price_out is None or e.price_out <= AUTO_PICK_CEILING_USD_PER_MTOK_OUT]
        return sorted(within or fitting, key=lambda e: _strongest_first(e, has_tools))[0]
    return sorted(fitting, key=lambda e: _cheapest_first(e, has_tools))[0]


def pick(needs: str, *, has_tools: bool, menu: list[ModelEntry]) -> Picked | NoModel | None:
    """The model an agent that needs `needs` gets. None with no menu (the workspace default, as before); `NoModel`
    when nothing on it can do the job, said rather than defaulted (SAI2's review)."""
    if not menu:
        return None
    usable = [e for e in menu if e.source not in ("cli", "local")]
    fitting = [e for e in usable if _fits(e, needs, has_tools)]
    if not fitting:
        # A local model is offered when it is the only kind there is (Naveen: only when asked, or the only option).
        local = [e for e in menu if e.source == "local" and _fits(e, needs, has_tools)]
        if local and not usable:
            fitting = local
        else:
            return _no_model(needs, has_tools)
    if len(fitting) == 1:
        return Picked(fitting[0].id, _ONLY)
    return Picked(_by_need(fitting, needs, ceiling=True, has_tools=has_tools).id, _REASONS[needs])


def stronger_than(current: str, needs: str, *, has_tools: bool, menu: list[ModelEntry]) -> Picked | None:
    """The strongest model above the ceiling that does the job, with its price, for Synapse AI to offer; None when
    there is none (or only ones whose price is not known: an offer says what it costs)."""
    now = next((e for e in menu if e.id == current), None)
    floor = max(AUTO_PICK_CEILING_USD_PER_MTOK_OUT, (now.price_out or 0) if now else 0)
    above = [e for e in menu if e.source not in ("cli", "local") and _fits(e, needs, has_tools)
             and e.price_out is not None and e.price_out > floor and e.id != current]
    if not above:
        return None
    best = sorted(above, key=lambda e: _strongest_first(e, has_tools))[0]
    return Picked(best.id, f"about ${best.price_out:g} per 1M output tokens")


# ── a person's wish ─────────────────────────────────────────────────────────────────────────────────────────────────

#: A word a person uses for a family of models, and what its ids hold.
FAMILIES = {"claude": "claude", "anthropic": "claude", "gpt": "gpt", "openai": "gpt", "gemini": "gemini",
            "google": "gemini", "nova": "nova", "llama": "llama", "mistral": "mistral", "grok": "grok",
            "deepseek": "deepseek", "qwen": "qwen", "kimi": "kimi"}
_CHEAPEST = {"cheapest", "cheaper", "cheap", "fastest", "faster", "smallest"}
_STRONGEST = {"strongest", "stronger", "best", "smartest", "most capable", "biggest"}


def _plain(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


def resolve_wish(wish: str, needs: str, *, has_tools: bool, menu: list[ModelEntry]) -> Picked | NoModel | Ask:
    """The model a person asked for, in their words: a model's own id ("nova pro"), a family ("Claude": its best for
    the need, not bound by the ceiling, as it is their choice), a rule ("the cheapest"), a CLI or a local model when
    they say so. None of these, or two families, is a question with the menu's names: never a guess."""
    words = set(re.findall(r"[a-z0-9]+", wish.casefold()))
    phrase = " ".join(re.findall(r"[a-z0-9]+", wish.casefold()))
    wants_cli, wants_local = "cli" in words, bool(words & {"local", "ollama"})
    # A CLI or a local model only when they say so (Naveen); then only those.
    kind = "cli" if wants_cli else ("local" if wants_local else None)
    allowed = [e for e in menu if (e.source == kind if kind else e.source not in ("cli", "local"))]
    names = sorted(e.id for e in menu if e.source not in ("cli", "local") or wants_cli or wants_local)

    # A model's own id, said as such ("nova pro", "claude-haiku-4-5").
    wanted = _plain(wish)
    own = [e for e in allowed if wanted and wanted in {_plain(e.id)} | {_plain(e.id[i + 1:]) for i, c in enumerate(e.id)
                                                                       if c in "./"}]
    if len(own) == 1:
        e = own[0]
        if has_tools and e.tools not in ("native", "prompt"):
            return NoModel(f"{e.id} can't call tools, which this agent needs.")
        if needs == "vision" and not e.vision:
            return NoModel(f"{e.id} can't read images, which this agent needs.")
        return Picked(e.id, f"you asked for {wish.strip()}")

    families = {FAMILIES[w] for w in words if w in FAMILIES}
    if len(families) > 1:
        return Ask("Which model do you mean? This workspace can use: " + ", ".join(names) + ".", names)
    if families:
        family = families.pop()
        said = next((m.group(0) for w, f in FAMILIES.items() if f == family
                     for m in [re.search(rf"\b{w}\b", wish, re.I)] if m), family)
        members = [e for e in allowed if family in e.id.lower()]
        if not members:
            return Ask(f"No {said} model is available here. This workspace can use: " + ", ".join(names) + ".", names)
        fitting = [e for e in members if _fits(e, needs, has_tools)]
        if not fitting:
            return _no_model(needs, has_tools)
        return Picked(_by_need(fitting, needs, ceiling=False, has_tools=has_tools).id, f"you asked for {said}")

    fitting = [e for e in allowed if _fits(e, needs, has_tools)]
    if wants_cli or wants_local:
        if not fitting:
            return Ask(f"No {'CLI' if wants_cli else 'local'} model here can do this. This workspace can use: "
                       + ", ".join(names) + ".", names)
        return Picked(_by_need(fitting, needs, ceiling=False, has_tools=has_tools).id,
                      f"you asked for {'a CLI' if wants_cli else 'a local'} model")
    if words & _CHEAPEST and fitting:
        return Picked(sorted(fitting, key=lambda e: _cheapest_first(e, has_tools))[0].id,
                      "you asked for the lowest-cost model")
    if (words & _STRONGEST or "most capable" in phrase) and fitting:
        return Picked(sorted(fitting, key=lambda e: _strongest_first(e, has_tools))[0].id,
                      "you asked for the strongest model")
    return Ask(f"I don't know which model \"{wish.strip()}\" is. This workspace can use: " + ", ".join(names) + ".",
               names)
