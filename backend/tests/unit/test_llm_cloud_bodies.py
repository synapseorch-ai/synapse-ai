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
        text, it, ot, cr, cw, *_ = await llm.call_gemini(
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


class TestGeminiRetriesOnlyWhatCanSucceed:
    """A request Gemini refuses as malformed is refused identically every time.
    It was retried five times with 5/10/20/40s backoff — 79 seconds, measured
    2026-09-13, for a delegate whose tool schema carried an empty enum value —
    before the person saw the error it had already had on the first call. The
    other providers already retry only 429/499/5xx."""

    @staticmethod
    def _flaky(monkeypatch, failures):
        from google.genai import errors

        calls = {"n": 0}
        part = SimpleNamespace(function_call=None, text="ok", thought=None)
        cand = SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"), content=SimpleNamespace(parts=[part]))
        ok = SimpleNamespace(candidates=[cand], usage_metadata=None)

        class _Models:
            def generate_content(self, model, contents, config):
                calls["n"] += 1
                if calls["n"] <= len(failures):
                    raise failures[calls["n"] - 1]
                return ok

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(llm, "_gemini_client", SimpleNamespace(models=_Models()))
        monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
        return calls, errors

    async def test_a_refused_request_is_not_retried(self, monkeypatch):
        from google.genai import errors

        refusal = errors.ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                                     "message": "enum[3]: cannot be empty"}})
        calls, _ = self._flaky(monkeypatch, [refusal] * 5)
        with pytest.raises(llm.LLMError) as exc:
            await llm.call_gemini("gemini-2.5-pro", [{"role": "user", "content": "q"}], "sys", "key")
        assert calls["n"] == 1
        assert "INVALID_ARGUMENT" in str(exc.value)

    @pytest.mark.parametrize("code", [429, 500, 503])
    async def test_rate_limits_and_server_errors_are_still_retried(self, monkeypatch, code):
        from google.genai import errors

        cls = errors.ClientError if code < 500 else errors.ServerError
        calls, _ = self._flaky(monkeypatch, [cls(code, {"error": {"code": code, "status": "X", "message": "try later"}})])
        text, *_ = await llm.call_gemini("gemini-2.5-pro", [{"role": "user", "content": "q"}], "sys", "key")
        assert text == "ok" and calls["n"] == 2

    async def test_a_dropped_connection_is_still_retried(self, monkeypatch):
        calls, _ = self._flaky(monkeypatch, [ConnectionError("reset by peer")])
        text, *_ = await llm.call_gemini("gemini-2.5-pro", [{"role": "user", "content": "q"}], "sys", "key")
        assert text == "ok" and calls["n"] == 2


class TestBedrockBody:
    async def test_converse_success(self, monkeypatch):
        class _FakeBedrock:
            def converse(self, **kwargs):
                return {"output": {"message": {"content": [{"text": "bedrock answer"}]}},
                        "usage": {"inputTokens": 12, "outputTokens": 7}}

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        text, it, ot, cr, cw, *_ = await llm.call_bedrock(
            "bedrock.anthropic.claude", [{"role": "user", "content": "q"}], "sys",
            "us-east-1", {})
        assert text == "bedrock answer"
        assert it == 12 and ot == 7


def _client_error(code, message="nope", status=400):
    import botocore.exceptions
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )


class TestBedrockRetries:
    """F6: `call_bedrock` retried every error five times over ~75 s of sleeps, and
    tried the Anthropic-only `invoke_model` payload after any Converse failure in
    the same attempt — so a request that could never succeed was sent up to ten
    times, and the error reported was the fallback's, not the real one. The rule
    is the model gateway's: by error code, never by HTTP status."""

    def _client(self, monkeypatch, converse_errors, invoke=None):
        calls = {"converse": 0, "invoke": 0, "sleeps": []}

        class _FakeBedrock:
            def converse(self, **kwargs):
                calls["converse"] += 1
                if converse_errors:
                    raise converse_errors.pop(0)
                return {"output": {"message": {"content": [{"text": "ok"}]}},
                        "usage": {"inputTokens": 1, "outputTokens": 1}}

            def invoke_model(self, **kwargs):
                calls["invoke"] += 1
                if invoke is not None:
                    raise invoke
                import io
                return {"body": io.BytesIO(b'{"content": [{"text": "old model"}]}')}

        async def _no_sleep(seconds):
            calls["sleeps"].append(seconds)

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
        return calls

    async def test_a_request_bedrock_refuses_is_not_sent_again(self, monkeypatch):
        calls = self._client(monkeypatch, [_client_error("ValidationException", "maxTokens too large")])
        with pytest.raises(llm.LLMError) as exc:
            await llm.call_bedrock("bedrock.amazon.nova-pro-v1:0", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {})
        assert calls["converse"] == 1 and calls["invoke"] == 0 and calls["sleeps"] == []
        assert "ValidationException" in str(exc.value) and "maxTokens too large" in str(exc.value)

    async def test_throttling_is_retried_on_converse_without_the_fallback(self, monkeypatch):
        calls = self._client(monkeypatch, [_client_error("ThrottlingException", "Rate exceeded", 429)])
        text, *_ = await llm.call_bedrock("bedrock.amazon.nova-pro-v1:0", [{"role": "user", "content": "q"}],
                                          "sys", "us-east-1", {})
        assert text == "ok"
        assert calls["converse"] == 2 and calls["invoke"] == 0 and len(calls["sleeps"]) == 1

    async def test_a_model_without_converse_still_falls_back(self, monkeypatch):
        calls = self._client(monkeypatch, [_client_error(
            "ValidationException",
            "This action doesn't support the model that you provided. Try again with a supported text or chat model.",
        )])
        text, *_ = await llm.call_bedrock("bedrock.anthropic.claude-v2", [{"role": "user", "content": "q"}],
                                          "sys", "us-east-1", {})
        assert text == "old model" and calls["invoke"] == 1

    async def test_the_api_key_path_keeps_the_code(self, monkeypatch):
        """It turned every error into `RuntimeError("HTTP 400: …")`, so no rule
        could tell a refusal from a throttle."""
        import boto3

        attempts = {"n": 0}

        class _Client:
            def converse(self, **kwargs):
                attempts["n"] += 1
                raise _client_error("ValidationException", "bad input")

        class _Session:
            events = type("E", (), {"register": lambda *a, **k: None})()

            def client(self, *a, **k):
                return _Client()

        async def _no_sleep(seconds):
            return None

        monkeypatch.setattr(boto3.session, "Session", lambda *a, **k: _Session())
        monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)
        with pytest.raises(llm.LLMError):
            await llm.call_bedrock("bedrock.amazon.nova-pro-v1:0", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {"bedrock_api_key": "ABSKexample"})
        assert attempts["n"] == 1


class TestHuggingFaceGuard:
    async def test_hf_without_torch_errors(self):
        # torch/transformers aren't installed in the test env -> a clear failure,
        # not a silent hang. (If they ARE installed, the model load would fail on a
        # bogus id — still an exception.)
        with pytest.raises(Exception):
            await llm.call_huggingface(
                "hf.nonexistent/model", [{"role": "user", "content": "q"}], "sys", {})
