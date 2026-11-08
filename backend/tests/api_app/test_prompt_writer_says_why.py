"""
The agent editor's AI Prompt Writer says why the model could not write (F183).

The OSS test pass pressed Generate on a default model the provider refused
(Gemini's 404, "no longer available to new users"): the route answered 500,
the page showed nothing and the prompt stayed as it was. A provider's refusal
is now a 502 whose detail is a sentence with the provider's own words, which
the page shows under the field. Anything else is still a 500: a bug here is
not the model's fault.
"""
import pytest

from core.llm_providers import EmptyAnswer, LLMError

REFUSED = "Cloud API Error: 404 NOT_FOUND. This model is no longer available to new users."


def _stub(monkeypatch, behaviour):
    import core.routes.agents as agents_mod

    async def generate(*_a, **_kw):
        if isinstance(behaviour, BaseException):
            raise behaviour
        return behaviour

    monkeypatch.setattr(agents_mod, "llm_generate_response", generate)


class TestAModelThatCannotAnswer:
    async def test_is_a_502_that_says_why(self, client, monkeypatch):
        _stub(monkeypatch, LLMError(REFUSED))
        response = await client.post("/api/agents/generate-prompt", json={"description": "a helper"})
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail.startswith("The model couldn't write the prompt: ")
        assert "no longer available to new users" in detail

    async def test_an_empty_answer_is_one_too(self, client, monkeypatch):
        _stub(monkeypatch, EmptyAnswer("The model returned an empty answer."))
        response = await client.post("/api/agents/generate-prompt", json={"description": "a helper"})
        assert response.status_code == 502, response.text
        assert "empty answer" in response.json()["detail"]


class TestEverythingElse:
    async def test_a_bug_is_still_a_500(self, client, monkeypatch):
        _stub(monkeypatch, RuntimeError("boom"))
        response = await client.post("/api/agents/generate-prompt", json={"description": "a helper"})
        assert response.status_code == 500, response.text

    async def test_a_written_prompt_is_unchanged(self, client, monkeypatch):
        _stub(monkeypatch, "You are a helper.")
        response = await client.post("/api/agents/generate-prompt", json={"description": "a helper"})
        assert response.status_code == 200, response.text
        assert response.json() == {"system_prompt": "You are a helper."}
