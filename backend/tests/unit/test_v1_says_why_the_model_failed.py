"""
A v1 caller can tell "top up" from "try again" (F275).

Gemini answered 402 RESOURCE_EXHAUSTED, "Your prepayment credits are depleted";
v1 answered `{"detail": "The agent encountered an error processing your
request."}`, the same words as for a timeout or a 503. An integration cannot act
on that: out of credit wants a top-up and never succeeds on a retry, a provider
that is down wants a retry. And OpenAI's `insufficient_quota` 429 — permanent —
was retried five times with backoff before anyone heard.

Now every model failure carries a stable `code` and whether retrying can help,
and v1 says which in our own sentence. The provider's own words stay with a
member in the app, as D118 decided (Naveen, 2026-09-28): v1 keeps its first
sentence, its 500, and never the provider's text.
"""
import asyncio
import json

import pytest

import core.llm_providers as llm
from _fakes.fake_http import FakeResponse, install_httpx
from core.llm_providers import EmptyAnswer, LLMError

GENERIC = "The agent encountered an error processing your request."


# ── the classification ───────────────────────────────────────────────────────

class TestEveryFailureHasACode:
    @pytest.mark.parametrize("message,code", [
        # What the stress pass saw, exactly as the Gemini adapter words it.
        ("Gemini LLM Error: the request was refused. 402 RESOURCE_EXHAUSTED. {'error': {'code': 402, "
         "'message': 'Your prepayment credits are depleted.', 'status': 'RESOURCE_EXHAUSTED'}}", "quota_exhausted"),
        ("OpenAI LLM Error: failed after 1 attempt. Last error: HTTP 429: {\"error\": {\"type\": "
         "\"insufficient_quota\", \"message\": \"You exceeded your current quota, please check your plan and "
         "billing details.\"}}", "quota_exhausted"),
        ("Claude LLM Error: API error 400: Your credit balance is too low to access the Anthropic API.",
         "quota_exhausted"),
        ("OpenAI LLM Error: failed after 5 attempts. Last error: HTTP 429: Rate limit reached for gpt-4o "
         "on tokens per min.", "rate_limited"),
        # Gemini's free-tier per-minute limit reads like OpenAI's permanent quota, and clears.
        ("Gemini LLM Error: failed after 5 attempts. Last error: 429 RESOURCE_EXHAUSTED. You exceeded your "
         "current quota, please check your plan and billing details.", "rate_limited"),
        ("OpenAI LLM Error: failed after 1 attempt. Last error: HTTP 401: Incorrect API key provided.",
         "provider_refused"),
        ("Gemini LLM Error: the request was refused. 404 NOT_FOUND. This model is no longer available.",
         "provider_refused"),
        ("OpenAI LLM Error: failed after 5 attempts. Last error: HTTP 503: The server is overloaded.",
         "provider_unavailable"),
        ("OpenAI LLM Error: failed after 5 attempts. Last error: Request timed out (180s)",
         "provider_unavailable"),
        ("Local Agent Error: Ollama is not reachable at http://127.0.0.1:11434.", "provider_unavailable"),
    ])
    def test_the_message_it_already_carries_is_enough(self, message, code):
        assert LLMError(message).code == code

    def test_a_status_given_outright_wins(self):
        err = LLMError("Model gateway refused the call: Balance too low.", provider="gateway", status=402)
        assert err.code == "quota_exhausted" and err.status == 402 and err.provider == "gateway"

    def test_an_empty_answer_is_no_answer(self):
        assert EmptyAnswer("Error: Gemini returned only reasoning").code == "model_no_answer"

    @pytest.mark.parametrize("code,retryable", [
        ("quota_exhausted", False), ("provider_refused", False), ("model_no_answer", False),
        ("rate_limited", True), ("provider_unavailable", True),
    ])
    def test_whether_a_retry_can_help(self, code, retryable):
        assert LLMError("x", code=code).retryable is retryable


# ── a quota is not retried ───────────────────────────────────────────────────

@pytest.fixture
def no_backoff(monkeypatch):
    real = asyncio.sleep

    async def instant(_delay, *args, **kwargs):
        await real(0)

    monkeypatch.setattr(llm.asyncio, "sleep", instant)


class TestAQuotaIsNotRetried:
    QUOTA = FakeResponse(status_code=429, text=json.dumps({"error": {
        "type": "insufficient_quota", "code": "insufficient_quota",
        "message": "You exceeded your current quota, please check your plan and billing details."}}))
    RATE = FakeResponse(status_code=429, text="Rate limit reached for requests per min.")

    def _count(self, monkeypatch, response):
        calls = []

        def handler(url, kwargs):
            calls.append(url)
            return response

        install_httpx(monkeypatch, handler)
        return calls

    async def test_openai(self, monkeypatch, no_backoff):
        calls = self._count(monkeypatch, self.QUOTA)
        with pytest.raises(LLMError) as raised:
            await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")
        assert len(calls) == 1, "a depleted quota never succeeds on a retry"
        assert raised.value.code == "quota_exhausted"

    async def test_a_v1_compatible_endpoint(self, monkeypatch, no_backoff):
        calls = self._count(monkeypatch, self.QUOTA)
        with pytest.raises(LLMError) as raised:
            await llm.call_v1_compatible("m", [{"role": "user", "content": "q"}], "sys", "http://host", "key")
        assert len(calls) == 1
        assert raised.value.code == "quota_exhausted"

    async def test_a_rate_limit_is_still_retried(self, monkeypatch, no_backoff):
        calls = self._count(monkeypatch, self.RATE)
        with pytest.raises(LLMError) as raised:
            await llm.call_openai("gpt-4o", [{"role": "user", "content": "q"}], "key")
        assert len(calls) == 5
        assert raised.value.code == "rate_limited"


# ── what v1 says ─────────────────────────────────────────────────────────────

WORDS = "Your prepayment credits are depleted. SECRETWORDS"


def _failing(monkeypatch, error):
    import core.react_engine as re_mod

    async def refused(*_a, **_kw):
        raise error

    monkeypatch.setattr(re_mod, "llm_generate_response", refused)


class TestWhatV1Says:
    async def test_out_of_credit(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _failing(monkeypatch, LLMError(f"Gemini LLM Error: the request was refused. 402 RESOURCE_EXHAUSTED. {WORDS}"))
        sync = await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])
        assert sync.status_code == 500
        body = sync.json()
        assert body["code"] == "quota_exhausted" and body["retryable"] is False
        assert body["detail"].startswith(GENERIC) and "credit or quota" in body["detail"]
        assert "SECRETWORDS" not in sync.text, "the provider's words stay in the app (D118)"

    async def test_a_provider_that_is_down(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _failing(monkeypatch, LLMError("OpenAI LLM Error: failed after 5 attempts. Last error: HTTP 503: SECRETWORDS"))
        sync = await client.post("/api/v1/chat", json={"message": "hi"}, headers=api_key["headers"])
        body = sync.json()
        assert sync.status_code == 500 and body["code"] == "provider_unavailable" and body["retryable"] is True
        assert "SECRETWORDS" not in sync.text

    async def test_the_stream_says_it_too(self, client, api_key, seed_agent, monkeypatch):
        await seed_agent()
        _failing(monkeypatch, LLMError(f"Gemini LLM Error: the request was refused. 402 RESOURCE_EXHAUSTED. {WORDS}"))
        stream = await client.post("/api/v1/chat/stream", json={"message": "hi"}, headers=api_key["headers"])
        frames = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")]
        (error,) = [f for f in frames if f.get("type") == "error"]
        assert error["code"] == "quota_exhausted" and error["retryable"] is False
        assert error["message"].startswith(GENERIC)
        assert "SECRETWORDS" not in stream.text
