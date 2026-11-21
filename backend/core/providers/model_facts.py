"""What each model is, for building: its family, tier, generation and size, and whether it builds well (F1598).

Synapse AI's pickers listed every model alike, and an automatic pick ranked a family's models by price, so a dearer,
older model read as stronger and nothing kept a 4B model from writing. This table is what the product knows of each
model instead, row by row, each from its source: the provider's own tier, or the model card a figure was read from.

**Small**, the rule accepted for it "as proposed" (the measured pass checks it later):
- dense under 30B;
- a mixture of experts only when its active parameters are under 20B and its total is under 200B;
- the nano, lite, micro and flash-lite tiers;
- never Haiku, mini or Flash.

Dense means the family's own paper or card says so. A model whose architecture is not stated is small under 30B
either way, and above that gets no facts-based mark.

**Not for building:** dense models of 9B or less. They are left out of Synapse AI's pickers, as was asked: "let's not
show 8b 4b models". A size named by the provider itself counts (Ministral 3B, Gemma 3 4B).

**Recommended for building**, from facts alone, needs all three:
- the window the model is served with is 200K tokens or more;
- it calls tools natively;
- its size is known and is not small.

A model its provider deprecated, or the product knows is retired, is never recommended for a workspace's own key; a
platform's own model retires by its catalogue instead. A measured fit (`set_measured`) overrides the facts either way,
and the host's own word (`host_fit`, cloud's staff) overrides both.

Rows match like the window table's: the longest prefix over the id and each id its provider prefixes leave, so
`us.anthropic.claude-…`, `cli.claude.…` and `synapse.…` find their row. A newer id that only shares an older one's
opening letters has a row of its own with nothing known (`gpt-5.`, `kimi-k2.`), so it is never read as the older one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from core.model_availability import RETIRED

#: The least served window a model is recommended for building with, from facts alone.
RECOMMEND_WINDOW = 200_000
#: Dense models of this size or less, in billions of parameters, are not for building.
NOT_FOR_BUILDING_B = 9
#: Small: dense under this; a mixture of experts under both of the next two.
SMALL_DENSE_B = 30
SMALL_ACTIVE_B = 20
SMALL_TOTAL_B = 200

FITS = ("recommended", "not_for_building")


@dataclass(frozen=True)
class Row:
    prefix: str
    family: str
    #: Higher is stronger, only where the provider orders its own tiers; None otherwise.
    tier: int | None
    #: The version, newer is larger, as written here (never read from an id at run time); None when not ordered.
    generation: tuple[int, ...] | None
    #: True, False, or None when not known.
    small: bool | None
    #: Billions of parameters, from the model card; `active` only for a mixture of experts.
    total: float | None
    active: float | None
    #: True when the family's own paper or card says dense, False for a mixture of experts, None when not stated.
    dense: bool | None
    #: "not_for_building" for a dense model of 9B or less; None otherwise.
    fit: str | None
    #: The provider's tier ("provider tier") or the card the figures were read from.
    source: str


def is_small(total: float | None, active: float | None, dense: bool | None) -> bool | None:
    """The small rule on a model's parameters (billions): True, False, or None when it cannot say."""
    if total is None:
        return None
    if active is not None or dense is False:
        return (active if active is not None else total) < SMALL_ACTIVE_B and total < SMALL_TOTAL_B
    if dense or total < SMALL_DENSE_B:
        return total < SMALL_DENSE_B
    return None


def _tier(prefix: str, family: str, tier: int, generation: tuple[int, ...] | None, small: bool,
          source: str = "provider tier") -> Row:
    return Row(prefix, family, tier, generation, small, None, None, None, None, source)


def _sized(prefix: str, family: str, total: float | None, active: float | None = None, *, dense: bool | None,
           source: str) -> Row:
    """A row sized by its card, or by the size its provider names it by (Ministral 3B, Gemma 3 4B). A model of 9B or
    less that is not a mixture of experts is not for building, whatever its card says of its architecture."""
    tiny = total is not None and active is None and total <= NOT_FOR_BUILDING_B
    return Row(prefix, family, None, None, is_small(total, active, dense), total, active, dense,
               "not_for_building" if tiny else None, source)


def _unknown(prefix: str, family: str) -> Row:
    """A newer id that shares an older one's opening letters: nothing known, so never read as the older one."""
    return Row(prefix, family, None, None, None, None, None, None, None, "provider tier")


_CLAUDE = "https://platform.claude.com/docs/en/about-claude/models/overview"
_NOVA = "https://docs.aws.amazon.com/nova/latest/userguide/what-is-nova.html"
_GEMINI = "https://ai.google.dev/gemini-api/docs/models/"
_LLAMA31 = "https://www.llama.com/docs/model-cards-and-prompt-formats/llama3_1/"
_LLAMA32 = "https://www.llama.com/docs/model-cards-and-prompt-formats/llama3_2/"
_LLAMA33 = "https://www.llama.com/docs/model-cards-and-prompt-formats/llama3_3/"
_LLAMA3 = "https://www.llama.com/docs/model-cards-and-prompt-formats/other-models/#meta-llama-3"
_LLAMA4 = "https://www.llama.com/docs/model-cards-and-prompt-formats/llama4/"
_HF = "https://huggingface.co/"
_MISTRAL = "https://docs.mistral.ai/getting-started/models/models_overview/"
_GEMMA3 = "https://ai.google.dev/gemma/docs/core/model_card_3"
#: Ollama's library: each tag page says which model a tag is, and which is its `:latest`.
_OLLAMA = "https://ollama.com/library/"

ROWS: tuple[Row, ...] = (
    # Anthropic: Fable and Mythos above Opus, Opus above Sonnet, Sonnet above Haiku; none of them small.
    *(_tier(p, "claude", 5, g, False, _CLAUDE) for p, g in (
        ("claude-fable-5", (5,)), ("claude-fable-5-1", (5, 1)), ("claude-fable-5-2", (5, 2)),
        ("claude-mythos-5", (5,)), ("claude-mythos-5-1", (5, 1)), ("claude-mythos-5-2", (5, 2)))),
    *(_tier(p, "claude", 4, g, False, _CLAUDE) for p, g in (
        ("claude-opus-5", (5,)), ("claude-opus-5-5", (5, 5)), ("claude-opus-4-8", (4, 8)), ("claude-opus-4-7", (4, 7)),
        ("claude-opus-4-6", (4, 6)), ("claude-opus-4-5", (4, 5)), ("claude-opus-4-1", (4, 1)),
        ("claude-opus-4", (4,)))),
    *(_tier(p, "claude", 3, g, False, _CLAUDE) for p, g in (
        ("claude-sonnet-5", (5,)), ("claude-sonnet-5-5", (5, 5)), ("claude-sonnet-4-6", (4, 6)),
        ("claude-sonnet-4-5", (4, 5)), ("claude-sonnet-4", (4,)), ("claude-3-7-sonnet", (3, 7)))),
    *(_tier(p, "claude", 2, g, False, _CLAUDE) for p, g in (
        ("claude-haiku-5", (5,)), ("claude-haiku-5-5", (5, 5)), ("claude-haiku-4-5", (4, 5)),
        ("claude-3-5-haiku", (3, 5)))),
    # OpenAI: the flagship, mini and nano tiers; nano is small. A point release is a row of its own, or nothing known.
    _tier("gpt-6", "gpt", 3, (6,), False), _tier("gpt-6.1", "gpt", 3, (6, 1), False),
    _tier("gpt-6-mini", "gpt", 2, (6,), False), _tier("gpt-6-nano", "gpt", 1, (6,), True), _unknown("gpt-6.", "gpt"),
    _tier("gpt-5", "gpt", 3, (5,), False), _tier("gpt-5-mini", "gpt", 2, (5,), False),
    _tier("gpt-5-nano", "gpt", 1, (5,), True), _unknown("gpt-5.", "gpt"),
    _tier("gpt-4.1", "gpt", 3, (4, 1), False), _tier("gpt-4.1-mini", "gpt", 2, (4, 1), False),
    _tier("gpt-4.1-nano", "gpt", 1, (4, 1), True),
    _tier("gpt-4o", "gpt", 3, (4, 0), False), _tier("gpt-4o-mini", "gpt", 2, (4, 0), False),
    # Google: Pro, Flash and Flash-Lite; Flash-Lite is small.
    _tier("gemini-2.5-pro", "gemini", 3, (2, 5), False, _GEMINI + "gemini-2.5-pro"),
    _tier("gemini-2.5-flash", "gemini", 2, (2, 5), False, _GEMINI + "gemini-2.5-flash"),
    _tier("gemini-2.5-flash-lite", "gemini", 1, (2, 5), True, _GEMINI + "gemini-2.5-flash-lite"),
    _tier("gemini-3-pro", "gemini", 3, (3,), False), _tier("gemini-3-flash", "gemini", 2, (3,), False),
    _tier("gemini-3.1-pro", "gemini", 3, (3, 1), False), _tier("gemini-3.1-flash-lite", "gemini", 1, (3, 1), True),
    # Amazon: Premier, Pro, Lite and Micro; Lite and Micro are small.
    _tier("nova-premier", "nova", 4, (1,), False, _NOVA), _tier("nova-pro", "nova", 3, (1,), False, _NOVA),
    _tier("nova-lite", "nova", 2, (1,), True, _NOVA), _tier("nova-micro", "nova", 1, (1,), True, _NOVA),
    _tier("nova-2-lite", "nova", 2, (2,), True, _NOVA),
    # Meta: Llama 3.x is dense (its paper); Llama 4 is a mixture of experts. Bedrock spells 3.1 `llama3-1-`, a
    # platform `llama-3-1-`.
    *(_sized(p, "llama", 8.0, dense=True, source=_LLAMA31) for p in ("llama3-1-8b", "llama-3.1-8b", "llama-3-1-8b")),
    *(_sized(p, "llama", 70.0, dense=True, source=_LLAMA31) for p in ("llama3-1-70b", "llama-3.1-70b", "llama-3-1-70b")),
    *(_sized(p, "llama", 405.0, dense=True, source=_LLAMA31)
      for p in ("llama3-1-405b", "llama-3.1-405b", "llama-3-1-405b")),
    *(_sized(p, "llama", 1.23, dense=True, source=_LLAMA32) for p in ("llama3-2-1b", "llama-3.2-1b", "llama-3-2-1b")),
    *(_sized(p, "llama", 3.21, dense=True, source=_LLAMA32) for p in ("llama3-2-3b", "llama-3.2-3b", "llama-3-2-3b")),
    *(_sized(p, "llama", 70.0, dense=True, source=_LLAMA33) for p in ("llama3-3-70b", "llama-3.3-70b", "llama-3-3-70b")),
    *(_sized(p, "llama", 8.0, dense=True, source=_LLAMA3) for p in ("llama3-8b", "llama-3-8b")),
    *(_sized(p, "llama", 70.0, dense=True, source=_LLAMA3) for p in ("llama3-70b", "llama-3-70b")),
    *(_sized(p, "llama", 109.0, 17.0, dense=False, source=_LLAMA4) for p in ("llama4-scout", "llama-4-scout")),
    *(_sized(p, "llama", 400.0, 17.0, dense=False, source=_LLAMA4) for p in ("llama4-maverick", "llama-4-maverick")),
    # OpenAI's open weights: mixtures of experts.
    _sized("gpt-oss-120b", "gpt-oss", 117.0, 5.1, dense=False, source=_HF + "openai/gpt-oss-120b"),
    _sized("gpt-oss-20b", "gpt-oss", 21.0, 3.6, dense=False, source=_HF + "openai/gpt-oss-20b"),
    # Qwen3: its cards say dense or a mixture.
    _sized("qwen3-235b-a22b", "qwen3", 235.0, 22.0, dense=False, source=_HF + "Qwen/Qwen3-235B-A22B"),
    _sized("qwen3-30b-a3b", "qwen3", 30.5, 3.3, dense=False, source=_HF + "Qwen/Qwen3-30B-A3B"),
    _sized("qwen3-coder-480b", "qwen3", 480.0, 35.0, dense=False, source=_HF + "Qwen/Qwen3-Coder-480B-A35B-Instruct"),
    _sized("qwen3-32b", "qwen3", 32.8, dense=True, source=_HF + "Qwen/Qwen3-32B"),
    _sized("qwen3-14b", "qwen3", 14.8, dense=True, source=_HF + "Qwen/Qwen3-14B"),
    _sized("qwen3-8b", "qwen3", 8.2, dense=True, source=_HF + "Qwen/Qwen3-8B"),
    _sized("qwen3-4b", "qwen3", 4.0, dense=True, source=_HF + "Qwen/Qwen3-4B"),
    # DeepSeek, Kimi, GLM and MiniMax: mixtures of experts.
    _sized("deepseek-v3", "deepseek", 671.0, 37.0, dense=False, source=_HF + "deepseek-ai/DeepSeek-V3"),
    _sized("deepseek-r1", "deepseek", 671.0, 37.0, dense=False, source=_HF + "deepseek-ai/DeepSeek-R1"),
    _sized("kimi-k2", "kimi", 1000.0, 32.0, dense=False, source=_HF + "moonshotai/Kimi-K2-Instruct"),
    _unknown("kimi-k2.", "kimi"),
    _sized("glm-4.5", "glm", 355.0, 32.0, dense=False, source=_HF + "zai-org/GLM-4.5"),
    _sized("glm-4.5-air", "glm", 106.0, 12.0, dense=False, source=_HF + "zai-org/GLM-4.5"),
    _sized("minimax-m1", "minimax", 456.0, 45.9, dense=False, source=_HF + "MiniMaxAI/MiniMax-M1-80k"),
    _sized("minimax-m2", "minimax", 230.0, 10.0, dense=False, source=_HF + "MiniMaxAI/MiniMax-M2"),
    _unknown("minimax-m2.", "minimax"),
    # Mistral: Large 2 is dense (its 24.11 card); the rest do not say, and under 30B are small either way.
    _sized("mistral-large-2407", "mistral", 123.0, dense=True, source=_HF + "mistralai/Mistral-Large-Instruct-2407"),
    _sized("mistral-large-2411", "mistral", 123.0, dense=True, source=_HF + "mistralai/Mistral-Large-Instruct-2411"),
    _sized("mistral-small-2501", "mistral", 24.0, dense=None, source=_HF + "mistralai/Mistral-Small-24B-Instruct-2501"),
    _sized("mistral-small-2503", "mistral", 24.0, dense=None,
           source=_HF + "mistralai/Mistral-Small-3.1-24B-Instruct-2503"),
    _sized("mistral-small-2506", "mistral", 24.0, dense=None,
           source=_HF + "mistralai/Mistral-Small-3.2-24B-Instruct-2506"),
    _sized("ministral-8b", "mistral", 8.02, dense=None, source=_HF + "mistralai/Ministral-8B-Instruct-2410"),
    _sized("ministral-3b", "mistral", 3.0, dense=None, source=_MISTRAL),
    _sized("mistral-7b", "mistral", 7.3, dense=None, source=_HF + "mistralai/Mistral-7B-Instruct-v0.3"),
    _sized("mixtral-8x7b", "mistral", 46.7, 12.9, dense=False, source=_MISTRAL),
    _sized("pixtral-12b", "mistral", 12.4, dense=None, source=_HF + "mistralai/Pixtral-12B-2409"),
    _sized("pixtral-large", "mistral", 124.0, dense=None, source=_HF + "mistralai/Pixtral-Large-Instruct-2411"),
    _sized("devstral-small", "mistral", 24.0, dense=None, source=_HF + "mistralai/Devstral-Small-2505"),
    # Google's open weights: Gemma 3's card names each size.
    _sized("gemma-3-1b", "gemma", 1.0, dense=None, source=_GEMMA3),
    _sized("gemma-3-4b", "gemma", 4.0, dense=None, source=_GEMMA3),
    _sized("gemma-3-12b", "gemma", 12.0, dense=None, source=_GEMMA3),
    _sized("gemma-3-27b", "gemma", 27.0, dense=None, source=_GEMMA3),
    # NVIDIA: the Nano models are small; the larger ones do not say their architecture, so no facts-based mark.
    _sized("llama-3.1-nemotron-nano-8b", "nemotron", 8.0, dense=None,
           source=_HF + "nvidia/Llama-3.1-Nemotron-Nano-8B-v1"),
    _sized("nvidia-nemotron-nano-9b", "nemotron", 9.0, dense=None, source=_HF + "nvidia/NVIDIA-Nemotron-Nano-9B-v2"),
    _sized("llama-3.1-nemotron-70b", "nemotron", 70.0, dense=None,
           source=_HF + "nvidia/Llama-3.1-Nemotron-70B-Instruct-HF"),
    _sized("llama-3_3-nemotron-super-49b", "nemotron", 49.0, dense=None,
           source=_HF + "nvidia/Llama-3_3-Nemotron-Super-49B-v1"),
    # Ollama's own spellings of the common local models (F1598), each tag as its tags page names it, and its
    # `:latest` where that is the same model (a name alone is its `:latest`, `_ids`). Sizes are the cards' (the rows
    # above): the dense ones of 9B or less are not for building, so Synapse AI's picker leaves them out.
    *(_sized(p, "llama", 8.0, dense=True, source=_OLLAMA + "llama3.1/tags")
      for p in ("llama3.1:8b", "llama3.1:latest")),
    *(_sized(p, "llama", 3.0, dense=True, source=_OLLAMA + "llama3.2/tags")
      for p in ("llama3.2:3b", "llama3.2:latest")),
    _sized("llama3.2:1b", "llama", 1.0, dense=True, source=_OLLAMA + "llama3.2/tags"),
    *(_sized(p, "gemma", 4.0, dense=None, source=_OLLAMA + "gemma3/tags") for p in ("gemma3:4b", "gemma3:latest")),
    _sized("gemma3:1b", "gemma", 1.0, dense=None, source=_OLLAMA + "gemma3/tags"),
    _sized("gemma3:270m", "gemma", 0.27, dense=None, source=_OLLAMA + "gemma3/tags"),
    _sized("gemma3:12b", "gemma", 12.0, dense=None, source=_OLLAMA + "gemma3/tags"),
    *(_sized(p, "qwen3", 8.2, dense=True, source=_OLLAMA + "qwen3/tags") for p in ("qwen3:8b", "qwen3:latest")),
    _sized("qwen3:4b", "qwen3", 4.0, dense=True, source=_OLLAMA + "qwen3/tags"),
    _sized("qwen3:1.7b", "qwen3", 1.7, dense=True, source=_OLLAMA + "qwen3/tags"),
    _sized("qwen3:0.6b", "qwen3", 0.6, dense=True, source=_OLLAMA + "qwen3/tags"),
    _sized("qwen3:30b", "qwen3", 30.5, 3.3, dense=False, source=_OLLAMA + "qwen3/tags"),
    *(_sized(p, "phi", 3.8, dense=True, source=_OLLAMA + "phi4-mini/tags")
      for p in ("phi4-mini:3.8b", "phi4-mini:latest")),
    *(_sized(p, "mistral", 7.0, dense=True, source=_OLLAMA + "mistral/tags")
      for p in ("mistral:7b", "mistral:latest", "mistral:instruct", "mistral:text", "mistral:v0.")),
    # The first-run tip's words trace here: 14 GB and 128K on its tags page; 16 GB of memory on its card ("run within
    # 16GB of memory") and Ollama's page ("as little as 16GB memory").
    *(_sized(p, "gpt-oss", 21.0, 3.6, dense=False,
             source=f"{_OLLAMA}gpt-oss/tags and {_OLLAMA}gpt-oss and {_HF}openai/gpt-oss-20b")
      for p in ("gpt-oss:20b", "gpt-oss:latest")),
)

#: Models their provider deprecated, for a workspace's own key, from each provider's deprecations page; the product's
#: own `model_availability.RETIRED` is read too. A dated id (`gpt-5-2025-08-07`) is its model.
DEPRECATED: frozenset[str] = frozenset({
    "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1-nano", "gemini-2.5-pro", "gemini-2.5-flash",
    "mistral-large-2407", "mistral-large-2411", "mistral-large-2402", "mistral-small-2501", "mistral-small-2503",
    "mistral-small-2506", "ministral-8b-2410", "ministral-3b-2410", "pixtral-12b-2409", "pixtral-large-2411",
    "devstral-small-2505",
})
_DATED = re.compile(r"-\d{4}-?\d{2}-?\d{2}$")


def _ids(model_id: str) -> list[str]:
    from core.providers.cli_aliases import resolved

    model_id = (model_id or "").strip().lower()
    model_id = resolved(model_id) or model_id  # a CLI's alias is the model it runs (F1598 D)
    if model_id.startswith("ollama.") and ":" not in model_id:
        model_id += ":latest"  # Ollama's name alone is its `:latest` tag (F1598)
    return [model_id, *(model_id[n + 1:] for n, c in enumerate(model_id) if c in "./")] if model_id else []


def _longest(model_id: str, table) -> tuple | None:
    found = [(len(key), value) for key, value in table for i in _ids(model_id) if i.startswith(key.lower())]
    return max(found, key=lambda f: f[0])[1] if found else None


def size_of(model_id: str) -> Row | None:
    """`model_id`'s row, or None when the table does not know it."""
    return _longest(model_id, ((r.prefix, r) for r in ROWS))


def deprecated(model_id: str) -> bool:
    """Whether a workspace's own key's `model_id` is one its provider deprecated or the product knows is retired."""
    for i in _ids(model_id):
        bare = _DATED.sub("", i)
        if bare in DEPRECATED or i in RETIRED or bare in RETIRED:
            return True
    return False


# ── the measured pass ───────────────────────────────────────────────────────────────────────────────────────────────

#: The measured pass (F1598 D), by id prefix: `(fit, rank)`, order only. Four models built two real asks on a scratch
#: install (a support assistant over docs and an order API; a refund that waits for an approval before it is sent);
#: a third ask, a change to an existing form, was not measured. The Claude models ran through the Claude CLI, Gemini 3
#: Flash through its API. Rank = 100 x asks right + 10 x a judge's mean helpfulness (1-5): Sonnet 4.6 right on one,
#: helpfulness 4 and 5; Haiku 4.5 right on one, 5 and 1; Haiku 5.5 right on one, 3 and 2; Gemini 3 Flash right on
#: none, 5 and 2. No ask was right on every model, and none on the refund. It sets no fit: "Recommended" stays the
#: facts' call, and the rank orders only the measured models of one family's tier (`models_menu.ordered`).
MEASURED: dict[str, tuple[str | None, int | None]] = {
    "claude-sonnet-4-6": (None, 145),
    "claude-haiku-4-5": (None, 130),
    "claude-haiku-5-5": (None, 125),
    "gemini-3-flash-preview": (None, 35),
}

#: What the measured pass said, by id prefix: `(fit or None, rank or None)`, a higher rank stronger.
_measured: dict[str, tuple[str | None, int | None]] = dict(MEASURED)


def set_measured(rows: dict[str, tuple[str | None, int | None]] | None) -> None:
    """The measured rows to read: `rows`, or `{}` for none; None puts the shipped table (`MEASURED`) back."""
    global _measured
    rows = MEASURED if rows is None else rows
    _measured = {k: (v[0] if v[0] in FITS else None, v[1]) for k, v in rows.items()}


def measured(model_id: str) -> tuple[str | None, int | None] | None:
    """What the measured pass said of `model_id`, or None when it was not measured."""
    return _longest(model_id, _measured.items())


def builder_fit(model_id: str, *, window: int | None, tools: str, host_fit: str | None = None) -> str | None:
    """Whether `model_id` builds well: "recommended", "not_for_building", or None (no mark). The host's word first,
    then the measured pass, then the table's own mark, then the facts: a served window of `RECOMMEND_WINDOW` or more,
    native tools, a known size that is not small, and, for a workspace's own key, not deprecated."""
    if host_fit in FITS:
        return host_fit
    said = measured(model_id)
    if said is not None and said[0] is not None:
        return said[0]
    row = size_of(model_id)
    if row is None:
        return None
    if row.fit is not None:
        return row.fit
    platform = (model_id or "").strip().lower().startswith("synapse.")
    if row.small is False and tools == "native" and window is not None and window >= RECOMMEND_WINDOW \
            and (platform or not deprecated(model_id)):
        return "recommended"
    return None
