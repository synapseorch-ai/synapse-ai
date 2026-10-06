"""F327: a step's semantic response cache embeds with the workspace's Embedding Model.

It went through `MemoryStore.get_embedding`, which always asked Ollama for
`nomic-embed-text`, whatever Settings → Models said. Without Ollama the semantic
cache matched nothing, and its warning sent people to a setting that changed
nothing. It also embedded the system prompt with the user's message, cut to the
first 20,000 characters, so a long system prompt made every message on a step
one hit.

Now it embeds the user's message through `embed_batch_ex` with the Embedding
Model, scoped by the chat model and the system prompt; with none set it is
exact match only, and says where to set one.
"""
import hashlib
import math

import pytest

from core import llm_providers as llm
from core.cache import response_cache as rc

EMBED_A = "text-embedding-3-small"
EMBED_B = "gemini-embedding-001"


def _vector(text: str, width: int) -> list[float]:
    """The same unit vector for texts that differ only in case or end punctuation."""
    norm = text.strip().lower().rstrip("?!. ")
    digest = hashlib.sha256(norm.encode()).digest()
    raw = [((digest[i % len(digest)] + 1) * (i + 1)) % 251 - 125 for i in range(width)]
    size = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / size for v in raw]


@pytest.fixture
def embeds(monkeypatch):
    """Every `embed_batch_ex` call; 4 wide for EMBED_A, 8 for anything else."""
    box: dict = {"calls": [], "vectors": {}, "fail": None}

    async def fake(texts, model, settings, *, input_type=None, dims=None):
        box["calls"].append({"texts": list(texts), "model": model, "input_type": input_type})
        if box["fail"] is not None:
            raise box["fail"]
        width = 4 if model == EMBED_A else 8
        vectors = [box["vectors"].get(t) or _vector(t, width) for t in texts]
        return llm.EmbeddingResponse(vectors, model, llm._providers.provider_for(model).id)

    monkeypatch.setattr(llm, "embed_batch_ex", fake)
    return box


@pytest.fixture
def ollama_asked(monkeypatch):
    """Every Ollama client anyone builds (there should be none)."""
    import ollama

    built: list = []

    class Client:
        def __init__(self, *a, **k):
            built.append(k)

        def embeddings(self, **k):
            raise ConnectionError("no Ollama here")

    monkeypatch.setattr(ollama, "Client", Client)
    return built


@pytest.fixture
def store(monkeypatch):
    """A real Chroma-backed MemoryStore in this test's own state dir, as the server holds it."""
    from core import server
    from core.memory import MemoryStore

    mem = MemoryStore()
    monkeypatch.setattr(server, "memory_store", mem, raising=False)
    rc.set_response_cache_backend(None)
    _forget()
    yield mem
    _forget()


def _forget() -> None:
    rc.forget_local_state()


async def _use(model: str) -> None:
    from core.routes.settings import save_settings
    from core.config import load_settings

    settings = load_settings()
    settings["embedding_model"] = model
    await save_settings(settings)
    _forget()


async def _put(step, user, text, *, model="openai.gpt-5-mini", system="sys"):
    await rc.set_semantic_routed(step, model, system, user, text=text, input_tokens=1, output_tokens=2)


async def _ask(step, user, *, model="openai.gpt-5-mini", system="sys"):
    hit = await rc.get_semantic_routed(step, model, system, user)
    return hit and hit["text"]


def _names(mem) -> set[str]:
    return {c if isinstance(c, str) else c.name for c in mem.client.list_collections()}


class TestItEmbedsWithTheEmbeddingModel:
    async def test_a_near_identical_prompt_hits_once_an_embedding_model_is_set(self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        await _put("s1", "What is the capital of France?", "Paris")
        assert await _ask("s1", "what is the capital of france") == "Paris"
        assert await _ask("s1", "Name a colour") is None

    async def test_it_embeds_with_the_workspace_model_and_never_asks_ollama(self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        await _put("s1", "hello", "hi")
        await _ask("s1", "hello")
        assert embeds["calls"] and {c["model"] for c in embeds["calls"]} == {EMBED_A}
        assert ollama_asked == []

    async def test_a_miss_then_its_set_embed_once(self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        assert await _ask("s1", "something new") is None
        await _put("s1", "something new", "answer")
        assert [c["texts"] for c in embeds["calls"]] == [["something new"]]

    async def test_the_collection_measures_cosine(self, store, embeds, ollama_asked):
        """Chroma 0.5, which requirements allow, measures l2 unless told."""
        await _use(EMBED_A)
        embeds["vectors"]["short"] = [0.1, 0.0, 0.0, 0.0]
        embeds["vectors"]["long"] = [3.0, 0.0, 0.0, 0.0]
        await _put("s1", "short", "same direction")
        assert await _ask("s1", "long") == "same direction"


class TestWithoutOne:
    async def test_with_no_embedding_model_semantic_is_skipped_and_the_log_says_where_to_set_one(
            self, store, embeds, ollama_asked, capsys):
        await _use("")
        await _put("s1", "hello", "hi")
        assert await _ask("s1", "hello") is None
        assert embeds["calls"] == [] and ollama_asked == []
        said = capsys.readouterr().out
        assert "Embedding Model" in said and "Settings → Models" in said
        assert "nomic" not in said and "Ollama" not in said
        # Exact match never needed an embedding.
        rc.set_exact("m", "sys", [{"role": "user", "content": "hello"}], None,
                     text="hi", input_tokens=1, output_tokens=2)
        assert rc.get_exact("m", "sys", [{"role": "user", "content": "hello"}], None)["text"] == "hi"

    async def test_a_missing_key_is_named(self, store, embeds, ollama_asked, capsys):
        await _use(EMBED_A)
        embeds["fail"] = llm.EmbeddingFailed("OpenAI embeddings need an OpenAI key (Settings → Models).",
                                             provider="openai", missing="openai_key")
        assert await _ask("s1", "hello") is None
        await _put("s1", "hello", "hi")
        assert "OpenAI embeddings need an OpenAI key" in capsys.readouterr().out

    def test_the_memory_store_has_no_ollama_path(self, store, ollama_asked):
        assert store.get_embedding("anything") is None
        assert ollama_asked == []


class TestAChangeOfModel:
    async def test_a_new_embedding_model_starts_its_own_collection_and_drops_the_old(
            self, store, embeds, ollama_asked):
        # What HEAD left behind: one unhashed collection per step, of another width.
        store.client.get_or_create_collection("responses_semantic_s1").add(
            ids=["old"], embeddings=[[0.5] * 3], documents=["old"])
        store.client.get_or_create_collection("responses_semantic_s1_b").add(
            ids=["other"], embeddings=[[0.5] * 3], documents=["other"])
        await _use(EMBED_A)
        await _put("s1", "hello", "four wide")
        assert await _ask("s1", "hello") == "four wide"
        await _use(EMBED_B)
        await _put("s1", "hello", "eight wide")
        assert await _ask("s1", "hello") == "eight wide"
        s1 = {n for n in _names(store) if n.startswith("responses_semantic_s1") and n != "responses_semantic_s1_b"}
        assert len(s1) == 1 and s1 != {"responses_semantic_s1"}
        assert "responses_semantic_s1_b" in _names(store)

    async def test_saving_settings_lets_the_next_call_use_the_new_model(self, store, embeds, ollama_asked, client):
        await _use(EMBED_A)
        await _put("s1", "hello", "hi")
        saved = await client.post("/api/settings", json={"embedding_model": EMBED_B})
        assert saved.status_code == 200, saved.text
        from core import server
        server.memory_store = store  # the save no longer swaps the store; keep this test's one either way
        embeds["calls"].clear()
        await _ask("s1", "hello")
        assert [c["model"] for c in embeds["calls"]] == [EMBED_B]


class TestWhatIsEmbedded:
    async def test_a_long_system_prompt_does_not_make_every_prompt_a_hit(self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        system = "You are a careful assistant. " * 1000
        await _put("s1", "What is 2+2?", "4", system=system)
        assert await _ask("s1", "what is 2+2", system=system) == "4"
        assert await _ask("s1", "Name a colour", system=system) is None
        assert embeds["calls"] and all(
            c["texts"] in (["What is 2+2?"], ["what is 2+2"], ["Name a colour"]) for c in embeds["calls"])

    async def test_two_system_prompts_on_one_step_keep_their_own_entries(self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        await _put("s1", "hello", "from A", system="You are A.")
        await _put("s1", "hello", "from B", system="You are B.")
        assert await _ask("s1", "hello", system="You are A.") == "from A"
        assert await _ask("s1", "hello", system="You are B.") == "from B"

    async def test_a_neighbour_from_another_model_does_not_hide_this_models_entry(
            self, store, embeds, ollama_asked):
        await _use(EMBED_A)
        await _put("s1", "hello", "from one", model="openai.gpt-5-mini")
        await _put("s1", "hello", "from two", model="openai.gpt-5")
        assert await _ask("s1", "hello", model="openai.gpt-5-mini") == "from one"
        assert await _ask("s1", "hello", model="openai.gpt-5") == "from two"
