"""
The OpenAI model list offers every chat model the account has (F62).

It kept only ids starting `gpt-4` or `gpt-3.5`, so `gpt-5`, `gpt-4.1` was
listed but `o1`, `o3`, `o4-mini` and `chatgpt-4o-latest` could not be picked —
even after routing learned they are OpenAI's (batch 1A). The list and the router
now agree, and what is left out is what the chat-completions call cannot serve:
audio, image, realtime, speech, transcription, moderation, search, embedding
and the Responses-only models.
"""
import pytest

from _fakes.fake_http import FakeResponse, install_httpx

ACCOUNT = [
    "gpt-5", "gpt-5-mini", "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo",
    "chatgpt-4o-latest", "o1", "o3", "o3-mini", "o4-mini",
    # not chat-completions models
    "gpt-4o-mini-tts", "gpt-4o-transcribe", "gpt-4o-realtime-preview", "gpt-realtime", "gpt-audio",
    "gpt-4o-audio-preview", "gpt-image-1", "chatgpt-image-latest", "gpt-4o-search-preview",
    "gpt-3.5-turbo-instruct", "o1-pro", "o3-pro", "gpt-5-pro", "gpt-5-codex", "o3-deep-research",
    "text-embedding-3-small", "whisper-1", "tts-1", "dall-e-3", "omni-moderation-latest",
    "babbage-002", "orca-mini",
]


@pytest.fixture
def openai_account(monkeypatch):
    import core.routes.data as data
    monkeypatch.setattr(data, "load_settings", lambda: {"openai_key": "sk-test"})

    def handler(url, kwargs):
        if "api.openai.com" in url:
            return FakeResponse({"data": [{"id": i} for i in ACCOUNT]})
        return FakeResponse({}, status_code=404)

    return handler


async def test_every_chat_model_is_listed_and_nothing_else(client, monkeypatch, openai_account):
    install_httpx(monkeypatch, openai_account)

    openai = (await client.get("/api/models")).json()["providers"]["openai"]

    assert sorted(openai["models"]) == sorted([
        "gpt-5", "gpt-5-mini", "gpt-5-chat-latest", "gpt-4.1", "gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo",
        "chatgpt-4o-latest", "o1", "o3", "o3-mini", "o4-mini",
    ])
    assert "text-embedding-3-small" in openai["embedding_models"]


@pytest.mark.parametrize("model", ["gpt-5", "o4-mini", "chatgpt-4o-latest"])
def test_what_is_listed_routes_to_openai(model):
    from core.llm_providers import detect_mode_from_model, is_openai_chat_model

    assert is_openai_chat_model(model)
    assert detect_mode_from_model(model) == "cloud"
