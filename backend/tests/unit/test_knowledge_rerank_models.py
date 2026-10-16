"""
Which rerank models a workspace's keys can use (BF9, D293).

Knowledge's rerank picker lists what each pasted key answers for, as PROV1's model
list does for chat: Cohere's own list (`endpoint=rerank`), the list we keep for
Voyage AI and Jina AI (they publish none) once a rerank on the key answers, and
what a TEI or Infinity server says it serves. A refused key lists nothing and says
so; one provider's failure never hides another's.
"""
from __future__ import annotations

import httpx
import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from core.knowledge import rerank_models
from core.knowledge.rerank_models import list_rerank_models

COHERE_LIST = {"models": [
    {"name": "rerank-v3.5", "endpoints": ["rerank"], "is_deprecated": False},
    {"name": "rerank-multilingual-v3.0", "endpoints": ["rerank"], "is_deprecated": False},
    {"name": "rerank-english-v2.0", "endpoints": ["rerank"], "is_deprecated": True},
]}


@pytest.fixture(autouse=True)
def _fresh():
    rerank_models.forget()
    yield
    rerank_models.forget()


def _serve(monkeypatch, answers: dict) -> list:
    """Answer by URL prefix; record (method, url, params, json, auth)."""
    seen = []

    def handler(url, kwargs):
        seen.append({"method": "POST" if "json" in kwargs else "GET", "url": url, "params": kwargs.get("params"),
                     "json": kwargs.get("json"), "auth": (kwargs.get("headers") or {}).get("Authorization")})
        for prefix, answer in answers.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"unexpected call to {url}")

    install_httpx(monkeypatch, handler)
    return seen


def _by_provider(listings) -> dict:
    return {listing.provider: listing for listing in listings}


async def test_no_key_lists_nothing_and_calls_nothing(monkeypatch):
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    assert await list_rerank_models({}) == []


async def test_cohere_s_own_list_is_read_for_rerank_models_without_the_deprecated(monkeypatch):
    seen = _serve(monkeypatch, {"https://api.cohere.com/v1/models": FakeResponse(COHERE_LIST)})
    [cohere] = await list_rerank_models({"cohere_key": "k-co"})
    assert seen[0]["method"] == "GET" and seen[0]["auth"] == "Bearer k-co"
    assert seen[0]["params"]["endpoint"] == "rerank"
    assert (cohere.provider, cohere.label, cohere.key, cohere.error) == ("cohere", "Cohere", "cohere_key", None)
    assert [m.id for m in cohere.models] == ["cohere.rerank-v3.5", "cohere.rerank-multilingual-v3.0"]


async def test_cohere_s_list_is_followed_across_its_pages(monkeypatch):
    pages = iter([FakeResponse({"models": COHERE_LIST["models"][:1], "next_page_token": "p2"}),
                  FakeResponse({"models": COHERE_LIST["models"][1:2]})])
    params = []
    install_httpx(monkeypatch, lambda url, kwargs: (params.append(kwargs.get("params")), next(pages))[1])
    [cohere] = await list_rerank_models({"cohere_key": "k-co"})
    assert [m.id for m in cohere.models] == ["cohere.rerank-v3.5", "cohere.rerank-multilingual-v3.0"]
    assert "page_token" not in params[0] and params[1]["page_token"] == "p2"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_key_lists_nothing_and_says_so(monkeypatch, status):
    _serve(monkeypatch, {"https://api.cohere.com/": FakeResponse({"message": "invalid api token"},
                                                                 status_code=status)})
    [cohere] = await list_rerank_models({"cohere_key": "bad"})
    assert cohere.models == () and cohere.error == f"Cohere refused this key ({status})"


async def test_a_list_that_cannot_be_read_says_so_and_lists_nothing(monkeypatch):
    _serve(monkeypatch, {"https://api.cohere.com/": httpx.ConnectError("down")})
    [cohere] = await list_rerank_models({"cohere_key": "k-co"})
    assert cohere.models == () and "could not be read" in cohere.error


@pytest.mark.parametrize("provider,key,url,first", [
    ("voyage", "voyage_key", "https://api.voyageai.com/v1/rerank", "rerank-3"),
    ("jina", "jina_key", "https://api.jina.ai/v1/rerank", "jina-reranker-v3.5"),
])
async def test_voyage_and_jina_list_the_kept_list_once_one_rerank_on_the_key_answers(
        monkeypatch, provider, key, url, first):
    seen = _serve(monkeypatch, {url: FakeResponse({"results": [{"index": 0, "relevance_score": 1.0}]})})
    [listing] = await list_rerank_models({key: "k"})
    assert len(seen) == 1, "one call proves the key; the list is ours"
    assert seen[0]["method"] == "POST" and seen[0]["auth"] == "Bearer k"
    assert seen[0]["json"]["model"] == first and len(seen[0]["json"]["documents"]) == 1
    assert listing.error is None and listing.note is None
    assert [m.id for m in listing.models] == [f"{provider}.{name}" for name in rerank_models.CATALOG[provider]]


async def test_a_refused_voyage_key_lists_nothing(monkeypatch):
    _serve(monkeypatch, {"https://api.voyageai.com/": FakeResponse({"detail": "Provided API key is invalid."},
                                                                   status_code=401)})
    [voyage] = await list_rerank_models({"voyage_key": "bad"})
    assert voyage.models == () and voyage.error == "Voyage AI refused this key (401)"


@pytest.mark.parametrize("answer", [FakeResponse({"detail": "slow down"}, status_code=429),
                                    FakeResponse({}, status_code=503), httpx.ReadTimeout("slow")])
async def test_a_key_check_that_cannot_finish_keeps_the_list_and_says_so(monkeypatch, answer):
    _serve(monkeypatch, {"https://api.jina.ai/": answer})
    [jina] = await list_rerank_models({"jina_key": "k"})
    assert jina.error is None and jina.models and "could not be checked" in jina.note


async def test_the_host_s_kept_list_replaces_the_shipped_one(monkeypatch):
    seen = _serve(monkeypatch, {"https://api.voyageai.com/": FakeResponse({"data": []})})
    listings = _by_provider(await list_rerank_models({"voyage_key": "k", "jina_key": "k"},
                                                     catalog={"voyage": ["rerank-4"], "jina": []}))
    assert [m.id for m in listings["voyage"].models] == ["voyage.rerank-4"]
    assert seen[0]["json"]["model"] == "rerank-4"
    assert listings["jina"].models == () and "no rerank models" in listings["jina"].error


async def test_a_tei_server_says_what_it_serves(monkeypatch):
    seen = _serve(monkeypatch, {"http://tei.test:8080/info": FakeResponse({"model_id": "BAAI/bge-reranker-v2-m3"})})
    [tei] = await list_rerank_models({"knowledge_rerank_base_url": "http://tei.test:8080/",
                                      "knowledge_rerank_key": "k-tei"})
    assert seen[0]["auth"] == "Bearer k-tei"
    assert (tei.provider, tei.key) == ("tei", "knowledge_rerank_base_url")
    assert [m.id for m in tei.models] == ["tei.BAAI/bge-reranker-v2-m3"]


async def test_an_infinity_server_lists_its_rerank_models(monkeypatch):
    _serve(monkeypatch, {
        "http://inf.test/info": FakeResponse({}, status_code=404),
        "http://inf.test/models": FakeResponse({"data": [
            {"id": "mixedbread-ai/mxbai-rerank-base-v1", "capabilities": ["rerank"]},
            {"id": "BAAI/bge-small-en-v1.5", "capabilities": ["embed"]}]})})
    [tei] = await list_rerank_models({"knowledge_rerank_base_url": "http://inf.test"})
    assert [m.id for m in tei.models] == ["tei.mixedbread-ai/mxbai-rerank-base-v1"]


async def test_a_server_that_says_nothing_is_named_on_its_card(monkeypatch):
    _serve(monkeypatch, {"http://tei.test/": httpx.ConnectError("refused")})
    [tei] = await list_rerank_models({"knowledge_rerank_base_url": "http://tei.test"})
    assert tei.models == () and "http://tei.test" in tei.error


async def test_one_provider_s_failure_never_hides_another_s(monkeypatch):
    _serve(monkeypatch, {"https://api.cohere.com/": RuntimeError("boom"),
                         "https://api.voyageai.com/": FakeResponse({"data": []})})
    listings = _by_provider(await list_rerank_models({"cohere_key": "k", "voyage_key": "k"}))
    assert listings["cohere"].models == () and listings["cohere"].error
    assert listings["voyage"].models and listings["voyage"].error is None


async def test_an_answer_is_kept_per_key_until_refreshed(monkeypatch):
    seen = _serve(monkeypatch, {"https://api.cohere.com/v1/models": FakeResponse(COHERE_LIST)})
    await list_rerank_models({"cohere_key": "k1"})
    await list_rerank_models({"cohere_key": "k1"})
    assert len(seen) == 1
    await list_rerank_models({"cohere_key": "k2"})
    assert len(seen) == 2, "another key is asked afresh"
    await list_rerank_models({"cohere_key": "k1"}, refresh=True)
    assert len(seen) == 3


def test_every_listed_provider_is_a_rerank_route():
    from core.knowledge.rerankers import route_for

    for provider, names in rerank_models.CATALOG.items():
        for name in names:
            route = route_for(f"{provider}.{name}")
            assert route is not None and route.provider == provider


async def test_a_changed_kept_list_is_read_afresh(monkeypatch):
    _serve(monkeypatch, {"https://api.voyageai.com/": FakeResponse({"data": []})})
    [before] = await list_rerank_models({"voyage_key": "k"}, catalog={"voyage": ["rerank-3"]})
    [after] = await list_rerank_models({"voyage_key": "k"}, catalog={"voyage": ["rerank-3", "rerank-4"]})
    assert [m.id for m in before.models] == ["voyage.rerank-3"]
    assert [m.id for m in after.models] == ["voyage.rerank-3", "voyage.rerank-4"], "staff's edit shows at once"
