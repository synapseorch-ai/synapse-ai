"""Each model's input window, in tokens: the one source the model pick (F1251) and a run's context budget (D339) read.

A host states its own models' windows over this (cloud: its platform rows, through `models_menu.set_model_facts`'s
`window`). None is "not known": the pick never rules a model out for it, and D339 budgets a default.
"""
from __future__ import annotations

#: Known input windows: `(id prefix, tokens)`, matched against a model's id and each id its provider's prefix leaves
#: (`us.anthropic.claude-haiku-4-5` is also `claude-haiku-4-5`); the longest prefix that matches wins. The rows are
#: D339's (TRI1), each from its provider's docs.
WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic: every current Claude model's standard window (a 1M beta is opt-in, not assumed).
    ("claude-", 200_000),
    # OpenAI.
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("o3", 200_000),
    ("o4-mini", 200_000),
    # Google.
    ("gemini-2.", 1_048_576),
    ("gemma-3-1b", 32_000),
    ("gemma-3", 128_000),
    # Amazon.
    ("nova-pro", 300_000),
    ("nova-lite", 300_000),
    ("nova-micro", 128_000),
    # Meta and Mistral.
    ("llama-3.1", 128_000),
    ("llama-3.3", 128_000),
    ("mistral-large", 128_000),
)


def window_tokens(model_id: str) -> int | None:
    """`model_id`'s input window in tokens, or None when it is not known."""
    model_id = (model_id or "").strip().lower()
    if not model_id:
        return None
    ids = [model_id, *(model_id[n + 1:] for n, c in enumerate(model_id) if c in "./")]
    found = [(len(prefix), tokens) for prefix, tokens in WINDOWS for i in ids if i.startswith(prefix.lower())]
    return max(found)[1] if found else None
