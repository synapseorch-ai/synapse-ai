#!/usr/bin/env python3
"""Say where our model tables and the pinned extract of a public list disagree (F1598b). It only reports.

For each model in `scripts/model_list.json` (see `scripts/model_list.py`), it compares:
- the window table's input window with the list's `max_input_tokens`;
- the rate card's prices per 1M tokens with the list's prices per token, times a million, exactly.

It prints each difference and writes nothing. A difference is for a person to read against the provider's own page:
the list can be wrong or late, and so can we.

    python scripts/model_list_diff.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "scripts/model_list.json"


def per_million(per_token: float) -> Decimal:
    """A price per token as a price per 1M tokens, exactly, from the figure as written (3e-06 → 3)."""
    return _plain(Decimal(repr(per_token)) * 1_000_000)


def _plain(value: Decimal) -> Decimal:
    return Decimal(format(value.normalize(), "f"))


def rows(extract: dict, pricing: dict, window_of) -> list[dict]:
    """Each difference: the model, what differs, ours and the list's."""
    out: list[dict] = []
    for model, entry in extract.get("models", {}).items():
        ours, listed = window_of(model), entry.get("max_input_tokens")
        if ours and listed and ours != listed:
            out.append({"model": model, "what": "input window", "ours": ours, "list": listed})
        price = pricing.get(model) or {}
        for side, field, mine in (("input", "input_cost_per_token", "input_per_1m"),
                                  ("output", "output_cost_per_token", "output_per_1m")):
            if entry.get(field) is None or price.get(mine) is None:
                continue
            ours_price, listed_price = _plain(Decimal(repr(price[mine]))), per_million(entry[field])
            if ours_price != listed_price:
                out.append({"model": model, "what": f"{side} price per 1M tokens", "ours": str(ours_price),
                            "list": str(listed_price)})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", action="store_true", help="the differences as JSON")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "backend"))
    from core.model_pricing import DEFAULT_MODEL_PRICING
    from core.providers.windows import window_tokens

    extract = json.loads(EXTRACT.read_text(encoding="utf-8"))
    found = rows(extract, DEFAULT_MODEL_PRICING, window_tokens)
    if args.json:
        print(json.dumps(found, indent=1))
        return 0
    print(f"{len(found)} differences with {extract['source']} at {extract['commit'][:12]}")
    for row in found:
        print(f"  {row['model']}: {row['what']}: ours {row['ours']}, the list {row['list']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
