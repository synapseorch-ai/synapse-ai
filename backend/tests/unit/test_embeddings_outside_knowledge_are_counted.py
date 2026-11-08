"""F394: embeddings outside Knowledge write a usage row.

Knowledge records its embeddings in its own ledger. The semantic cache (one
`embed_batch_ex` call per lookup it has not embedded) and code search (a query,
and every indexing pass) wrote nothing to `usage_logs`, so Usage under-counted
a paid embedding model. Each now writes a row — the provider's token count when
it gives one, else one counted from the text, marked as estimated; $0 for the
platform gateway (its wallet settles it, F330) and a local model.
"""
import asyncio

import pytest

from core import llm_providers as llm
from core import usage_tracker


async def _rows(source):
    return [r for r in await usage_tracker.get_usage_logs(limit=50) if r.get("source") == source]


class TestTheSemanticCache:
    @pytest.fixture
    def answer(self, monkeypatch):
        box = {"tokens": 7, "provider": "openai", "calls": 0}

        async def fake(texts, model, settings, *, input_type=None, dims=None):
            box["calls"] += 1
            return llm.EmbeddingResponse([[0.1, 0.2, 0.3]], model, box["provider"], input_tokens=box["tokens"])

        monkeypatch.setattr(llm, "embed_batch_ex", fake)
        from core.cache import response_cache
        response_cache.forget_local_state()
        yield box
        response_cache.forget_local_state()

    async def test_a_call_writes_a_row_with_its_tokens(self, answer):
        from core.cache.response_cache import _embed

        await _embed("what is the capital of france", "text-embedding-3-small", {})
        [row] = await _rows("cache")
        assert row["tool_name"] == "cache:embed" and row["model"] == "text-embedding-3-small"
        assert row["session_id"] == "embeddings:cache", "never 'unknown', which Usage calls the system prompt's"
        assert row["input_tokens"] == 7 and row["provider"] == "openai"
        assert row["estimated_cost"] == pytest.approx(7 * 0.02 / 1_000_000), "the model's own rate, as Knowledge prices it"

    async def test_a_vector_it_already_had_writes_none(self, answer):
        from core.cache.response_cache import _embed

        await _embed("hello", "text-embedding-3-small", {})
        await _embed("hello", "text-embedding-3-small", {})
        assert answer["calls"] == 1 and len(await _rows("cache")) == 1

    async def test_a_provider_that_does_not_count_is_estimated(self, answer):
        from core.cache.response_cache import _embed

        answer["tokens"] = 0
        await _embed("x" * 40, "gemini-embedding-001", {})
        [row] = await _rows("cache")
        assert row["input_tokens"] == 10

    async def test_the_platform_gateways_costs_nothing_here(self, answer):
        from core.cache.response_cache import _embed

        answer["provider"] = "model_gateway"
        await _embed("hello", "synapse.titan-embed-v2", {})
        [row] = await _rows("cache")
        assert row["estimated_cost"] == 0.0


class TestCodeSearch:
    async def test_a_query_writes_a_row(self, monkeypatch):
        import core.llm_providers as providers
        from tools import code_search

        monkeypatch.setattr(code_search, "_resolve_query_embedding_config",
                            lambda: ("text-embedding-3-small", 4, {}))

        async def fake(texts, model, settings):
            return [[0.1, 0.2, 0.3, 0.4]]

        monkeypatch.setattr(providers, "embed_batch", fake)
        await code_search._get_query_embedding("where is the login handler defined?")
        [row] = await _rows("code_search")
        assert row["tool_name"] == "code_search:embed" and row["input_tokens"] == 9

    async def test_an_indexing_pass_writes_one_row_per_model(self, monkeypatch):
        from services import code_indexer

        monkeypatch.setattr(code_indexer, "_owner_loop", asyncio.get_running_loop())
        code_indexer._count_embedded("text-embedding-3-small", ["a" * 8, "b" * 8])
        code_indexer._count_embedded("text-embedding-3-small", ["c" * 4])
        await asyncio.to_thread(code_indexer._record_embedded)   # as the indexer's own thread does
        [row] = await _rows("code_index")
        assert row["tool_name"] == "code_index:embed" and row["input_tokens"] == 5
        assert code_indexer._embedded_this_pass == {}
