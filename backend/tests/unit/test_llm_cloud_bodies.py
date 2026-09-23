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

    async def test_empty_candidates_raise(self, monkeypatch):
        """This asserted the failure came back as the answer's text ("Error: …"),
        which an agent then said and a step stored. It raises now (F133)."""
        empty = SimpleNamespace(models=SimpleNamespace(
            generate_content=lambda model, contents, config: SimpleNamespace(
                candidates=[], usage_metadata=None)))
        monkeypatch.setattr(llm, "_gemini_client", empty)
        with pytest.raises(llm.EmptyAnswer, match="No response candidates"):
            await llm.call_gemini("gemini-2.0", [{"role": "user", "content": "q"}], "sys", "key")


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


class TestAnInferenceProfileIsAModel:
    """F66: `bedrock_inference_profile` replaced every `bedrock.*` model with the
    profile, so an agent that picked Claude on Bedrock ran whatever the profile
    served — with Claude's reasoning payload. A profile is a model now: picked
    as one, sent as one; the workspace setting is not applied."""

    def _recording(self, monkeypatch, error=None):
        sent = []

        class _FakeBedrock:
            def converse(self, **kwargs):
                sent.append(kwargs["modelId"])
                if error is not None:
                    raise error
                return {"output": {"message": {"content": [{"text": "ok"}]}},
                        "usage": {"inputTokens": 1, "outputTokens": 1}}

            def invoke_model(self, **kwargs):
                raise AssertionError("no InvokeModel fallback for an on-demand refusal")

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())
        return sent

    async def test_the_model_picked_is_the_model_called(self, monkeypatch):
        sent = self._recording(monkeypatch)
        await llm.call_bedrock(
            "bedrock.us.anthropic.claude-sonnet-4-5-20250929-v1:0", [{"role": "user", "content": "q"}],
            "sys", "us-east-1", {"bedrock_inference_profile": "us.amazon.nova-pro-v1:0"})
        assert sent == ["us.anthropic.claude-sonnet-4-5-20250929-v1:0"]

    async def test_an_application_profile_arn_is_sent_as_picked(self, monkeypatch):
        sent = self._recording(monkeypatch)
        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
        await llm.call_bedrock(f"bedrock.{arn}", [{"role": "user", "content": "q"}], "sys", "us-east-1", {})
        assert sent == [arn]

    async def test_the_on_demand_refusal_names_the_profile_a_workspace_had_stored(self, monkeypatch):
        """The note for a stored value: the setting stopped doing anything, so
        the error that follows says which model to pick instead."""
        self._recording(monkeypatch, _client_error(
            "ValidationException",
            "Invocation of model ID anthropic.claude-sonnet-4-5-20250929-v1:0 with on-demand throughput "
            "isn't supported. Retry your request with the ID or ARN of an inference profile that contains this model.",
        ))
        with pytest.raises(llm.LLMError) as exc:
            await llm.call_bedrock(
                "bedrock.anthropic.claude-sonnet-4-5-20250929-v1:0", [{"role": "user", "content": "q"}],
                "sys", "us-east-1", {"bedrock_inference_profile": "us.anthropic.claude-sonnet-4-5-20250929-v1:0"})
        message = str(exc.value)
        assert "pick an inference profile as the model" in message
        assert "bedrock.us.anthropic.claude-sonnet-4-5-20250929-v1:0" in message
        assert "no longer applied" in message

    async def test_the_on_demand_refusal_without_a_stored_profile(self, monkeypatch):
        self._recording(monkeypatch, _client_error(
            "ValidationException",
            "Invocation of model ID anthropic.claude-sonnet-4-5 with on-demand throughput isn't supported.",
        ))
        with pytest.raises(llm.LLMError) as exc:
            await llm.call_bedrock("bedrock.anthropic.claude-sonnet-4-5", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {})
        assert "pick an inference profile as the model" in str(exc.value)
        assert "no longer applied" not in str(exc.value)


def test_the_retired_profile_setting_is_gone_from_the_model_and_the_defaults():
    """F66, the last half. A profile is picked as a model now, and both Models
    screens have stopped writing the workspace-wide override (OSS batch 2, cloud
    batch 4), so nothing sets it. A value stored earlier stays in the store —
    the tenant settings are overlaid without the model — which is what lets the
    on-demand refusal above still name it."""
    from core.config import default_settings
    from core.models import Settings

    assert "bedrock_inference_profile" not in Settings.model_fields
    assert "bedrock_inference_profile" not in default_settings()



class TestABedrockReplyThatArrivedIsReadOnce:
    """Bedrock's is the same defect, one turn worse (F138).

    The Converse answer was read inside the inner `try`, so an exception while
    reading a reply that had arrived was taken for a *Converse failure* — which
    falls through to InvokeModel in the same attempt, and then to the outer
    retry. One unreadable reply bought an InvokeModel call and four more
    Converse calls.
    """

    async def test_an_unreadable_converse_reply_buys_nothing(self, monkeypatch):
        calls = {"converse": 0, "invoke": 0}

        class _FakeBedrock:
            def converse(self, **kwargs):
                calls["converse"] += 1
                # Delivered, and not the shape we read: `output` is not a dict.
                return {"output": "surprise", "usage": {}}

            def invoke_model(self, **kwargs):
                calls["invoke"] += 1
                raise AssertionError("InvokeModel must not be tried for a reply that arrived")

        monkeypatch.setattr(llm, "_make_aws_client", lambda *a, **k: _FakeBedrock())

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(llm.asyncio, "sleep", _no_sleep)

        with pytest.raises(Exception):
            await llm.call_bedrock("bedrock.anthropic.claude", [{"role": "user", "content": "q"}],
                                   "sys", "us-east-1", {})
        assert calls == {"converse": 1, "invoke": 0}
