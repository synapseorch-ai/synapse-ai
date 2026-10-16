"""
`GET /api/models/rerank`: the rerank models the workspace's keys can use (BF9, D293).

Knowledge's rerank pickers and the reranker cards read it, as the chat pickers
read `/api/models`. It is Knowledge's, so it answers 404 while Knowledge is off,
and it never puts a reranker in `/api/models`' chat lists.
"""
from __future__ import annotations

import pytest

from _fakes.fake_http import FakeResponse, install_httpx
from core.knowledge import rerank_models

COHERE = {"models": [{"name": "rerank-v3.5", "endpoints": ["rerank"], "is_deprecated": False}]}


@pytest.fixture
def keyed(monkeypatch):
    import core.knowledge.config as knowledge_config
    from core.routes import data

    rerank_models.forget()
    monkeypatch.setattr(knowledge_config, "knowledge_enabled", lambda: True)
    monkeypatch.setattr(data, "load_settings", lambda: {"cohere_key": "k-co", "voyage_key": "bad"})
    calls = []

    def handler(url, kwargs):
        calls.append(url)
        if url.startswith("https://api.cohere.com/v1/models"):
            return FakeResponse(COHERE)
        if url.startswith("https://api.voyageai.com/"):
            return FakeResponse({"detail": "Provided API key is invalid."}, status_code=401)
        return FakeResponse({}, status_code=404)

    install_httpx(monkeypatch, handler)
    yield calls
    rerank_models.forget()


async def test_each_keyed_provider_is_listed_with_its_models_or_why_not(client, keyed):
    resp = await client.get("/api/models/rerank")
    assert resp.status_code == 200
    by = {p["provider"]: p for p in resp.json()["providers"]}
    assert set(by) == {"cohere", "voyage"}
    assert by["cohere"] == {"provider": "cohere", "label": "Cohere", "key": "cohere_key",
                            "models": [{"id": "cohere.rerank-v3.5", "label": "rerank-v3.5"}],
                            "error": None, "note": None}
    assert by["voyage"]["models"] == [] and by["voyage"]["error"] == "Voyage AI refused this key (401)"


async def test_refresh_asks_again(client, keyed):
    await client.get("/api/models/rerank")
    cohere_calls = sum(url.startswith("https://api.cohere.com/") for url in keyed)
    await client.get("/api/models/rerank")
    assert sum(url.startswith("https://api.cohere.com/") for url in keyed) == cohere_calls, "kept"
    await client.get("/api/models/rerank?refresh=1")
    assert sum(url.startswith("https://api.cohere.com/") for url in keyed) == cohere_calls + 1


async def test_off_it_is_a_404(client, monkeypatch):
    import core.knowledge.config as knowledge_config

    monkeypatch.setattr(knowledge_config, "knowledge_enabled", lambda: False)
    install_httpx(monkeypatch, lambda url, kwargs: pytest.fail("called"))
    assert (await client.get("/api/models/rerank")).status_code == 404
