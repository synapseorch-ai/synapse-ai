"""The rate card reads each provider's own price, from the provider's own page (F1648).

A public model list disagreed with three rows. Each provider's own page, read on 2026-11-17, said the row was out of
date:
- Google's Gemini API pricing lists Gemini 3.1 Flash-Lite at $0.25 in and $1.50 out per 1M tokens (text), where the
  row said $0.125 and $0.75;
- Together AI's pricing lists Kimi K3 at $3.00 in, $0.30 cached and $15.00 out, where the row said $2.70 and $13.50;
- Mistral's Large 4 page lists $0.7 in, $0.07 cached and $2.1 out, where the row said $1.36, $0.14 and $4.18.

A run's cost estimate read the lower figure, and so did a guard that stops spending before a cap. Each corrected row
now names its page beside it.

Near misses: rows the pages agree with are as they were (Gemini 3 Flash; 3.1 Pro's first tier; GLM-4.7-Flash, free,
where the list said otherwise); a row whose provider's page no longer lists the model (deepseek-chat) is not touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.model_pricing import DEFAULT_MODEL_PRICING as RATES

SOURCE = (Path(__file__).resolve().parents[2] / "core/model_pricing.py").read_text(encoding="utf-8")

#: (model, input, output, cached input or None, the page the figures were read from).
CORRECTED = [
    ("gemini-3.1-flash-lite-preview", 0.25, 1.5, None, "aistudio.google.com/docs/pricing"),
    ("together.moonshotai/Kimi-K3", 3.0, 15.0, 0.3, "together.ai/pricing"),
    ("mistral.mistral-large-4", 0.7, 2.1, 0.07, "docs.mistral.ai/models/mistral-large-4-0"),
]
#: Rows the providers' own pages agree with, and one no page lists any more: as they were.
AS_THEY_WERE = [("gemini-3-flash-preview", 0.5, 3), ("gemini-3.1-pro-preview", 2, 12), ("zai.glm-4.7-flash", 0, 0),
                ("deepseek-chat", 0.27, 1.1), ("deepseek-reasoner", 0.55, 2.19)]


@pytest.mark.parametrize("model, inp, out, cached, page", CORRECTED, ids=[c[0] for c in CORRECTED])
def test_a_row_reads_its_providers_own_price(model, inp, out, cached, page):
    row = RATES[model]
    assert (row["input_per_1m"], row["output_per_1m"]) == (inp, out), row
    assert row.get("cache_read_per_1m") == cached, row


@pytest.mark.parametrize("model, inp, out, cached, page", CORRECTED, ids=[c[0] for c in CORRECTED])
def test_a_corrected_row_names_the_page_it_was_read_from(model, inp, out, cached, page):
    near = SOURCE[max(0, SOURCE.index(f'"{model}"') - 400):SOURCE.index(f'"{model}"')]
    assert page in near and "2026-11-17" in near, near[-300:]


@pytest.mark.parametrize("model, inp, out", AS_THEY_WERE, ids=[a[0] for a in AS_THEY_WERE])
def test_a_row_the_page_agrees_with_or_no_longer_lists_is_as_it_was(model, inp, out):
    row = RATES[model]
    assert (row["input_per_1m"], row["output_per_1m"]) == (inp, out), row
