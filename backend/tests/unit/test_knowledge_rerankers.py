"""
Bring-your-own rerankers and our gateway's (B2a, D163).

A rerank model id names who serves it (Naveen, D163 #1): `synapse.` our
gateway, `cohere.`, `voyage.`, `jina.`, or `tei.` a TEI/Infinity server of the
customer's. Each is asked in its own shape, read in its own shape, and answers
a `RerankResult` best first with ties by index; each says what it cost.
"""
from __future__ import annotations

import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from core.knowledge import rerankers, seams
from core.knowledge.errors import RerankError, WalletExhausted
from core.knowledge.rerankers import ProviderReranker, from_settings
from core.knowledge.types import Attribution, RerankSpec

ATTRIBUTION = Attribution(kb_id="kb_1", query_id="kq_1")
SETTINGS = {"model_gateway_base_url": "http://gw.test", "model_gateway_key": "k-gw", "cohere_key": "k-co",
            "voyage_key": "k-vo", "jina_key": "k-ji", "knowledge_rerank_base_url": "http://tei.test:8080/"}
DOCS = ["alpha", "beta", "gamma"]


async def _rerank(model, settings=SETTINGS, top_n=3):
    return await ProviderReranker().rerank("q", DOCS, spec=RerankSpec(model=model, top_n=top_n), settings=settings,
                                           attribution=ATTRIBUTION)


def _capture(monkeypatch, answer: FakeResponse) -> list:
    seen = []

    def handler(url, kwargs):
        seen.append({"url": url, "body": kwargs["json"], "auth": kwargs["headers"].get("Authorization")})
        return answer

    install_httpx(monkeypatch, handler)
    return seen


def test_none_by_default_and_a_router_once_a_model_is_named():
    assert from_settings({}) is None and from_settings({"knowledge_rerank_model": ""}) is None
    assert isinstance(from_settings({"knowledge_rerank_model": "cohere.rerank-v3.5"}), ProviderReranker)
    assert seams.get_reranker({}) is None, "OSS reranks only when someone set a model"


async def test_our_gateway_is_asked_in_c1_s_shape_and_reports_its_charge(monkeypatch):
    body = {"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4},
                        {"index": 1, "relevance_score": 0.4}],
            "model": "cohere-rerank-3-5", "usage": {"search_units": 1, "charged_microusd": 2000,
                                                   "cost_source": "platform"}}
    seen = _capture(monkeypatch, FakeResponse(body))
    result = await _rerank("synapse.cohere-rerank-3-5")
    assert seen == [{"url": "http://gw.test/v1/rerank", "auth": "Bearer k-gw",
                     "body": {"model": "cohere-rerank-3-5", "query": "q", "documents": DOCS, "top_n": 3}}]
    assert result.ranking == [(2, 0.9), (0, 0.4), (1, 0.4)], "best first, ties by index"
    u = result.usage
    assert (u.stage, u.cost_source, u.unit, u.units, u.charged_microusd, u.est_usd) == (
        "rerank", "platform", "search_units", 1.0, 2000, 0.002)


async def test_our_gateway_s_402_is_an_empty_wallet(monkeypatch):
    body = {"error": {"message": "Your model wallet is empty.", "code": "model_wallet_empty",
                      "needed_microusd": 2000, "available_microusd": 0}}
    _capture(monkeypatch, FakeResponse(body, status_code=402))
    with pytest.raises(WalletExhausted) as caught:
        await _rerank("synapse.cohere-rerank-3-5")
    assert caught.value.needed_microusd == 2000


async def test_cohere_is_asked_at_v2_and_priced_per_search(monkeypatch):
    body = {"results": [{"index": 1, "relevance_score": 0.8}], "meta": {"billed_units": {"search_units": 1}}}
    seen = _capture(monkeypatch, FakeResponse(body))
    result = await _rerank("cohere.rerank-v3.5", top_n=1)
    assert seen[0]["url"] == "https://api.cohere.com/v2/rerank" and seen[0]["auth"] == "Bearer k-co"
    assert seen[0]["body"] == {"model": "rerank-v3.5", "query": "q", "documents": DOCS, "top_n": 1}
    assert result.ranking == [(1, 0.8)] and result.usage.cost_source == "byok"
    assert result.usage.est_usd == pytest.approx(0.002) and result.usage.charged_microusd is None


async def test_voyage_takes_top_k_answers_data_and_is_priced_per_token(monkeypatch):
    body = {"data": [{"index": 0, "relevance_score": 0.7}, {"index": 2, "relevance_score": 0.6}],
            "usage": {"total_tokens": 1000}}
    seen = _capture(monkeypatch, FakeResponse(body))
    result = await _rerank("voyage.rerank-2.5", top_n=2)
    assert seen[0]["url"] == "https://api.voyageai.com/v1/rerank"
    assert seen[0]["body"] == {"model": "rerank-2.5", "query": "q", "documents": DOCS, "top_k": 2}
    assert result.ranking == [(0, 0.7), (2, 0.6)]
    assert (result.usage.unit, result.usage.input_tokens) == ("tokens", 1000)
    assert result.usage.est_usd == pytest.approx(1000 * 0.05 / 1_000_000)


async def test_jina_is_unpriced(monkeypatch):
    _capture(monkeypatch, FakeResponse({"results": [{"index": 0, "relevance_score": 0.5}],
                                        "usage": {"total_tokens": 12}}))
    result = await _rerank("jina.jina-reranker-v2-base-multilingual", top_n=1)
    assert not result.usage.priced and result.usage.est_usd == 0.0


@pytest.mark.parametrize("answer", [
    [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}],  # TEI's bare list
    {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]},  # Infinity
])
async def test_a_tei_or_infinity_server_is_local_and_either_shape_is_read(monkeypatch, answer):
    seen = _capture(monkeypatch, FakeResponse(answer))
    result = await _rerank("tei.bge-reranker-v2-m3", top_n=2)
    assert seen[0]["url"] == "http://tei.test:8080/rerank" and seen[0]["auth"] is None
    assert seen[0]["body"]["texts"] == DOCS and seen[0]["body"]["documents"] == DOCS
    assert result.ranking == [(1, 0.9), (0, 0.1)]
    assert (result.usage.cost_source, result.usage.est_usd, result.usage.priced) == ("local", 0.0, True)


@pytest.mark.parametrize("model,missing", [("cohere.rerank-v3.5", "cohere_key"),
                                           ("tei.x", "knowledge_rerank_base_url"),
                                           ("synapse.cohere-rerank-3-5", "model_gateway_base_url")])
async def test_a_missing_key_or_address_is_named_and_nothing_is_called(monkeypatch, model, missing):
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    with pytest.raises(RerankError, match=missing) as caught:
        await _rerank(model, settings={})
    assert not caught.value.retryable


async def test_an_unknown_vendor_is_refused():
    with pytest.raises(RerankError, match="No reranker serves"):
        await _rerank("acme.rerank-1")


@pytest.mark.parametrize("status,retryable", [(429, True), (503, True), (400, False)])
async def test_a_refusal_raises_retryable_by_its_status(monkeypatch, status, retryable):
    _capture(monkeypatch, FakeResponse({"message": "nope"}, status_code=status))
    with pytest.raises(RerankError, match="nope") as caught:
        await _rerank("cohere.rerank-v3.5")
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("answer", [{"results": [{"index": 9, "relevance_score": 0.5}]}, {"what": []},
                                    {"results": [{"index": 0}]}])
async def test_an_answer_that_names_no_document_it_was_given_is_refused(monkeypatch, answer):
    _capture(monkeypatch, FakeResponse(answer))
    with pytest.raises(RerankError):
        await _rerank("cohere.rerank-v3.5")


async def test_at_most_a_hundred_candidates_are_sent_each_cut_to_its_cap(monkeypatch):
    seen = _capture(monkeypatch, FakeResponse({"results": [{"index": 0, "relevance_score": 1.0}]}))
    docs = ["x" * 10_000] * 150
    await ProviderReranker().rerank("q", docs, spec=RerankSpec(model="cohere.rerank-v3.5", top_n=500),
                                    settings=SETTINGS, attribution=ATTRIBUTION)
    body = seen[0]["body"]
    assert len(body["documents"]) == 100 and len(body["documents"][0]) == rerankers.MAX_DOCUMENT_CHARS
    assert body["top_n"] == 100
