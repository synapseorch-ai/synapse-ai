"""Contextual Retrieval counts a cache only where one holds (F1589, CTX2's O12).

`enrich.caching_for` read "the deployment prices a cache read for this platform model" as "the gateway caches the
document by itself": true for Nova (KL1), whose writes cost nothing, so the gateway places Nova's point unasked. Cloud's
migration 0107 (F1587) gives the Claude rows cache rates too, but the gateway caches Claude only when the caller marks
the prompt (D191), and enrich's calls are not marked. A window's estimate and its plan would then count cache reads
on Claude that never happen: a quote below what the run is charged.

A platform model whose cache write costs more than its input answers "no cache" until enrich marks its document;
Nova's stays "explicit", and a model with no cache price stays none.
"""
from __future__ import annotations

import pytest

from core.knowledge import costs, enrich
from core.knowledge.costs import Rate

MARKUP = 1.10
NOVA = "synapse.nova-lite"
CLAUDE = "synapse.claude-sonnet-4-6"
LLAMA = "synapse.llama-3-1-8b"
RATES = {
    # migration 0067: read a quarter of input, write $0
    NOVA: Rate(input_per_mtok=0.06 * MARKUP, output_per_mtok=0.24 * MARKUP, cache_read_per_mtok=0.015 * MARKUP,
               cache_write_per_mtok=0.0),
    # migration 0107 (F1587): read 0.1x, write 1.25x input, minimum 1,024
    CLAUDE: Rate(input_per_mtok=3.30 * MARKUP, output_per_mtok=16.50 * MARKUP, cache_read_per_mtok=0.33 * MARKUP,
                 cache_write_per_mtok=4.125 * MARKUP, cache_min_tokens=1024),
    LLAMA: Rate(input_per_mtok=0.22 * MARKUP, output_per_mtok=0.22 * MARKUP),
}


@pytest.fixture
def pricer():
    async def price(model: str) -> Rate | None:
        return RATES.get(model)

    costs.set_platform_pricer(price)
    yield
    costs.set_platform_pricer(None)


async def test_a_platform_model_whose_write_costs_more_than_input_is_not_counted_as_cached(pricer):
    assert await enrich.caching_for(CLAUDE) == (None, 0)
    doc, passages = 20_000, [300, 280, 310, 290]
    uncached, _out, read, write = enrich.window_tokens(CLAUDE, doc, passages, caching=await enrich.caching_for(CLAUDE))
    assert (read, write) == (0, 0), "no read the gateway will not make"
    assert uncached > doc * len(passages), "every call pays the whole document"


async def test_nova_is_still_counted_as_cached_by_itself(pricer):
    assert await enrich.caching_for(NOVA) == ("explicit", 0)
    _in, _out, read, write = enrich.window_tokens(NOVA, 20_000, [300, 280], caching=await enrich.caching_for(NOVA))
    assert read > 0 and write > 0


async def test_a_platform_model_with_no_cache_price_is_none(pricer):
    assert await enrich.caching_for(LLAMA) == (None, 0)
