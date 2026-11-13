"""
Ollama is asked for a context that holds the call (F1569).

Synapse sent Ollama no `num_ctx`, so Ollama used its own default: 4,096 tokens on an 8 GB GPU (Ollama 0.21 picks it
from the GPU's memory). Synapse AI's planner prompt is about 27,500 tokens, so qwen3 8B read 4,096 of them —
Ollama's log said `truncating input prompt limit=4096 prompt=27448` — and the person was told "I couldn't turn
that into something I can make or change here." (SHN1, a fresh 2.0.8, 11-13). Any long call was cut the same way, and
silently: an agent with many tools, a long chat, a step reading a long document.

Now a call that needs more than Ollama's smallest default (4,096) is sent `options.num_ctx`: what the call is, by the
engine's own estimate (four characters a token: the system prompt, the history, the message, the tools), plus room
for the answer (2,048, or the cap someone set where it is more), rounded up to a power of two so calls of a like size share one
loaded model, and never more than the model itself reads (`/api/show`, asked once per model). A model Ollama already
holds with enough room keeps its size (`/api/ps`), and a Modelfile's own `num_ctx` is never cut down: a change of
size makes Ollama load the model again. Where Ollama cannot say how long the model reads, at most 32,768 is asked.
A call longer than the model reads is not sent: the person is told, in words, instead of a silent cut.
A call that fits 4,096 with that room is sent as it always was, with nothing asked of Ollama first.
"""
import re

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx

#: About 27,500 tokens by the engine's estimate: Synapse AI's planner prompt in SHN1's run.
BUILDER_PROMPT = "word " * 22_000
BUILDER_TOKENS = len(BUILDER_PROMPT) // 4
ROOM = 2048
TOOLS = [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from core import usage_tracker

    async def nothing(**_kwargs):
        return None

    monkeypatch.setattr(usage_tracker, "log_usage", nothing)
    # What each model reads is remembered per process: each test starts with nothing known.
    monkeypatch.setattr(llm, "_ollama_model_contexts", {}, raising=False)


def _ollama(monkeypatch, model: str, *, longest: int | None = 40960, set_ctx: int | None = None,
            loaded: int | None = None, show_status: int = 200) -> dict:
    """A fake Ollama: `/api/show` says how long `model` reads (and its Modelfile's num_ctx), `/api/ps` what it is
    loaded with; every chat and generate body is kept."""
    seen = {"show": [], "ps": 0, "calls": []}

    def handler(url, kwargs):
        if url.endswith("/api/show"):
            seen["show"].append(kwargs.get("json"))
            if show_status != 200:
                return FakeResponse({"error": f"model '{model}' not found"}, status_code=show_status)
            info = {"general.architecture": "qwen3"}
            if longest:
                info["qwen3.context_length"] = longest
            data = {"model_info": info, "details": {"family": "qwen3"}}
            if set_ctx:
                data["parameters"] = f'num_ctx                        {set_ctx}\nstop                           "<|im_end|>"'
            return FakeResponse(data)
        if url.endswith("/api/ps"):
            seen["ps"] += 1
            models = [{"name": f"{model}:latest", "model": f"{model}:latest", "context_length": loaded}] if loaded else []
            return FakeResponse({"models": models})
        seen["calls"].append((url.rsplit("/", 1)[-1], kwargs.get("json") or {}))
        if url.endswith("/api/chat"):
            return FakeResponse({"message": {"content": "ok"}, "done_reason": "stop"})
        return FakeResponse({"response": "ok", "done_reason": "stop"})

    install_httpx(monkeypatch, handler)
    return seen


async def _ask(model: str, prompt: str = BUILDER_PROMPT, *, tools=None, history=None, settings=None, **kwargs):
    return await llm.generate_response_ex(prompt, "s", "local", f"ollama.{model}", settings or {},
                                          tools=tools, history_messages=history, **kwargs)


def _num_ctx(seen: dict) -> int:
    assert seen["calls"], "nothing was sent to Ollama"
    options = seen["calls"][-1][1].get("options") or {}
    assert "num_ctx" in options, f"Ollama was sent no context size: options={options}"
    return options["num_ctx"]


@pytest.mark.parametrize("tools,endpoint", [(TOOLS, "chat"), (None, "generate")])
async def test_a_long_call_is_sent_a_context_that_holds_it(monkeypatch, tools, endpoint):
    seen = _ollama(monkeypatch, "qwen3", longest=40960)
    await _ask("qwen3", tools=tools)
    assert seen["calls"][-1][0] == endpoint
    assert BUILDER_TOKENS + ROOM <= _num_ctx(seen) <= 40960, seen["calls"][-1][1].get("options")


async def test_a_long_history_counts_as_much_as_a_long_message(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=40960)
    history = [{"role": "user", "content": BUILDER_PROMPT[: len(BUILDER_PROMPT) // 2]},
               {"role": "assistant", "content": BUILDER_PROMPT[len(BUILDER_PROMPT) // 2:]}]
    await _ask("qwen3", "and now?", tools=TOOLS, history=history)
    assert _num_ctx(seen) >= BUILDER_TOKENS + ROOM


async def test_a_cap_someone_set_is_the_room_left_for_the_answer(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=131072)
    await _ask("qwen3", tools=TOOLS, settings={"max_output_tokens": 12000})
    assert _num_ctx(seen) >= BUILDER_TOKENS + 12000
    assert seen["calls"][-1][1]["options"]["num_predict"] == 12000


async def test_never_more_than_the_model_reads(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=30000)
    await _ask("qwen3", tools=TOOLS)
    assert _num_ctx(seen) == 30000


async def test_a_call_longer_than_the_model_reads_is_said_not_cut(monkeypatch):
    seen = _ollama(monkeypatch, "llama2", longest=4096)
    with pytest.raises(llm.LLMError) as err:
        await _ask("llama2", tools=TOOLS)
    assert seen["calls"] == [], "a call Ollama would cut was sent"
    said = err.value.said or ""
    assert "llama2" in said and "4,096" in said and re.search(r"\b27,5\d\d\b", said), said
    assert err.value.code == llm.PROVIDER_REFUSED
    assert not err.value.retryable


async def test_a_model_already_loaded_with_room_keeps_its_size(monkeypatch):
    """Sending a smaller size would make Ollama load it again (OLLAMA_CONTEXT_LENGTH set to 65,536, say)."""
    seen = _ollama(monkeypatch, "qwen3", longest=131072, loaded=65536)
    await _ask("qwen3", tools=TOOLS)
    assert _num_ctx(seen) == 65536


async def test_a_model_loaded_too_small_is_asked_for_more(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=131072, loaded=8192)
    await _ask("qwen3", tools=TOOLS)
    assert BUILDER_TOKENS + ROOM <= _num_ctx(seen) <= 65536


async def test_a_modelfiles_own_context_is_never_cut_down(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3-64k", longest=131072, set_ctx=65536)
    await _ask("qwen3-64k", tools=TOOLS)
    assert _num_ctx(seen) == 65536


@pytest.mark.parametrize("prompt", [BUILDER_PROMPT, "word " * 50_000], ids=["27k-tokens", "62k-tokens"])
async def test_with_no_length_from_ollama_at_most_32k_is_asked(monkeypatch, prompt):
    seen = _ollama(monkeypatch, "mystery", show_status=404)
    await _ask("mystery", prompt, tools=TOOLS)
    assert _num_ctx(seen) == 32768


async def test_the_models_length_is_asked_once(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=40960)
    await _ask("qwen3", tools=TOOLS)
    await _ask("qwen3", tools=TOOLS)
    assert len(seen["show"]) == 1
    assert seen["show"][0].get("model") == "qwen3"


async def test_a_call_that_fits_the_smallest_default_is_sent_as_before(monkeypatch):
    seen = _ollama(monkeypatch, "qwen3", longest=40960)
    await _ask("qwen3", "hello", tools=TOOLS)
    await _ask("qwen3", "hello")
    assert [body.get("options") for _, body in seen["calls"]] == [None, None]
    assert seen["show"] == [] and seen["ps"] == 0


async def test_a_large_workspace_cap_leaves_a_short_call_as_it_was(monkeypatch):
    """A cap set for every model (a hosted one's, say) is still sent as num_predict, and does not make each short
    Ollama call ask for a large context."""
    seen = _ollama(monkeypatch, "qwen3", longest=40960)
    await _ask("qwen3", "hello", tools=TOOLS, settings={"max_output_tokens": 16384})
    assert seen["calls"][-1][1].get("options") == {"num_predict": 16384}
    assert seen["show"] == [] and seen["ps"] == 0
