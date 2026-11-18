#!/usr/bin/env python3
"""Refresh the pinned extract of a public model list (F1598b), by hand at release time.

Synapse's model tables (the window table, the rate card, the size table) are written by hand, each row from its
provider's page. This takes one public list, LiteLLM's `model_prices_and_context_window.json` (MIT), at one commit,
and keeps only what is needed to check them: the ids the rate card knows, each with the source's own fields as the
source writes them, under a header that names the source, the commit and the source's licence in full. An id the
source does not list is marked as ours. `scripts/model_list_diff.py` then says where our tables and the extract
disagree; it changes nothing.

It never runs in CI, and the product never fetches or reads the list: a person runs it, reads the diff, and fixes a
row by hand from its provider's page.

    python scripts/model_list.py --commit <the commit's full 40-character id> [--out scripts/model_list.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://github.com/BerriAI/litellm"
FILE = "model_prices_and_context_window.json"
RAW = "https://raw.githubusercontent.com/BerriAI/litellm/{commit}/{path}"
#: The source's fields kept, as it names them; every other is left out, never reworded.
FIELDS = ("max_input_tokens", "max_output_tokens", "input_cost_per_token", "output_cost_per_token",
          "supports_function_calling")
#: Our provider prefix → the source's, for an id our tables write as `<provider>.<model>`.
PREFIXES = {"groq": "groq", "together": "together_ai", "fireworks": "fireworks_ai", "mistral": "mistral",
            "moonshot": "moonshot", "qwen": "dashscope", "zai": "zai", "minimax": "minimax", "cerebras": "cerebras"}
#: Model families the source also lists under their provider's name.
FAMILIES = {"gemini-": "gemini", "grok-": "xai", "deepseek-": "deepseek"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def candidates(model: str) -> list[str]:
    """The source's keys `model` may be listed under: itself, then its provider's spelling."""
    keys = [model]
    provider, dot, rest = model.partition(".")
    if dot and provider in PREFIXES:
        keys.append(f"{PREFIXES[provider]}/{rest}")
    keys += [f"{name}/{model}" for start, name in FAMILIES.items() if model.startswith(start)]
    return keys


def extract(source: dict, models: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Each of `models` the source lists, with the source's own kept fields as it writes them (and the key it is
    listed under); and the ones it does not list, which are ours."""
    kept: dict[str, dict] = {}
    ours: list[str] = []
    for model in models:
        key = next((k for k in candidates(model) if isinstance(source.get(k), dict)), None)
        if key is None:
            ours.append(model)
            continue
        kept[model] = {"key": key, **{field: source[key][field] for field in FIELDS if field in source[key]}}
    return kept, ours


def licence_section(text: str) -> str:
    """The source's MIT licence, whole, from its title to its end (its file names other terms for another folder)."""
    start = text.find("MIT License")
    if start < 0:
        raise ValueError("the source's licence has no MIT section")
    return text[start:]


def our_models() -> list[str]:
    """The ids the rate card knows."""
    sys.path.insert(0, str(ROOT / "backend"))
    from core.model_pricing import DEFAULT_MODEL_PRICING

    return list(DEFAULT_MODEL_PRICING)


def fetch(commit: str, path: str) -> str:
    with urllib.request.urlopen(RAW.format(commit=commit, path=path), timeout=60) as answer:
        return answer.read().decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--commit", required=True, help="the source commit's full 40-character id")
    parser.add_argument("--out", default=str(ROOT / "scripts/model_list.json"))
    args = parser.parse_args(argv)
    if not _COMMIT.match(args.commit):
        print("--commit takes a commit's full 40-character id, never a branch or a tag: the extract is pinned.",
              file=sys.stderr)
        return 2
    source = json.loads(fetch(args.commit, FILE))
    licence = licence_section(fetch(args.commit, "LICENSE"))
    kept, ours = extract(source, our_models())
    out = {
        "source": SOURCE, "file": FILE, "commit": args.commit,
        "about": ("An extract of the source at this commit, for scripts/model_list_diff.py: only the ids Synapse's rate "
                  "card knows, each with the source's own fields as it writes them. The source's licence follows."),
        "licence": licence,
        "models": kept,
        "ours": {model: "not in the source at this commit" for model in ours},
    }
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(kept)} listed, {len(ours)} ours, from {SOURCE} at {args.commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
