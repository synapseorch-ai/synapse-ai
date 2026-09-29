"""
Reasoning, as the provider does it rather than as a prompt convention.

Until now "reasoning" in this engine meant one thing: the system prompt asked
the model to write `[REASONING]...[/REASONING]` around its thinking, and
`react_engine.strip_reasoning` pulled it back out. That is a convention, not a
capability — it works as well as the model's willingness to follow it, which is
why it reads well on Claude and badly on Gemini. Meanwhile every major provider
grew a real one: a separate channel the model thinks in, with a budget we set
and tokens we are billed for.

This module owns the *policy* — which models have that channel, and what "low"
or "high" means in each one's units. The provider adapters own the *shape*,
because a `ThinkingConfig` object, an `additionalModelRequestFields` dict and a
`reasoning_effort` string have nothing in common but their meaning. Splitting it
that way is the same split `model_pricing.py` already makes: the rate card lives
in one table, the arithmetic lives where the call is.

Two things are worth knowing before editing the ladder below.

**A budget must be strictly under the response cap.** Anthropic and Bedrock both
reject `budget_tokens >= max_tokens`, and `DEFAULT_MAX_OUTPUT_TOKENS` is 8192 —
under the `high` budget. So `plan_for` takes the cap, may raise it, and returns
both. Returning them apart is how you ship a silent 400 at one effort level.

**"Off" is not always zero.** Gemini 2.5 Pro cannot stop thinking; its floor is
128 tokens and a budget of 0 is refused. Asking for "off" on a model that has no
off is answered with its minimum, not with an error — the setting is a
preference, and a model that cannot honour it should still run.
"""
from __future__ import annotations

from typing import Any, NamedTuple

#: What a person can choose. `None` on an agent means "inherit the workspace
#: setting"; the setting itself is always one of these.
EFFORT = ("off", "low", "medium", "high")

DEFAULT_EFFORT = "medium"

#: Thinking tokens per effort level. Anthropic and Bedrock take these directly;
#: Gemini 2.5 takes them as `thinking_budget`. The floor of 2048 at "low" is
#: above Anthropic's 1024 minimum on purpose — a budget the model cannot finish
#: a thought in is worse than none, because it is billed either way.
_BUDGET = {"off": 0, "low": 2048, "medium": 6144, "high": 16384}

#: Gemini 2.5 is more generous per token than Anthropic is, and its own
#: documented ceiling is higher, so it gets its own column rather than sharing.
_GEMINI_BUDGET = {"off": 0, "low": 2048, "medium": 8192, "high": 24576}

#: Gemini 3 replaced the raw budget with a named level, and OpenAI has always
#: used one. The names line up with ours exactly, which is why the ladder has
#: four rungs and not three or five.
_LEVEL = {"off": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}

_OPENAI_EFFORT = {"off": "minimal", "low": "low", "medium": "medium", "high": "high"}


def _openai_off(name: str) -> str | None:
    """What Off sends an OpenAI reasoning model — the least each accepts (F293).

    Measured 2026-09-30 on the bat-and-ball question: sent nothing, gpt-5-nano
    thought 256–384 tokens and gpt-5-mini 64–128, against none at `minimal`;
    gpt-5.x takes `none` (not `minimal`, a 400) and thinks none by default; the
    o-series refuses both and its least is `low`. None: not an OpenAI reasoning
    model, and nothing here to say.
    """
    if name.startswith(("o1", "o3", "o4")):
        return "low"
    if name.startswith("gpt-5."):
        return "none"
    if name.startswith("gpt-5"):
        return "minimal"
    return None

#: Headroom left for the visible answer when a budget forces the cap up. The
#: model still has to write something after it finishes thinking.
_ANSWER_HEADROOM = 4096

#: What Gemini 3 is expected to think at each level, for sizing the cap (F263).
#: It takes a level, not a budget, so nothing we send bounds its thinking — only
#: the cap does, and the cap counts the thinking too. Sized from `_GEMINI_BUDGET`
#: it gave medium 12,288 in all, and a long 3.1 Pro turn thought through every
#: token of it and answered nothing (stress pass S2). Never sent: a larger cap
#: costs only what the model actually writes.
_GEMINI3_THINKING = {"off": 4096, "low": 8192, "medium": 24576, "high": 49152}

#: The most a model family will write in one response. A cap above it is lowered
#: to it rather than sent: Gemini refuses a `max_output_tokens` past its ceiling.
_OUTPUT_CEILING = {
    "gemini-2.5": 65536,
    "gemini-3": 65536,
}


def output_ceiling(model: str) -> int | None:
    """This model's own largest response, where we know it."""
    name = (model or "").strip().lower()
    for prefix, ceiling in _OUTPUT_CEILING.items():
        if name.startswith(prefix):
            return ceiling
    return None

#: Models whose thinking cannot be switched off, and the floor they insist on.
#: Matched by prefix, longest first, like every other table here.
_MINIMUM_BUDGET = {
    "gemini-2.5-pro": 128,
}

#: Prefixes with a native reasoning channel. Anything not matched here falls
#: back to the `[REASONING]` prompt convention, which still works.
#:
#: Deliberately a prefix list and not a regex: a new model in a family we
#: already support should light up on the day it ships, without an edit here.
#: Anthropic families that take `thinking: {type: "enabled", budget_tokens}`.
_BUDGET_CLAUDE = ("claude-3-7", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4")

#: Anthropic models that take adaptive thinking with an effort instead (F9). On
#: Claude 5 and Opus 4.7/4.8 `budget_tokens` is a 400; on the 4.6 pair it is
#: deprecated. Matched before `_BUDGET_CLAUDE`, whose `claude-opus-4` would
#: otherwise claim `claude-opus-4-8`. From the claude-api reference, 2026-09-17.
_ADAPTIVE_CLAUDE = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)

#: Of those, the ones that think when `thinking` is omitted — so "off" cannot be
#: honoured by sending nothing, and the reference advises against disabling it
#: on Opus 5 (tool calls written as text, leaked tags). "Off" runs at low.
_THINKS_BY_DEFAULT = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5")


def _gemini_3_pro(name: str) -> bool:
    """A Gemini 3 Pro, which cannot stop thinking (F269).

    `thinking_level: MINIMAL` is a 400 on it ("Thinking level MINIMAL is not
    supported for this model", gemini-3.1-pro-preview, measured 2026-09-28), and
    LOW thought as much as sending nothing (329 against 298 output tokens on the
    same question). Its Off is its Low.
    """
    return name.startswith("gemini-3") and "-pro" in name

#: How a Claude model on the Anthropic API takes a thinking plan.
ADAPTIVE = "adaptive"
BUDGET = "budget"

#: Bedrock-hosted Anthropic, in every cross-region form Bedrock names — the
#: geos cloud's catalog sync matches. An inference profile is picked as a model
#: (F66), so a jp. profile has to read like its us. twin.
_BEDROCK_ANTHROPIC = tuple(
    f"bedrock.{geo}anthropic." for geo in ("", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "global.")
)

_NATIVE = (
    # Anthropic — extended thinking arrived with 3.7; nothing before it has one.
    *_BUDGET_CLAUDE, *_ADAPTIVE_CLAUDE,
    # Gemini — 2.5 and everything after.
    "gemini-2.5", "gemini-3",
    # OpenAI reasoning models. gpt-4o and gpt-4.1 have no channel and reject the
    # parameter, so they are absent on purpose.
    "o1", "o3", "o4", "gpt-5",
    # xAI and DeepSeek.
    "grok-3-mini", "grok-4", "deepseek-reasoner",
    # Bedrock-hosted Anthropic (budget families only — see `supports_native`)
    # and DeepSeek, which carry a region/vendor prefix.
    *(vendor + family for vendor in _BEDROCK_ANTHROPIC for family in _BUDGET_CLAUDE),
    "bedrock.deepseek.r1",
)

#: Providers whose reasoning arrives on the wire without being asked for, so
#: there is nothing to send — only something to read. An OpenAI-compatible
#: endpoint serving a reasoning model returns `reasoning_content` whether or not
#: we set anything, and gpt-oss through our own gateway is exactly that case.
_READ_ONLY_NATIVE = ("synapse.", "oaic.", "locv1.")

#: How a model takes the ladder — what `reasoning_kind` serves to the screens.
#:
#: `LEVELS` is every prefix in `_NATIVE`: off, low, medium and high all mean
#: something. The other two exist for models a deployment has *measured* and
#: listed in `settings["reasoning_models"]` (`{model id: kind}`), because an
#: OpenAI-compatible gateway accepts `reasoning_effort` for every model and
#: honours it for few — Bedrock ignored an invented key without complaint, so
#: acceptance proves nothing and only a measurement does (cloud's
#: `scripts/probe-bedrock-reasoning.py`, 2026-09-14). Two shapes came out:
#:
#: * `GRADED` — low, medium and high think progressively more, and the model
#:   cannot be switched off (gpt-oss refuses `minimal`), so off runs at low.
#: * `SWITCH` — the model thinks only when asked for high; low and medium were
#:   indistinguishable from off (qwen3, glm, kimi-k2.5, nemotron, deepseek-v3.2).
LEVELS = "levels"
GRADED = "graded"
SWITCH = "switch"
_LISTED_KINDS = (GRADED, SWITCH)


class ReasoningPlan(NamedTuple):
    """What this call should ask the provider for.

    `budget` and `level` are both populated because a provider family can switch
    between them by generation — Gemini 2.5 reads the budget, Gemini 3 reads the
    level — and the adapter, not this module, knows which one it is talking to.
    """
    effort: str
    #: Thinking tokens. Zero means "asked for off"; an adapter for a model that
    #: cannot be turned off substitutes its floor via `minimum_budget`.
    budget: int
    #: `MINIMAL` | `LOW` | `MEDIUM` | `HIGH`.
    level: str
    #: OpenAI's spelling of the same thing.
    openai_effort: str
    #: The response cap this call must use — raised above the caller's if the
    #: budget would not otherwise fit under it.
    max_output_tokens: int

    @property
    def enabled(self) -> bool:
        return self.effort != "off"


def normalise(effort: str | None, fallback: str = DEFAULT_EFFORT) -> str:
    """Coerce anything stored on an agent or in settings into the ladder.

    Unknown values become the fallback rather than raising: this reads a JSON
    blob a person or an older version may have written, and a chat turn is not
    the place to discover a typo.
    """
    value = (effort or "").strip().lower()
    return value if value in EFFORT else fallback


def listed_kind(model: str, listed: Any = None) -> str | None:
    """This model's kind in a deployment's `reasoning_models`, or None.

    Anything malformed reads as unlisted: the value is JSON a person or an
    older version wrote, and a turn is not the place to discover a typo.
    """
    if not isinstance(listed, dict):
        return None
    name = (model or "").strip().lower()
    for key, kind in listed.items():
        if isinstance(key, str) and key.strip().lower() == name and kind in _LISTED_KINDS:
            return str(kind)
    return None


def supports_native(model: str, listed: Any = None) -> bool:
    """Whether this model has a reasoning channel we can ask for.

    False here is not "no reasoning" — it is "reasoning stays the prompt
    convention", which is what every model did until now. `listed` is
    `settings["reasoning_models"]`; see `GRADED` and `SWITCH`.
    """
    name = (model or "").strip().lower()
    if name.startswith(_BEDROCK_ANTHROPIC) and _bedrock_claude(name).startswith(_ADAPTIVE_CLAUDE):
        # Budget-less Claude on Bedrock: `budget_tokens` is refused, and the
        # Converse shape for adaptive thinking was not documented where F9 was
        # checked. The prompt convention works; a guessed field would 400.
        return listed_kind(name, listed) is not None
    return bool(name) and (name.startswith(_NATIVE) or listed_kind(name, listed) is not None)


def _bedrock_claude(name: str) -> str:
    """`bedrock.eu.anthropic.claude-opus-4-8-v1:0` → `claude-opus-4-8-v1:0`."""
    return name.split("anthropic.", 1)[1] if "anthropic." in name else name


def anthropic_thinking(model: str) -> str | None:
    """`ADAPTIVE` or `BUDGET` for a Claude model on the Anthropic API, else None.

    The policy the Anthropic adapter asks: adaptive models get
    `thinking: {type: "adaptive"}` and `output_config.effort`; the rest get a
    `budget_tokens`.
    """
    name = (model or "").strip().lower()
    if name.startswith(_ADAPTIVE_CLAUDE):
        return ADAPTIVE
    if name.startswith(_BUDGET_CLAUDE):
        return BUDGET
    return None


def reasoning_kind(model: str, listed: Any = None) -> str | None:
    """How the screens should offer effort for this model, or None for no control."""
    kind = listed_kind(model, listed)
    if kind is not None:
        return kind
    return LEVELS if supports_native(model) else None


def reads_native(model: str) -> bool:
    """Whether a reply from this model may carry reasoning we should read.

    Wider than `supports_native`: an OpenAI-compatible endpoint in front of a
    reasoning model returns `reasoning_content` unprompted, and dropping it —
    which is what happened until now — loses the one thing the panel exists for.
    """
    name = (model or "").strip().lower()
    return supports_native(name) or name.startswith(_READ_ONLY_NATIVE)


def minimum_budget(model: str) -> int:
    """The smallest budget this model accepts. Zero when it can be turned off."""
    name = (model or "").strip().lower()
    for prefix, floor in _MINIMUM_BUDGET.items():
        if name.startswith(prefix):
            return floor
    return 0


def off_runs_as(model: str, listed: Any = None) -> str | None:
    """What reasoning effort "off" does on this model, for the screens (F269).

    - "off": the model stops thinking.
    - "minimum": it runs at its lowest thinking level, which may think a little
      (Gemini 3 Flash at MINIMAL, Gemini 2.5 Pro at its 128-token floor).
    - "low": its least is Low, so an Off would be Low — the screens then offer
      no Off at all, rather than one that quietly means Low (F65).
    - None: no reasoning channel, so nothing to say.

    Served from here, like `reasoning_capable`, so the screens never restate it.
    """
    if not supports_native(model, listed):
        return None
    name = (model or "").strip().lower()
    kind = listed_kind(name, listed)
    if kind == GRADED:
        return "low"
    if kind == SWITCH:
        return "off"
    if name.startswith(_THINKS_BY_DEFAULT) or _gemini_3_pro(name) or _openai_off(name) == "low":
        return "low"
    if minimum_budget(name) or name.startswith("gemini-3") or _openai_off(name) == "minimal":
        return "minimum"
    return "off"


def plan_for(
    model: str,
    effort: str | None,
    max_output_tokens: int,
    listed: Any = None,
) -> ReasoningPlan | None:
    """The reasoning settings for one call, or None if there are none to send.

    None means "send nothing" — either the model has no channel, or the person
    asked for off and this model can honour that. Both are the same instruction
    to an adapter, so they are the same return value.
    """
    if not supports_native(model, listed):
        return None

    resolved = normalise(effort)
    kind = listed_kind(model, listed)
    if kind == SWITCH and resolved != "high":
        # Not a plan with the channel off: a plan drops the `[REASONING]`
        # prompt convention, and this model would then not think either.
        return None
    if kind == GRADED and resolved == "off":
        resolved = "low"
    name = (model or "").strip().lower()
    if resolved == "off" and (name.startswith(_THINKS_BY_DEFAULT) or _gemini_3_pro(name)
                              or _openai_off(name) == "low"):
        resolved = "low"
    floor = minimum_budget(model)
    # Off on OpenAI is said too (F293): nothing sent is each model's default
    # effort — medium on gpt-5 and the o-series — and it left the `[REASONING]`
    # convention taught on top.
    openai_off = _openai_off(name) if resolved == "off" else None

    # Off is said, not left unsaid, where saying nothing is not off (F269). A
    # Gemini model sent no thinking config thinks dynamically — 263–299 output
    # tokens on a one-line answer, against 9 with MINIMAL on gemini-3-flash and
    # with a budget of 0 on gemini-2.5-flash (measured 2026-09-28). A plan also
    # drops the `[REASONING]` prompt convention, which an Off turn used to be
    # taught on top of its own thinking. Everywhere else — Claude's budget
    # models above all — omitting it is off, so nothing is sent.
    if resolved == "off" and not floor and not name.startswith("gemini") and not openai_off:
        return None

    table = _GEMINI_BUDGET if name.startswith("gemini") else _BUDGET
    budget = max(table[resolved], floor)
    # What the cap must make room for: the budget where one is sent, and for
    # Gemini 3, which is sent a level, what that level is expected to think.
    thinking = _GEMINI3_THINKING[resolved] if name.startswith("gemini-3") else budget

    # The cap has to clear the thinking with room for an answer after it. Raising
    # it is safe in a way that lowering the budget is not: a smaller budget
    # silently gives the person less thinking than they asked for, while a
    # larger cap only ever costs what the model actually writes.
    cap = max(max_output_tokens, thinking + _ANSWER_HEADROOM)
    ceiling = output_ceiling(name)
    if ceiling is not None:
        cap = min(cap, ceiling)

    return ReasoningPlan(
        effort=resolved,
        budget=budget,
        level=_LEVEL[resolved],
        openai_effort=openai_off or _OPENAI_EFFORT[resolved],
        max_output_tokens=cap,
    )
