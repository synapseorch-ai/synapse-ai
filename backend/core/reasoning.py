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

from typing import NamedTuple

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

#: Headroom left for the visible answer when a budget forces the cap up. The
#: model still has to write something after it finishes thinking.
_ANSWER_HEADROOM = 4096

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
_NATIVE = (
    # Anthropic — extended thinking arrived with 3.7; nothing before it has one.
    "claude-3-7", "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
    # Gemini — 2.5 and everything after.
    "gemini-2.5", "gemini-3",
    # OpenAI reasoning models. gpt-4o and gpt-4.1 have no channel and reject the
    # parameter, so they are absent on purpose.
    "o1", "o3", "o4", "gpt-5",
    # xAI and DeepSeek.
    "grok-3-mini", "grok-4", "deepseek-reasoner",
    # Bedrock-hosted Anthropic and DeepSeek, which carry a region/vendor prefix.
    "bedrock.anthropic.claude-3-7", "bedrock.anthropic.claude-sonnet-4",
    "bedrock.anthropic.claude-opus-4", "bedrock.us.anthropic.claude-3-7",
    "bedrock.us.anthropic.claude-sonnet-4", "bedrock.us.anthropic.claude-opus-4",
    "bedrock.deepseek.r1",
)

#: Providers whose reasoning arrives on the wire without being asked for, so
#: there is nothing to send — only something to read. An OpenAI-compatible
#: endpoint serving a reasoning model returns `reasoning_content` whether or not
#: we set anything, and gpt-oss through our own gateway is exactly that case.
_READ_ONLY_NATIVE = ("synapse.", "oaic.", "locv1.")


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


def supports_native(model: str) -> bool:
    """Whether this model has a reasoning channel we can ask for.

    False here is not "no reasoning" — it is "reasoning stays the prompt
    convention", which is what every model did until now.
    """
    name = (model or "").strip().lower()
    return bool(name) and name.startswith(_NATIVE)


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


def plan_for(
    model: str,
    effort: str | None,
    max_output_tokens: int,
) -> ReasoningPlan | None:
    """The reasoning settings for one call, or None if there are none to send.

    None means "send nothing" — either the model has no channel, or the person
    asked for off and this model can honour that. Both are the same instruction
    to an adapter, so they are the same return value.
    """
    if not supports_native(model):
        return None

    resolved = normalise(effort)
    floor = minimum_budget(model)

    if resolved == "off" and not floor:
        return None

    name = (model or "").strip().lower()
    table = _GEMINI_BUDGET if name.startswith("gemini") else _BUDGET
    budget = max(table[resolved], floor)

    # The cap has to clear the budget with room for an answer after it. Raising
    # it is safe in a way that lowering the budget is not: a smaller budget
    # silently gives the person less thinking than they asked for, while a
    # larger cap only ever costs what the model actually writes.
    cap = max(max_output_tokens, budget + _ANSWER_HEADROOM)

    return ReasoningPlan(
        effort=resolved,
        budget=budget,
        level=_LEVEL[resolved],
        openai_effort=_OPENAI_EFFORT[resolved],
        max_output_tokens=cap,
    )
