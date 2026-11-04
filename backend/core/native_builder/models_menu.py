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
    #: Its input window in tokens (`core.providers.window_tokens`, a host's facts over it); None when not known.
    window: int | None = None


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
    from core.providers import window_tokens
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
        can_pay=True, window=window_tokens(model_id))


async def _pricing() -> dict:
    """The rate card: the store's rows over the seed it started from."""
    try:
        from core.store.usage import load_pricing

        pricing = await load_pricing() or {}
    except Exception:  # noqa: BLE001 — no rate card: the seeded one, below
        pricing = {}
    from core.model_pricing import DEFAULT_MODEL_PRICING

    return {**DEFAULT_MODEL_PRICING, **pricing}


async def menu() -> list[ModelEntry]:
    """Every model this workspace can call, with what is known of each; [] when the list cannot be read."""
    from core import model_choices

    try:
        listed = await model_choices.choices()
        if not listed:
            return []
        ids = list(dict.fromkeys(m for models in listed.values() for m in models))
        pricing = await _pricing()
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

#: The input window an agent that reads long inputs needs, in tokens (F1251): a model known to have less is skipped.
LONG_WINDOW = 128_000

#: The pools a pick looks in, in order (Naveen, 10-30): the workspace's own keys, then the platform's models.
POOLS = (("key", "from your own keys"), ("managed", "from the platform's models"))

_REASONS = {"narrow": "the lowest-cost model that fits a narrow job", "reasoning": "a stronger model, for reasoning",
            "default": "your workspace's default model, which fits",
            "best": "the strongest that fits, within the automatic price ceiling"}
_ONLY = "the only model here that fits"


def _fits(e: ModelEntry, needs: str, has_tools: bool, long_inputs: bool = False) -> bool:
    return (e.can_pay and (not has_tools or e.tools in ("native", "prompt")) and (needs != "vision" or e.vision)
            and (not long_inputs or e.window is None or e.window >= LONG_WINDOW))


def fit_words(needs: str, *, has_tools: bool, long_inputs: bool = False) -> str:
    """What "fits" asked of the model, in the card's words (F1251): what the job needs, never a guess at more."""
    said = [w for w, on in (("calls tools", has_tools), ("reads images", needs == "vision"),
                            ("reasons carefully", needs == "reasoning"),
                            (f"reads long inputs (a window of {LONG_WINDOW // 1000}k tokens or more, where known)",
                             long_inputs)) if on]
    return "; ".join(said)


def _cheapest_first(e: ModelEntry, has_tools: bool = False) -> tuple:
    # For an agent with tools, native tools before prompt-described ones; then by price, unknown after known.
    return (has_tools and e.tools != "native", e.price_out is None, e.price_out or 0, e.id)


def _strongest_first(e: ModelEntry, has_tools: bool = False) -> tuple:
    return (not e.reasoning, has_tools and e.tools != "native", e.price_out is None, -(e.price_out or 0), e.id)


def _no_model(candidates: list[ModelEntry], needs: str, has_tools: bool, *, of: str = "") -> NoModel:
    """Why nothing here does the job, said by what the candidates lack (SAI2's probe): the wallet first, then tools,
    then images. `of` names the family a wish asked for."""
    if candidates and not any(e.can_pay for e in candidates):
        if of:
            return NoModel(f"{', '.join(sorted(e.id for e in candidates))} can't be paid for now (the wallet).")
        return NoModel("No model this workspace can use can be paid for now (the wallet).")
    payable = [e for e in candidates if e.can_pay]
    kind = f"No {of} model here" if of else "No model this workspace can use"
    if has_tools and not any(e.tools in ("native", "prompt") for e in payable):
        lacks = "calls tools and reads images" if needs == "vision" and not any(e.vision for e in payable) else \
            "calls tools"
    elif needs == "vision" and not any(e.vision for e in payable):
        lacks = "reads images"
    else:
        lacks = f"reads long inputs (a window of {LONG_WINDOW // 1000}k tokens or more)"
    return NoModel(f"{kind} {lacks}." if not of else f"{kind} {lacks}, which this agent needs.")


def _within(fitting: list[ModelEntry], ceiling: bool) -> list[ModelEntry]:
    return [e for e in fitting
            if not ceiling or e.price_out is None or e.price_out <= AUTO_PICK_CEILING_USD_PER_MTOK_OUT] or fitting


def _by_need(fitting: list[ModelEntry], needs: str, *, ceiling: bool, has_tools: bool) -> ModelEntry:
    if needs == "reasoning":
        return sorted(_within(fitting, ceiling), key=lambda e: _strongest_first(e, has_tools))[0]
    return sorted(fitting, key=lambda e: _cheapest_first(e, has_tools))[0]


def _best(fitting: list[ModelEntry], needs: str, *, has_tools: bool, narrow: bool,
          default: str | None) -> tuple[ModelEntry, str]:
    """The best fit in one pool (Naveen, 10-30, replacing F966's cheapest-first): the cheapest only for a narrow job;
    the strongest within the ceiling for reasoning; else the workspace's default where it fits, then the strongest
    within the ceiling."""
    if narrow and needs != "reasoning":
        return _by_need(fitting, "simple", ceiling=True, has_tools=has_tools), "narrow"
    if needs == "reasoning":
        return _by_need(fitting, "reasoning", ceiling=True, has_tools=has_tools), "reasoning"
    if default and (mine := next((e for e in fitting if e.id == default), None)):
        return mine, "default"
    return sorted(_within(fitting, True), key=lambda e: _strongest_first(e, has_tools))[0], "best"


#: The price bands a stand-in keeps to (F1369), USD per 1M output tokens: never in a dearer band than the refused one.
STAND_IN_BANDS = (1.0, 5.0, float(AUTO_PICK_CEILING_USD_PER_MTOK_OUT))


async def stand_in(refused: str, *, has_tools: bool) -> str | None:
    """The model a call its provider refused as gone answers from, once (F1369, cap-04): the cheapest that fits from
    the **same provider and the same pool** (the workspace's own key, or the platform's), never in a dearer price band
    than the refused one's, never above the automatic ceiling. None when there is none, or the refused one's price is
    not known: a person who chose one provider never has a call sent to another because a model retired."""
    gone = await _own(refused, await _pricing())
    if gone.source in ("cli", "local") or gone.price_out is None:
        return None
    bound = next((b for b in STAND_IN_BANDS if gone.price_out <= b), STAND_IN_BANDS[-1])
    fitting = [e for e in await menu() if e.id != refused and e.provider == gone.provider and e.source == gone.source
               and _fits(e, "simple", has_tools) and e.price_out is not None and e.price_out <= bound]
    return sorted(fitting, key=lambda e: _cheapest_first(e, has_tools))[0].id if fitting else None


def pick(needs: str, *, has_tools: bool, menu: list[ModelEntry], narrow: bool = False, long_inputs: bool = False,
         default: str | None = None) -> Picked | NoModel | None:
    """The model an agent that needs `needs` gets: the best fit among the workspace's own-key models, then among the
    platform's (F1251). `narrow`: a small job with a narrow goal, which gets the cheapest that fits; `default`: the
    workspace's default model. None with no menu (the workspace default, as before); `NoModel` when nothing on it can
    do the job, said rather than defaulted (SAI2's review)."""
    if not menu:
        return None
    usable = [e for e in menu if e.source not in ("cli", "local")]
    asked = fit_words(needs, has_tools=has_tools, long_inputs=long_inputs)
    said = f": one that {asked}" if asked else ""
    every = [e for e in usable if _fits(e, needs, has_tools, long_inputs)]
    if len(every) == 1:
        return Picked(every[0].id, _ONLY + said)
    for source, where in POOLS:
        fitting = [e for e in every if e.source == source]
        if fitting:
            chosen, why = _best(fitting, needs, has_tools=has_tools, narrow=narrow, default=default)
            return Picked(chosen.id, f"{_REASONS[why]}, {where}{said}")
    # A local model is offered when it is the only kind there is (Naveen: only when asked, or the only option).
    local = [e for e in menu if e.source == "local" and _fits(e, needs, has_tools, long_inputs)]
    if local and not usable:
        chosen = local[0] if len(local) == 1 else _by_need(local, needs, ceiling=True, has_tools=has_tools)
        return Picked(chosen.id, (_ONLY if len(local) == 1 else _REASONS["narrow" if needs != "reasoning"
                                                                          else "reasoning"]) + said)
    return _no_model(usable or menu, needs, has_tools)


def workspace_model() -> str:
    """The workspace's own default model, which a pick prefers where it fits (F1251); "" when none is set."""
    try:
        from core.config import load_settings

        return str(load_settings().get("model") or "").strip()
    except Exception:  # noqa: BLE001 — no settings read: no default to prefer
        return ""


#: Providers whose model ids are the person's to name (an endpoint they run, a model they pulled): its list is what the
#: endpoint answered, which can be empty while the model answers. Set up (an address, a key), any id is theirs.
OPEN_LISTED = frozenset({"openai_compatible", "local_compatible", "ollama", "huggingface"})


async def builder_model_refusal(model: Any, before: Any = None, *, has_tools: bool = False) -> tuple[str | None,
                                                                                                     str | None]:
    """`model_refusal` at a builder door, only when Synapse AI itself sets the model, in its own run (Naveen 10-30:
    builder only). The person's own doors (the CLI, MCP, the app) set what they name, as before (FF14's gate)."""
    from core.builder_tools import _current_run

    if not _current_run.get():
        return None, (str(model).strip() or None) if model is not None else None
    return await model_refusal(model, before, has_tools=has_tools)


async def model_refusal(model: Any, before: Any = None, *, has_tools: bool = False,
                        default_ok: bool = True) -> tuple[str | None, str | None]:
    """Why `model` cannot be an agent's model here, in words, or None; and the model to save (F1250). One check at
    every door Synapse AI sets one through: what the agent editor's picker lists (`model_choices`; a provider with no
    key lists nothing), a model said by its own id being that one (F959), the workspace's own default, a set-up
    provider whose ids are the person's (`OPEN_LISTED`), and its best fit here named (F1251). A model the agent had
    already is never asked about again, so an agent saved before still saves. No list read refuses nothing."""
    from core import model_choices

    said = str(model or "").strip()
    if not said:
        return None, None
    if said == str(before or "").strip() or said == workspace_model():
        return None, said
    found = await model_choices.choices()
    if model_choices.offers(found, said):
        return None, said
    if (named := model_choices.named(found, said)) is not None:
        return None, named
    if await _open_listed_ready(said):
        return None, said
    listed = "; ".join(f"{p}: {', '.join(m[:6])}" for p, m in (found or {}).items())
    best = pick("simple", has_tools=has_tools, menu=await menu(), default=workspace_model())
    fit = f" Its best fit here is {best.id} ({best.reason})." if isinstance(best, Picked) else ""
    tail = " Or leave the model out for the workspace's default." if default_ok else ""
    return f'"{said}" is not a model this workspace can use.{fit} It can use: {listed}.{tail}', None


async def _open_listed_ready(model: str) -> bool:
    """Whether `model` is on a provider whose ids are the person's, set up here (its address or key saved; Ollama's
    own list read), as a run would call it (`model_readiness`)."""
    from core.model_readiness import detect_provider_from_model, model_problem

    if detect_provider_from_model(model) not in OPEN_LISTED:
        return False
    try:
        from core.config import load_settings

        return await model_problem(dict(load_settings()), model) is None
    except Exception:  # noqa: BLE001 — not read: not known to answer, so the list decides
        return False


def _strength(e: ModelEntry | None) -> tuple:
    return (bool(e and e.reasoning), (e.price_out or 0) if e else 0)


def stronger_than(current: str, needs: str, *, has_tools: bool, menu: list[ModelEntry]) -> Picked | None:
    """The next real step up from `current`, with its price, for Synapse AI to offer (F1018). Below the ceiling, the
    strongest within it; only from a model at or above the ceiling, the strongest above it (SAI2's probe: from Nova
    Pro, Sonnet, not Opus). Never an unpriced one: an offer says what it costs. None when nothing is stronger."""
    now = next((e for e in menu if e.id == current), None)
    priced = [e for e in menu if e.source not in ("cli", "local") and _fits(e, needs, has_tools)
              and e.price_out is not None and e.id != current and _strength(e) > _strength(now)]
    at_ceiling = now is not None and (now.price_out or 0) >= AUTO_PICK_CEILING_USD_PER_MTOK_OUT
    within = [e for e in priced if e.price_out <= AUTO_PICK_CEILING_USD_PER_MTOK_OUT]
    pool = [e for e in priced if e.price_out > AUTO_PICK_CEILING_USD_PER_MTOK_OUT] if at_ceiling else within
    if not pool:
        return None
    best = sorted(pool, key=lambda e: _strongest_first(e, has_tools))[0]
    return Picked(best.id, f"about ${best.price_out:g} per 1M output tokens")


# ── a person's wish ─────────────────────────────────────────────────────────────────────────────────────────────────

#: A word a person uses for a family of models: what its ids hold, and the providers that serve it.
FAMILIES: dict[str, tuple[str, frozenset[str]]] = {
    "claude": ("claude", frozenset({"anthropic", "anthropic_cli"})),
    "gpt": ("gpt", frozenset({"openai", "codex_cli"})),
    "gemini": ("gemini", frozenset({"gemini", "gemini_cli"})), "nova": ("nova", frozenset()),
    "llama": ("llama", frozenset()), "mistral": ("mistral", frozenset({"mistral"})),
    "grok": ("grok", frozenset({"grok"})), "deepseek": ("deepseek", frozenset({"deepseek"})),
    "qwen": ("qwen", frozenset({"qwen"})), "kimi": ("kimi", frozenset({"moonshot"})),
}
#: Other words for a family.
_SAME = {"anthropic": "claude", "openai": "gpt", "chatgpt": "gpt", "google": "gemini"}
_CHEAPEST = {"cheapest", "cheaper", "cheap", "fastest", "faster", "smallest"}
_STRONGEST = {"strongest", "stronger", "best", "smartest", "biggest"}


def _plain(text: str) -> str:
    return "".join(c for c in text.casefold() if c.isalnum())


def _named(menu: list[ModelEntry], wish: str, words: set[str]) -> tuple[list[ModelEntry], str]:
    """Models the person named outright, and the words they named it by: the whole wish is an id or its part after a
    prefix ("nova pro", "claude-opus-4-1"), else one of their words is a part of exactly one model's id that no family
    word is ("Opus" in "put it on Opus")."""
    whole = _plain(wish)
    exact = [e for e in menu if whole and whole in {_plain(e.id)} | {_plain(e.id[i + 1:])
                                                                       for i, c in enumerate(e.id) if c in "./"}]
    if exact:
        return exact, wish.strip()
    generic = set(FAMILIES) | set(_SAME) | _CHEAPEST | _STRONGEST | {"cli", "local", "ollama", "model", "use", "the"}
    for word in sorted(words - generic, key=len, reverse=True):
        if len(word) < 4:
            continue
        hits = [e for e in menu if word in re.split(r"[-._/:]", e.id.casefold())]
        if len(hits) == 1:
            said = re.search(rf"\b{re.escape(word)}\b", wish, re.I)
            return hits, said.group(0) if said else word
    return [], ""


def resolve_wish(wish: str, needs: str, *, has_tools: bool, menu: list[ModelEntry]) -> Picked | NoModel | Ask:
    """The model a person asked for, in their words (Naveen; SAI2's probe):
    - a model they name outright ("nova pro", "Opus", its id) is theirs, even above the ceiling: its price is shown;
    - a family ("Claude", "gpt") is a kind of model: its best for the need within the ceiling, matched by name or by
      provider, and the card offers a stronger one (`stronger_than`);
    - a rule ("the cheapest", "the strongest"); a CLI or a local model when they say so, or when what they named is
      only there;
    - two families, or nothing it could mean, is a question with the menu's names. Never a guess. A model that can't
      do the job is said, with the real reason."""
    words = set(re.findall(r"[a-z0-9]+", wish.casefold()))
    names = sorted(e.id for e in menu)
    asked_kind = "cli" if "cli" in words else ("local" if words & {"local", "ollama"} else None)
    ordinary = [e for e in menu if e.source not in ("cli", "local")]
    kind_pool = [e for e in menu if e.source == asked_kind] if asked_kind else ordinary

    named, named_as = _named(kind_pool, wish, words)
    if not named and not asked_kind:
        named, named_as = _named(menu, wish, words)
    if len(named) == 1:
        e = named[0]
        if not _fits(e, needs, has_tools):
            return _no_model([e], needs, has_tools, of=e.id) if not e.can_pay else NoModel(
                f"{e.id} can't {'call tools' if has_tools and e.tools == 'none' else 'read images'}, which this agent "
                "needs.")
        return Picked(e.id, f"you asked for {named_as}")

    families = {_SAME.get(w, w) for w in words if _SAME.get(w, w) in FAMILIES}
    if len(families) > 1:
        return Ask("Which model do you mean? This workspace can use: " + ", ".join(names) + ".", names)
    if families:
        family = families.pop()
        part, providers = FAMILIES[family]
        said = next((m.group(0) for w in [family, *[k for k, v in _SAME.items() if v == family]]
                     for m in [re.search(rf"\b{w}\b", wish, re.I)] if m), family)

        def member(e: ModelEntry) -> bool:
            return part in e.id.casefold() or e.provider in providers

        members = [e for e in kind_pool if member(e)] or ([e for e in menu if member(e)] if not asked_kind else [])
        if not members:
            return Ask(f"No {said} model is available here. This workspace can use: " + ", ".join(names) + ".", names)
        fitting = [e for e in members if _fits(e, needs, has_tools)]
        if not fitting:
            return _no_model(members, needs, has_tools, of=said)
        return Picked(_by_need(fitting, needs, ceiling=True, has_tools=has_tools).id, f"you asked for {said}")

    fitting = [e for e in kind_pool if _fits(e, needs, has_tools)]
    if asked_kind:
        if not fitting:
            return Ask(f"No {'CLI' if asked_kind == 'cli' else 'local'} model here can do this. This workspace can "
                       "use: " + ", ".join(names) + ".", names)
        return Picked(_by_need(fitting, needs, ceiling=False, has_tools=has_tools).id,
                      f"you asked for {'a CLI' if asked_kind == 'cli' else 'a local'} model")
    if words & _CHEAPEST and fitting:
        return Picked(sorted(fitting, key=lambda e: _cheapest_first(e, has_tools))[0].id,
                      "you asked for the lowest-cost model")
    if (words & _STRONGEST or "most capable" in " ".join(re.findall(r"[a-z0-9]+", wish.casefold()))) and fitting:
        return Picked(sorted(fitting, key=lambda e: _strongest_first(e, has_tools))[0].id,
                      "you asked for the strongest model")
    return Ask(f"I don't know which model \"{wish.strip()}\" is. This workspace can use: " + ", ".join(names) + ".",
               names)
