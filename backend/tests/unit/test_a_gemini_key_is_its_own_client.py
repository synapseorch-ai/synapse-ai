"""
A Gemini call goes out on its own key (F321).

The engine kept one `genai.Client` per process, built with the key of whichever
call came first. Every later call's `api_key` was ignored: in cloud's shared
worker, where each job carries its own organisation's key, the first
organisation's key served every later organisation's Gemini chat and
embeddings — billed to it, and sending their prompts under it. On a laptop, a
rotated key was not used until a restart. And whichever of chat or embeddings
built the client chose the timeout for both, so chat after an embedding ran
with none.

Now there is one client per credential, held in a small bounded cache whose
keys are never the key itself. `genai.Client` is replaced by a recording fake
here: no network, no key.
"""
import asyncio
import hashlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import core.llm_providers as llm
from core.config import LLM_REQUEST_TIMEOUT

CHAT_TIMEOUT_MS = int((LLM_REQUEST_TIMEOUT + 5) * 1000)
USER = [{"role": "user", "content": "q"}]


def _answer(text):
    part = SimpleNamespace(function_call=None, text=text, thought=None)
    cand = SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"), content=SimpleNamespace(parts=[part]))
    return SimpleNamespace(candidates=[cand], usage_metadata=None)


@pytest.fixture
def clients(monkeypatch):
    """Every `genai.Client` built in the test, in order."""
    from google import genai

    built = []

    class _Models:
        def __init__(self, owner):
            self._owner = owner

        def generate_content(self, model, contents, config):
            return _answer(f"from {self._owner.api_key}")

        def embed_content(self, model, contents, config):
            mark = float(built.index(self._owner) + 1)  # which client answered
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[mark, 0.5]) for _ in contents])

    class FakeClient:
        def __init__(self, *, api_key=None, http_options=None, **_kw):
            self.api_key = api_key
            self.http_options = http_options
            self.closed = 0
            self.models = _Models(self)
            built.append(self)

        def close(self):
            self.closed += 1

    monkeypatch.setattr(genai, "Client", FakeClient)
    monkeypatch.setattr(llm, "_gemini_clients", OrderedDict())
    return built


async def _chat(key):
    text, *_ = await llm.call_gemini("gemini-2.5-flash", USER, "sys", key)
    return text


async def test_chat_on_two_keys_goes_out_on_each(clients):
    assert await _chat("key-A") == "from key-A"
    assert await _chat("key-B") == "from key-B"
    assert [c.api_key for c in clients] == ["key-A", "key-B"]


async def test_embeddings_on_two_keys_go_out_on_each(clients):
    first = await llm._embed_gemini(["t"], "gemini-embedding-001", "key-A")
    second = await llm._embed_gemini(["t"], "gemini-embedding-001", "key-B")
    assert [c.api_key for c in clients] == ["key-A", "key-B"]
    assert first != second


async def test_an_embedding_first_does_not_choose_the_key_for_chat(clients):
    await llm._embed_gemini(["t"], "gemini-embedding-001", "key-A")
    assert await _chat("key-B") == "from key-B"


async def test_one_key_is_one_client(clients):
    await _chat("key-A")
    await _chat("key-A")
    await llm._embed_gemini(["t"], "gemini-embedding-001", "key-A")
    assert len(clients) == 1


async def test_chat_and_embeddings_both_carry_the_chat_timeout(clients):
    await llm._embed_gemini(["t"], "gemini-embedding-001", "key-A")
    await _chat("key-B")
    assert [c.http_options.timeout for c in clients] == [CHAT_TIMEOUT_MS, CHAT_TIMEOUT_MS]


async def test_the_cache_never_holds_the_key(clients):
    await _chat("key-A")
    keys = list(llm._gemini_clients)
    assert len(keys) == 1
    assert "key-A" not in keys[0]
    assert keys[0] != hashlib.sha256(b"key-A").hexdigest()


async def test_no_key_is_not_cached(clients):
    await llm._embed_gemini(["t"], "gemini-embedding-001", "")  # answers zeros, builds nothing
    llm._gemini_client_for(None, timeout_ms=CHAT_TIMEOUT_MS)
    llm._gemini_client_for(None, timeout_ms=CHAT_TIMEOUT_MS)
    assert len(clients) == 2
    assert len(llm._gemini_clients) == 0


def test_the_cache_is_bounded_and_drops_the_least_recently_used(clients):
    most = llm._GEMINI_CLIENTS_MAX
    for i in range(most):
        llm._gemini_client_for(f"k{i}", timeout_ms=CHAT_TIMEOUT_MS)
    llm._gemini_client_for("k0", timeout_ms=CHAT_TIMEOUT_MS)  # used again: now the newest
    for i in range(5):
        llm._gemini_client_for(f"new{i}", timeout_ms=CHAT_TIMEOUT_MS)

    assert len(llm._gemini_clients) == most
    built = len(clients)
    llm._gemini_client_for("k0", timeout_ms=CHAT_TIMEOUT_MS)
    assert len(clients) == built, "the key used again was dropped"
    llm._gemini_client_for("k1", timeout_ms=CHAT_TIMEOUT_MS)
    assert len(clients) == built + 1, "the least recently used key was kept"
    # A dropped client may still be answering a call on another thread.
    assert all(c.closed == 0 for c in clients)


def test_threads_asking_at_once_build_one_client_per_key(clients):
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: llm._gemini_client_for(f"k{i % 4}", timeout_ms=CHAT_TIMEOUT_MS), range(100)))
    assert sorted(c.api_key for c in clients) == ["k0", "k1", "k2", "k3"]


def test_code_indexing_s_fresh_event_loops_share_the_client(clients):
    """The code indexer runs each embedding call on a new event loop."""
    for _ in range(2):
        loop = asyncio.new_event_loop()
        try:
            vectors = loop.run_until_complete(llm._embed_gemini(["t"], "gemini-embedding-001", "key-A"))
        finally:
            loop.close()
        assert vectors == [[1.0, 0.5]]
    assert len(clients) == 1
