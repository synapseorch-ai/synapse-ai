"""
Coverage for the generate_response dispatch in core.llm_providers — routing to
each provider by model prefix/mode, usage logging, the response-cache paths, and
error handling. Provider call_* functions are stubbed so this focuses on the
dispatch logic itself.
"""
import pytest

from _fakes.fake_http import install_httpx, FakeResponse

# Capture the REAL generate_response at import time (before the autouse fake_llm
# fixture swaps the module attribute). This reference stays the real function.
import core.llm_providers as _llm_mod
_REAL_GENERATE = _llm_mod.generate_response


def _stub_call(text="hello", it=10, ot=5, cr=0, cw=0):
    async def _c(*a, **k):
        return text, it, ot, cr, cw
    return _c


class TestCloudDispatch:
    @pytest.mark.parametrize("model,fn", [
        ("gpt-4o", "call_openai"),
        ("claude-x", "call_anthropic"),
        ("gemini-2.0", "call_gemini"),
        ("grok-2", "call_grok"),
        ("deepseek-chat", "call_deepseek"),
    ])
    async def test_routes_to_provider(self, monkeypatch, model, fn):
        monkeypatch.setattr(_llm_mod, fn, _stub_call(text=f"{fn} said hi"))
        out = await _REAL_GENERATE("hi", "sys", "cloud", model, {"anthropic_key": "x", "openai_key": "x"})
        assert out == f"{fn} said hi"

    async def test_bedrock_estimates_tokens_when_zero(self, monkeypatch):
        monkeypatch.setattr(_llm_mod, "call_bedrock", _stub_call(text="bedrock", it=0, ot=0))
        out = await _REAL_GENERATE("hi", "sys", "bedrock", "bedrock.claude", {"aws_region": "us-east-1"})
        assert out == "bedrock"

    async def test_oaic_and_locv1_route_to_v1_compatible(self, monkeypatch):
        monkeypatch.setattr(_llm_mod, "call_v1_compatible", _stub_call(text="compat"))
        out1 = await _REAL_GENERATE("hi", "s", "cloud", "oaic.model", {"openai_compatible_base_url": "http://x"})
        out2 = await _REAL_GENERATE("hi", "s", "cloud", "locv1.model", {"local_compatible_base_url": "http://y"})
        assert out1 == "compat" and out2 == "compat"

    async def test_unknown_cloud_model(self, monkeypatch):
        out = await _REAL_GENERATE("hi", "s", "cloud", "weirdmodel", {})
        assert "Unknown cloud model" in out

    async def test_cloud_generic_error_is_wrapped(self, monkeypatch):
        async def _boom(*a, **k):
            raise ValueError("kaboom")
        monkeypatch.setattr(_llm_mod, "call_openai", _boom)
        out = await _REAL_GENERATE("hi", "s", "cloud", "gpt-4o", {"openai_key": "x"})
        assert out.startswith("Cloud API Error")

    async def test_llm_error_propagates(self, monkeypatch):
        async def _err(*a, **k):
            raise _llm_mod.LLMError("upstream down")
        monkeypatch.setattr(_llm_mod, "call_openai", _err)
        with pytest.raises(_llm_mod.LLMError):
            await _REAL_GENERATE("hi", "s", "cloud", "gpt-4o", {"openai_key": "x"})


class TestLocalDispatch:
    async def test_ollama_generate_path(self, monkeypatch):
        install_httpx(monkeypatch, FakeResponse({"response": "local answer",
                                                 "prompt_eval_count": 3, "eval_count": 2}))
        out = await _REAL_GENERATE("hi", "sys", "local", "ollama.llama3", {})
        assert out == "local answer"

    async def test_ollama_chat_with_tools_native_call(self, monkeypatch):
        tool_calls = {"message": {"tool_calls": [
            {"function": {"name": "get_time", "arguments": {"tz": "utc"}}}], "content": ""},
            "prompt_eval_count": 1, "eval_count": 1}
        install_httpx(monkeypatch, FakeResponse(tool_calls))
        out = await _REAL_GENERATE("hi", "sys", "local", "ollama.llama3", {},
                                   tools=[{"type": "function", "function": {"name": "get_time"}}])
        assert '"tool": "get_time"' in out

    async def test_ollama_error_is_wrapped(self, monkeypatch):
        install_httpx(monkeypatch, FakeResponse(status_code=500, text="boom"))
        out = await _REAL_GENERATE("hi", "sys", "local", "ollama.llama3", {})
        assert out.startswith("Local Agent Error")


class TestResponseCache:
    async def test_cache_hit_short_circuits(self, monkeypatch):
        from core.cache import response_cache
        model, system, msgs = "gpt-4o", "sys", [{"role": "user", "content": "cached?"}]
        response_cache.set_exact(model, system, msgs, None, text="CACHED", input_tokens=1, output_tokens=1)
        # call_openai must NOT be invoked on a hit.
        async def _fail(*a, **k):
            raise AssertionError("provider should not be called on cache hit")
        monkeypatch.setattr(_llm_mod, "call_openai", _fail)
        out = await _REAL_GENERATE("cached?", system, "cloud", model, {}, cache_response=True)
        assert out == "CACHED"

    async def test_cache_populated_on_success(self, monkeypatch):
        from core.cache import response_cache
        monkeypatch.setattr(_llm_mod, "call_openai", _stub_call(text="FRESH"))
        out = await _REAL_GENERATE("new-prompt", "sys2", "cloud", "gpt-4o", {}, cache_response=True)
        assert out == "FRESH"
        hit = response_cache.get_exact("gpt-4o", "sys2", [{"role": "user", "content": "new-prompt"}], None)
        assert hit and hit["text"] == "FRESH"


class TestMemoryContext:
    async def test_memory_context_appended_to_system(self, monkeypatch):
        seen = {}
        async def _capture(model, messages, system, *a, **k):
            seen["system"] = system
            return "ok", 1, 1, 0, 0
        monkeypatch.setattr(_llm_mod, "call_anthropic", _capture)
        await _REAL_GENERATE("hi", "BASE", "cloud", "claude-x", {"anthropic_key": "x"},
                             memory_context_text="REMEMBER THIS")
        assert "BASE" in seen["system"] and "REMEMBER THIS" in seen["system"]
