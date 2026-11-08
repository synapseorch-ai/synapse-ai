"""Each model's input window, in tokens: the one source the model pick (F1251) and a run's context budget (D339) read.

A host states its own models' windows over this (cloud: its platform rows, through `models_menu.set_model_facts`'s
`window`). None is "not known": the pick never rules a model out for it, and D339 budgets a default.
"""
from __future__ import annotations

#: Known input windows: `(id prefix, tokens)`, matched against a model's id and each id its provider's prefix leaves
#: (`us.anthropic.claude-haiku-4-5` is also `claude-haiku-4-5`); the longest prefix that matches wins. The rows are
#: D339's (TRI1), each from its provider's docs.
WINDOWS: tuple[tuple[str, int], ...] = (
    # Anthropic (the Claude API's models reference, 2026-10): the 5 family and Opus 4.6–4.8 / Sonnet 4.6 hold 1M by
    # default; Haiku 4.5, Sonnet 4.5 and Opus 4.5 hold 200K (Sonnet 4.5's 1M needs a beta header the engine never
    # sends). Any other Claude id is taken at the smaller window: a step that compacts early loses nothing.
    ("claude-", 200_000),
    ("claude-fable-5", 1_000_000),
    ("claude-mythos-5", 1_000_000),
    ("claude-opus-5", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
    ("claude-opus-4-7", 1_000_000),
    ("claude-opus-4-6", 1_000_000),
    ("claude-sonnet-5", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-haiku-5", 1_000_000),
    ("claude-haiku-4-5", 200_000),
    ("claude-sonnet-4-5", 200_000),
    ("claude-opus-4-5", 200_000),
    # OpenAI (developers.openai.com model pages, 10-2026): gpt-5's input is 272K of its 400K; gpt-oss shares its
    # 131,072 between input and output, which the budget's output reserve covers.
    ("gpt-5", 272_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("o3", 200_000),
    ("o4-mini", 200_000),
    ("gpt-oss-", 131_072),
    # Google (ai.google.dev model pages and model cards): Gemini 2.x and 3.x hold 1,048,576; Gemma 3's card says 32K
    # (1B) and 128K, and Gemma 4's config holds 262,144.
    ("gemini-2.", 1_048_576),
    ("gemini-3", 1_048_576),
    ("gemma-3-1b", 32_000),
    ("gemma-3", 128_000),
    ("gemma-4", 262_144),
    # Amazon (the Nova user guide and Bedrock model cards).
    ("nova-pro", 300_000),
    ("nova-lite", 300_000),
    ("nova-micro", 128_000),
    ("nova-premier", 1_000_000),
    ("nova-2-lite", 1_000_000),
    # Meta (its model cards; Bedrock's ids spell 3.1 and 3.3 `llama3-1-` / `llama3-3-`). Llama 3 itself is 8K.
    ("llama-3.1", 128_000),
    ("llama-3.3", 128_000),
    ("llama3-1-", 128_000),
    ("llama3-3-", 128_000),
    ("llama3-70b", 8_000),
    ("llama3-8b", 8_000),
    # Mistral (docs.mistral.ai): the -latest aliases point at 256K models; Large 4 is 1M; any other id is taken at
    # the smaller window an older one had.
    ("mistral-large", 128_000),
    ("mistral-large-latest", 256_000),
    ("mistral-medium-latest", 256_000),
    ("mistral-small-latest", 256_000),
    ("mistral-large-4", 1_000_000),
    # DeepSeek (api-docs.deepseek.com): deepseek-chat and -reasoner were retired 07-2026; V4 holds 1M.
    ("deepseek-v4", 1_000_000),
    ("deepseek-flash", 1_000_000),
    # Qwen (Alibaba Model Studio): 1M in all, of which input is 983,616 with thinking on (991,808 without).
    ("qwen3.7", 983_616),
    ("qwen3.8", 983_616),
)


def window_tokens(model_id: str) -> int | None:
    """`model_id`'s input window in tokens, or None when it is not known."""
    model_id = (model_id or "").strip().lower()
    if not model_id:
        return None
    ids = [model_id, *(model_id[n + 1:] for n, c in enumerate(model_id) if c in "./")]
    found = [(len(prefix), tokens) for prefix, tokens in WINDOWS for i in ids if i.startswith(prefix.lower())]
    return max(found)[1] if found else None
