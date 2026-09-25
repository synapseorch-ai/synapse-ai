"""
The Gemini model list offers the models that can chat, and only ones the
router sends to Google (F187).

It kept every id whose `supportedGenerationMethods` included
`generateContent` — which speech, image and music generation, transcription,
computer use and the deep-research agents all claim — so the Default Model
picker offered `*-tts`, `*-image`, `gemini-3.5-transcribe`, `lyria-*`, … (the
OSS test pass counted 44). Some did not even start with `gemini`, so the router
sent them to Ollama. F62 fixed the same for OpenAI; the list and the router
now agree here too.

`CATALOGUE` is Google's own list as it stood on 2026-09-26 (names and methods
only), fetched with a real key.
"""
import pytest

from _fakes.fake_http import FakeResponse, install_httpx

GENERATE = ["generateContent", "countTokens"]
CATALOGUE = {name: GENERATE for name in [
    "antigravity-preview-05-2026", "antigravity-preview-09-2026", "antigravity-preview-latest",
    "deep-research-max-preview-04-2026", "deep-research-preview-04-2026", "deep-research-pro-preview-12-2025",
    "gemini-2.5-computer-use-preview-10-2025", "gemini-2.5-flash", "gemini-2.5-flash-image",
    "gemini-2.5-flash-lite", "gemini-2.5-flash-preview-tts", "gemini-2.5-pro", "gemini-2.5-pro-preview-tts",
    "gemini-3-flash-preview", "gemini-3-pro-image", "gemini-3-pro-image-preview", "gemini-3.1-flash-image",
    "gemini-3.1-flash-image-preview", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-image",
    "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-tts-preview", "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.5-transcribe",
    "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite-tts",
    "gemini-3.8-flash-tts", "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-omni-1.1-flash",
    "gemini-omni-flash-preview", "gemini-pro-latest", "gemini-robotics-er-2-preview", "gemma-4-26b-a4b-it",
    "gemma-4-31b-it", "lyria-3-clip-preview", "lyria-3-pro-preview", "lyria-3.5", "nano-banana-pro-preview",
]}
CATALOGUE.update({
    "aqa": ["generateAnswer"],
    "gemini-3.8-live": ["bidiGenerateContent"],
    "gemini-embedding-001": ["embedContent", "countTokens"],
    "veo-3.1-generate-preview": ["predictLongRunning"],
})

CHAT = [
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro", "gemini-3-flash-preview",
    "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview", "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash",
    "gemini-3.7-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-flash-lite-latest",
    "gemini-omni-1.1-flash", "gemini-omni-flash-preview", "gemini-pro-latest", "gemini-robotics-er-2-preview",
    "gemma-4-26b-a4b-it", "gemma-4-31b-it",
]


@pytest.fixture
def gemini_account(monkeypatch):
    import core.routes.data as data
    monkeypatch.setattr(data, "load_settings", lambda: {"gemini_key": "AIza" + "t" * 35})

    def handler(url, kwargs):
        if "generativelanguage.googleapis.com" in url:
            return FakeResponse({"models": [
                {"name": f"models/{n}", "supportedGenerationMethods": m} for n, m in CATALOGUE.items()
            ]})
        return FakeResponse({}, status_code=404)

    install_httpx(monkeypatch, handler)


async def test_every_chat_model_is_listed_and_nothing_else(client, gemini_account):
    gemini = (await client.get("/api/models")).json()["providers"]["gemini"]

    assert sorted(gemini["models"]) == sorted(CHAT)
    assert "gemini-embedding-001" in gemini["embedding_models"]


@pytest.mark.parametrize("model", CHAT)
def test_a_listed_model_is_routed_to_google(model):
    from core.llm_providers import detect_mode_from_model, detect_provider_from_model, is_gemini_chat_model

    assert is_gemini_chat_model(model)
    assert detect_provider_from_model(model) == "gemini"
    assert detect_mode_from_model(model) == "cloud"


@pytest.mark.parametrize("model", [
    "gemini-2.5-flash-preview-tts", "gemini-3.8-flash-lite-tts", "gemini-2.5-flash-image",
    "gemini-3-pro-image-preview", "gemini-3.1-flash-lite-image", "gemini-3.5-transcribe",
    "gemini-2.5-computer-use-preview-10-2025", "lyria-3.5", "lyria-3-clip-preview",
    # generateContent, but not routed to Google at all
    "nano-banana-pro-preview", "deep-research-pro-preview-12-2025", "antigravity-preview-latest",
])
def test_a_model_that_cannot_chat_here_is_not(model):
    from core.llm_providers import is_gemini_chat_model

    assert not is_gemini_chat_model(model)


async def test_the_fallback_names_models_google_still_serves(client, monkeypatch):
    """What the picker offers when Google's list cannot be fetched."""
    import core.routes.data as data
    monkeypatch.setattr(data, "load_settings", lambda: {"gemini_key": "AIza" + "t" * 35})
    install_httpx(monkeypatch, lambda url, kwargs: FakeResponse({}, status_code=503))

    gemini = (await client.get("/api/models")).json()["providers"]["gemini"]

    assert gemini["models"] and set(gemini["models"]) <= set(CHAT)

