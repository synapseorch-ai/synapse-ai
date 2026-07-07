"""
Gemini and Bedrock provider bodies, exercised with mocked SDK clients (no
network). Covers response extraction + token accounting. The CLI (subprocess)
and HuggingFace (torch) providers are integration-only; only their guard paths
are checked here.
"""
from types import SimpleNamespace

import pytest

import core.llm_providers as llm


class TestGeminiBody:
    def _fake_client(self, parts, finish="STOP", usage=(10, 5, 0)):
        cand = SimpleNamespace(
            finish_reason=SimpleNamespace(name=finish),
            content=SimpleNamespace(parts=parts),
        )
        um = SimpleNamespace(prompt_token_count=usage[0], candidates_token_count=usage[1],
                             cached_content_token_count=usage[2])
        response = SimpleNamespace(candidates=[cand], usage_metadata=um)

        class _Models:
            def generate_content(self, model, contents, config):
                return response

        return SimpleNamespace(models=_Models())

    async def test_text_response(self, monkeypatch):
        part = SimpleNamespace(function_call=None, text="gemini answer")
        monkeypatch.setattr(llm, "_gemini_client", self._fake_client([part]))
        text, it, ot, cr, cw = await llm.call_gemini(
            "gemini-2.0", [{"role": "user", "content": "q"}], "sys", "key")
        assert text == "gemini answer"
        assert it == 10 and ot == 5

    async def test_empty_candidates_returns_error(self, monkeypatch):
        empty = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda model, contents, config: SimpleNamespace(
                candidates=[], usage_metadata=None)))
        monkeypatch.setattr(llm, "_gemini_client", empty)
        text, *_ = await llm.call_gemini("gemini-2.0", [{"role": "user", "content": "q"}], "sys", "key")
        assert text.startswith("Error")


class TestBedrockBody:
    async def test_converse_success(self, monkeypatch):
        class _FakeBedrock:
            def converse(self, **kwargs):
                return {"output": {"message": {"content": [{"text": "bedrock answer"}]}},
                        "usage": {"inputTokens": 12, "outputTokens": 7}}

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        text, it, ot, cr, cw = await llm.call_bedrock(
            "bedrock.anthropic.claude", [{"role": "user", "content": "q"}], "sys",
            "us-east-1", {})
        assert text == "bedrock answer"
        assert it == 12 and ot == 7


class TestHuggingFaceGuard:
    async def test_hf_without_torch_errors(self):
        # torch/transformers aren't installed in the test env -> a clear failure,
        # not a silent hang. (If they ARE installed, the model load would fail on a
        # bogus id — still an exception.)
        with pytest.raises(Exception):
            await llm.call_huggingface(
                "hf.nonexistent/model", [{"role": "user", "content": "q"}], "sys", {})
