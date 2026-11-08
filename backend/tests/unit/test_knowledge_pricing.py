"""What a deployment charges (F334, W1, D179): a platform model (`synapse.*`, or an OCR engine whose cost source is
`platform`) is quoted at `costs.platform_rate` — cloud's catalogue × the org's plan — and never at its list price
when the deployment prices it. Without a pricer (OSS) every quote is the list price it always was.
"""
from __future__ import annotations

import logging

import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from core.knowledge import budgets, costs, embedders, evals_generate, profiles
from core.knowledge.costs import Rate
from core.knowledge.rerankers import ProviderReranker
from core.knowledge.types import Attribution, RerankSpec

EMBED = "synapse.titan-embed-v2"  # $0.02 per 1M tokens at list
RERANK = "synapse.cohere-rerank-3-5"  # $0.002 a search unit at list
CHAT = "synapse.nova-micro"
OCR = "textract-detect-text-v1"  # $0.0015 a page at list
MARKUP = 1.25
RATES = {
    EMBED: Rate(input_per_mtok=0.02 * MARKUP),
    RERANK: Rate(per_unit=0.002 * MARKUP, unit="search_units"),
    CHAT: Rate(input_per_mtok=0.035 * MARKUP, output_per_mtok=0.14 * MARKUP),
    OCR: Rate(per_unit=0.0015 * MARKUP, unit="pages"),
}
TEXTS = ["The hosting fee is billed monthly in advance.", "A refund is paid within thirty days."]


class Pricer:
    """Cloud's pricer, as the engine sees it: a model in, its rate (or None) out. Records what it was asked."""

    def __init__(self, rates=RATES) -> None:
        self.rates = rates
        self.asked: list[str] = []

    async def __call__(self, model: str) -> Rate | None:
        self.asked.append(model)
        return self.rates.get(model)


class _Ocr:
    def __init__(self, *, cost_source: str = "platform", engine: str = OCR, price: float = 0.0015) -> None:
        self.cost_source, self.engine, self.price_per_page_usd = cost_source, engine, price


@pytest.fixture
def pricer():
    found = Pricer()
    costs.set_platform_pricer(found)
    yield found
    costs.set_platform_pricer(None)


async def test_without_a_pricer_every_quote_is_the_list_price():
    assert await costs.platform_rate(EMBED) is None
    assert await budgets.embed_rate(EMBED) == (0.02, "platform")
    assert await costs.rerank_usd_per_search(RERANK) == 0.002
    assert await costs.ocr_usd_per_page(_Ocr()) == 0.0015
    assert await costs.chat_usd(CHAT, 1000, 100) is None, "no rate card prices a platform chat model"
    assert await costs.ocr_usd_per_page(None) == 0.0


async def test_a_platform_cost_is_quoted_at_the_deployments_rate(pricer):
    assert await budgets.embed_rate(EMBED) == (pytest.approx(0.025), "platform")
    assert await embedders.usd_per_mtok(EMBED, profiles.profile_for(EMBED)) == pytest.approx(0.025)
    tokens = profiles.estimate_tokens(TEXTS)
    assert await budgets.embed_estimate_usd(EMBED, TEXTS) == pytest.approx(tokens * 0.025 / 1_000_000)
    assert await costs.rerank_usd_per_search(RERANK) == pytest.approx(0.0025)
    assert await costs.ocr_usd_per_page(_Ocr()) == pytest.approx(0.001875)
    marked = (1500 * 0.035 + 200 * 0.14) * MARKUP / 1_000_000  # cache reads at the input rate; 8 places, as the card
    assert await costs.chat_usd(CHAT, 1000, 200, cache_read=400, cache_write=100) == pytest.approx(marked, abs=1e-8)
    answers = (1000 * 0.035 + 200 * 0.14) * MARKUP / 1_000_000
    assert await evals_generate.chat_usd(CHAT, 1000, 200) == pytest.approx(answers, abs=1e-8)


async def test_a_model_the_gateway_does_not_serve_never_asks_the_pricer(pricer, monkeypatch):
    from core import usage_tracker

    monkeypatch.setattr(usage_tracker, "_pricing_snapshot", {
        "claude-haiku-4-5": {"provider": "anthropic", "input_per_1m": 1.0, "output_per_1m": 5.0}})
    assert await budgets.embed_rate("text-embedding-3-small") == (0.02, "byok")
    assert await costs.rerank_usd_per_search("cohere.rerank-v3.5") == 0.002
    assert await costs.rerank_usd_per_search("voyage.rerank-2.5") is None, "priced by the token, not the search"
    assert await costs.ocr_usd_per_page(_Ocr(cost_source="byok", price=0.004)) == 0.004
    assert await costs.chat_usd("claude-haiku-4-5", 1000, 200) == pytest.approx((1000 * 1.0 + 200 * 5.0) / 1e6)
    assert pricer.asked == []


async def test_a_rerank_is_estimated_by_the_search_or_by_its_tokens():
    """F373: a search unit's price as it is; a per-token reranker's query once per document plus the documents."""
    assert await costs.rerank_estimate_usd("cohere.rerank-v3.5", query_tokens=10, document_tokens=[500] * 50) == 0.002
    assert await costs.rerank_estimate_usd("voyage.rerank-3-lite", query_tokens=10, document_tokens=[90, 100]) == (
        pytest.approx((2 * 10 + 190) * 0.02 / 1e6))
    assert await costs.rerank_estimate_usd("voyage.rerank-3", query_tokens=10, document_tokens=[]) == 0.0
    assert await costs.rerank_estimate_usd("jina.jina-reranker-v2", query_tokens=10, document_tokens=[100]) is None
    assert await costs.rerank_estimate_usd("", query_tokens=10, document_tokens=[100]) is None


async def test_a_platform_model_the_deployment_does_not_price_keeps_its_list_price(pricer):
    assert await budgets.embed_rate("synapse.titan-embed-text-v1") == (0.10, "platform")
    assert await costs.ocr_usd_per_page(_Ocr(engine="another-ocr")) == 0.0015
    assert pricer.asked == ["synapse.titan-embed-text-v1", "another-ocr"]


async def test_a_pricer_that_fails_leaves_the_list_price_and_says_so(caplog):
    async def broken(model: str) -> Rate | None:
        raise RuntimeError("catalogue unreachable")

    costs.set_platform_pricer(broken)
    try:
        with caplog.at_level(logging.WARNING):
            assert await budgets.embed_rate(EMBED) == (0.02, "platform")
            assert await costs.rerank_usd_per_search(RERANK) == 0.002
    finally:
        costs.set_platform_pricer(None)
    assert "catalogue unreachable" in caplog.text


async def test_a_platform_rerank_is_estimated_at_the_deployments_rate_beside_its_charge(pricer, monkeypatch):
    body = {"results": [{"index": 0, "relevance_score": 0.9}], "model": "cohere-rerank-3-5",
            "usage": {"search_units": 1, "charged_microusd": 2500, "cost_source": "platform"}}
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse(body))
    result = await ProviderReranker().rerank(
        "q", ["alpha"], spec=RerankSpec(model=RERANK, top_n=1),
        settings={"model_gateway_base_url": "http://gw.test", "model_gateway_key": "k"},
        attribution=Attribution(kb_id="kb_1", query_id="kq_1"))
    assert (result.usage.est_usd, result.usage.charged_microusd) == (pytest.approx(0.0025), 2500)
