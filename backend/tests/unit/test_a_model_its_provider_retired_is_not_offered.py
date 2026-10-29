"""A model its provider says is gone is offered nowhere again (F1213, CAP3).

Synapse AI picks the lowest-cost model that fits an agent (F966). Google's list showed `gemini-2.5-flash-lite` while a
call answered 404 "This model … is no longer available to new users", so CAP3's stage 0 (10-29) saw the builder give
it to an agent whose first call failed, and nothing remembered: the next build would pick it again. Now a call the
provider refuses because the model is gone marks it unavailable for the workspace, with the provider's sentence; the
workspace's choices (the builder's menu, the agent editor's picker) and `/api/models` (the Models screen, which says
why) leave it out until the mark lapses.
"""
from __future__ import annotations

import time
import types

import pytest

from core import model_availability as A
from core import model_choices
from core.llm_providers import LLMError
from core.native_builder import models_menu as M

@pytest.fixture(autouse=True)
def fresh_cache():
    """This process's memory of what is marked starts empty with each test's store."""
    A._marked_cache.clear()
    yield
    A._marked_cache.clear()


GEMINI_GONE = ("Gemini LLM Error: the request was refused. 404 NOT_FOUND. {'error': {'code': 404, 'message': "
               "'This model models/gemini-2.5-flash-lite is no longer available to new users. Please update your "
               "code to use models/gemini-3.5-flash-lite'}}")


# ── what counts as "the model is gone" ───────────────────────────────────────────────────────────────────────────
class TestTheProviderSaysTheModelIsGone:
    @pytest.mark.parametrize("message", [
        GEMINI_GONE,
        "OpenAI LLM Error: 404 The model `gpt-4-32k` does not exist or you do not have access to it.",
        "Anthropic LLM Error: 404 {'type': 'error', 'error': {'type': 'not_found_error', 'message': 'model: claude-2'}}",
        "Groq LLM Error: 400 The model `mixtral-8x7b-32768` has been decommissioned and is no longer supported.",
    ])
    def test_these_are_gone(self, message):
        assert A.model_gone(LLMError(message))

    @pytest.mark.parametrize("message", [
        "OpenAI LLM Error: 400 This model does not support tools.",            # about the request, not the model
        "Vault Error: 404 File not found: reports/q1.json",                    # not a model
        "Gemini LLM Error: 429 RESOURCE_EXHAUSTED: quota for model gemini-2.5-flash",
        "Anthropic LLM Error: 529 overloaded_error: the model is overloaded",
        "Gemini LLM Error: failed after 3 attempts. Last error: connection reset",
    ])
    def test_these_are_not(self, message):
        assert A.model_gone(LLMError(message)) is None


class TestTheMark:
    async def test_a_gone_model_is_marked_with_the_providers_sentence(self):
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        reason = (await A.unavailable())["gemini-2.5-flash-lite"]["reason"]
        assert reason.startswith("This model models/gemini-2.5-flash-lite is no longer available to new users"), \
            "the provider's own sentence, not our error's wrapping"

    async def test_another_failure_marks_nothing(self):
        await A.note_failure("gemini-2.5-flash", LLMError("Gemini LLM Error: 503 UNAVAILABLE"))
        assert "gemini-2.5-flash" not in await A.unavailable()

    async def test_a_mark_lapses(self, monkeypatch):
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        later = time.time() + (A.KEEP_DAYS + 1) * 86400
        monkeypatch.setattr(A.time, "time", lambda: later)
        assert await A.unavailable() == {}, "a model a provider brings back is offered again"

    @pytest.mark.parametrize("listed", ["gemini-2.5-flash-lite", "models/gemini-2.5-flash-lite",
                                        "gemini.gemini-2.5-flash-lite"])
    def test_a_listed_id_is_the_called_one_whatever_its_prefix(self, listed):
        assert A.same_model(listed, "gemini-2.5-flash-lite")
        assert not A.same_model("gemini-2.5-flash", "gemini-2.5-flash-lite")
        assert not A.same_model("gemini-2.5-flash-lite", "gemini-2.5-flash")


# ── the choices, the menu and the pick ───────────────────────────────────────────────────────────────────────────
@pytest.fixture
def gemini_choices():
    async def listed():
        return {"gemini": ["gemini-2.5-flash", "gemini-2.5-flash-lite"]}
    model_choices.set_model_choices(listed)
    yield
    model_choices.set_model_choices(None)


class TestItIsOfferedNowhere:
    async def test_the_workspaces_choices_leave_it_out(self, gemini_choices):
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        assert await model_choices.choices() == {"gemini": ["gemini-2.5-flash"]}

    async def test_a_404d_model_is_never_picked_again(self, gemini_choices, monkeypatch):
        """CAP3's stage 0, end to end: picked as the lowest-cost fit, refused by its provider, never picked again."""
        before = M.pick("simple", has_tools=True, menu=await M.menu())
        assert before.id == "gemini-2.5-flash-lite", "the case itself: the lowest-cost model that fits"

        from core import llm_providers
        from core.providers import registry

        async def refused(call, spec):
            raise LLMError(GEMINI_GONE)

        spec = types.SimpleNamespace(id="gemini", mode="cloud", chat=refused, error="Gemini LLM Error")
        # `llm_providers._providers` is the registry module itself.
        monkeypatch.setattr(registry, "branch_for", lambda mode, model: spec)
        monkeypatch.setattr(registry, "resolve", lambda chat: chat)
        with pytest.raises(LLMError):
            await llm_providers.generate_response_ex("hi", "sys", "cloud", "gemini-2.5-flash-lite", {})

        after = M.pick("simple", has_tools=True, menu=await M.menu())
        assert after.id == "gemini-2.5-flash"

    async def test_the_models_screen_says_why(self, monkeypatch):
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        info = {"available": True, "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite"]}
        info["models"], left = A.gone_from(info["models"], await A.unavailable())
        assert info["models"] == ["gemini-2.5-flash"]
        assert left[0]["model"] == "gemini-2.5-flash-lite" and "no longer available" in left[0]["reason"]


# ── a mark always has a way back (8a's review) ──────────────────────────────────────────────────────────────────
class TestAMarkHasAWayBack:
    async def test_a_call_that_works_clears_it(self, gemini_choices, monkeypatch):
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        A._forget_cache()

        from core import llm_providers
        from core.providers import registry

        async def answered(call, spec):
            return ("hello", 3, 1, 0, 0, "stop", None, "")

        spec = types.SimpleNamespace(id="gemini", mode="cloud", chat=answered, error="Gemini LLM Error")
        monkeypatch.setattr(registry, "branch_for", lambda mode, model: spec)
        monkeypatch.setattr(registry, "resolve", lambda chat: chat)
        try:
            await llm_providers.generate_response_ex("hi", "sys", "cloud", "gemini-2.5-flash-lite", {})
        except Exception:  # what follows the call (usage, caching) is not this test's; the success hook ran first
            pass
        assert "gemini-2.5-flash-lite" not in await A.unavailable()
        assert await model_choices.choices() == {"gemini": ["gemini-2.5-flash", "gemini-2.5-flash-lite"]}

    async def test_try_again_on_the_models_screen_clears_it(self, gemini_choices):
        from core.routes import data
        await A.note_failure("gemini-2.5-flash-lite", LLMError(GEMINI_GONE))
        out = await data.try_model_again("models/gemini-2.5-flash-lite")
        assert out["cleared"] is True
        assert "gemini-2.5-flash-lite" in (await model_choices.choices())["gemini"]

    @pytest.mark.parametrize("provider,settings", [
        ("ollama", {}),
        ("local_compatible", {}),
        ("openai_compatible", {"openai_compatible_base_url": "http://127.0.0.1:1234/v1"}),
        ("openai_compatible", {"openai_compatible_base_url": "http://192.168.1.20:8000/v1"}),
        ("openai_compatible", {"openai_compatible_base_url": "http://lmstudio.localhost:1234/v1"}),
    ])
    async def test_a_local_providers_model_is_never_marked(self, provider, settings):
        """Ollama's "model not found" means "not pulled yet", and a wrong base URL answers the same."""
        err = LLMError("Ollama Error: 404 model 'llama3.3' not found, try pulling it first")
        assert A.model_gone(err), "the words are a gone model's; only the provider makes it not one"
        await A.note_failure("llama3.3", err, provider=provider, settings=settings)
        assert await A.unavailable() == {}

    async def test_a_remote_openai_compatible_one_is(self):
        await A.note_failure("gpt-oss", LLMError("404 The model `gpt-oss` does not exist"), provider="openai_compatible",
                             settings={"openai_compatible_base_url": "https://api.together.xyz/v1"})
        assert "gpt-oss" in await A.unavailable()


def test_the_gemini_fallback_no_longer_offers_flash_lite():
    import inspect

    from core.routes import data
    assert '"gemini-2.5-flash-lite"' not in inspect.getsource(data.get_models).split("GEMINI_FALLBACK = ")[1][:200]
