"""
Reasoning effort "Off" is the least thinking the model can do (F269).

Off sent nothing, which for a model that thinks by default is not off: Gemini 3
and Gemini 2.5 Flash then think dynamically, as much as they like. And because
nothing was sent, the engine also taught the `[REASONING]` text convention in
the system prompt (a plan is what switches it off), so an Off turn could think
twice — on the model's own channel and in the transcript. The stress pass
measured Off as the most expensive setting on `gemini-3-flash-preview`.

Measured 2026-09-28 on the bat-and-ball question, output tokens with thinking:

    model                    nothing sent   lowest setting
    gemini-3-flash-preview   263–299        MINIMAL: 9
    gemini-3.5-flash         314            MINIMAL: 14
    gemini-3.1-flash-lite    9              MINIMAL: 9
    gemini-2.5-flash         291–395        budget 0: 9
    gemini-3.1-pro-preview   298            MINIMAL refused (400); LOW: 329

So Off sends the lowest setting a Gemini model accepts. A Gemini 3 Pro cannot
stop thinking — its lowest level is Low — so for it Off is Low, and the screens
offer no Off that quietly means Low (F65's rule). Other providers are out of
this row (Naveen, 2026-09-28).
"""
import types

import pytest

from core import reasoning as R


class TestOffOnGemini:
    @pytest.mark.parametrize("model", ["gemini-3-flash-preview", "gemini-3.5-flash",
                                       "gemini-3.1-flash-lite-preview"])
    def test_a_gemini_3_flash_is_sent_minimal(self, model):
        plan = R.plan_for(model, "off", 8192, None)
        assert plan is not None, "sending nothing is dynamic thinking, not off"
        assert plan.level == "MINIMAL"
        assert plan.effort == "off"

    @pytest.mark.parametrize("model", ["gemini-3.1-pro-preview", "gemini-3.1-pro-preview-customtools",
                                       "gemini-3-pro-preview"])
    def test_a_gemini_3_pro_runs_off_at_low(self, model):
        """MINIMAL is a 400 on it (measured); Low is the least it thinks."""
        plan = R.plan_for(model, "off", 8192, None)
        assert plan is not None and plan.level == "LOW"

    # Gemini 2.5 Flash's budget of 0 is pinned where "send nothing" used to be:
    # test_reasoning.py::TestModelsThatCannotStopThinking::test_flash_can_actually_be_turned_off.

    def test_gemini_25_pro_keeps_its_floor(self):
        plan = R.plan_for("gemini-2.5-pro", "off", 8192, None)
        assert plan is not None and plan.budget == 128

    def test_a_claude_budget_model_is_still_sent_nothing(self):
        """Omitting `thinking` is off there; nothing about that changes."""
        assert R.plan_for("claude-sonnet-4-6", "off", 8192, None) is None


class TestWhatTheAdapterSends:
    @staticmethod
    def _capture(monkeypatch):
        from core import llm_providers as llm
        seen = {}
        part = types.SimpleNamespace(function_call=None, text="ok", thought=None)
        cand = types.SimpleNamespace(finish_reason=types.SimpleNamespace(name="STOP"),
                                     content=types.SimpleNamespace(parts=[part]))
        response = types.SimpleNamespace(
            candidates=[cand],
            usage_metadata=types.SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                                                 cached_content_token_count=0, thoughts_token_count=0))

        class _Models:
            def generate_content(self, model, contents, config):
                seen["config"] = config
                return response

        monkeypatch.setattr(llm, "_gemini_client", types.SimpleNamespace(models=_Models()))
        return seen

    async def test_gemini_3_flash_off_is_thinking_level_minimal(self, monkeypatch):
        from core import llm_providers as llm
        seen = self._capture(monkeypatch)
        plan = R.plan_for("gemini-3-flash-preview", "off", 8192, None)
        await llm.call_gemini("gemini-3-flash-preview", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=plan.max_output_tokens, reasoning=plan)
        assert seen["config"].thinking_config.thinking_level.value == "MINIMAL"

    async def test_gemini_25_flash_off_is_a_budget_of_zero(self, monkeypatch):
        from core import llm_providers as llm
        seen = self._capture(monkeypatch)
        plan = R.plan_for("gemini-2.5-flash", "off", 8192, None)
        await llm.call_gemini("gemini-2.5-flash", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=plan.max_output_tokens, reasoning=plan)
        assert seen["config"].thinking_config.thinking_budget == 0


class TestAnOffTurnIsNotTaughtToThinkInText:
    async def test_the_system_prompt_drops_the_marker_convention(self, fake_llm):
        """With a plan the model thinks on its own channel only; with none the
        prompt demanded a `[REASONING]` block before every tool call too."""
        from _fakes import seed as S
        from core.react_engine import run_agent_step

        agent = S.make_agent(tools=[], model="gemini-3-flash-preview", reasoning_effort="off")
        fake_llm.script(["done"])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        [ev async for ev in run_agent_step(
            message="hi", agent_id=agent["id"], session_id="s_off", server_module=server,
            agent_override=agent, max_turns=2,
        )]
        system = fake_llm.calls[0]["sys_prompt"]
        assert "[REASONING]...[/REASONING]" not in system
        assert "### ACTION FORMAT" in system


class TestTheScreensAreTold:
    """What Off does, per model, is served — never restated in TypeScript."""

    @pytest.mark.parametrize("model,runs_as", [
        ("gemini-3-flash-preview", "minimum"),
        ("gemini-3.5-flash", "minimum"),
        ("gemini-2.5-pro", "minimum"),
        ("gemini-3.1-pro-preview", "low"),
        ("claude-opus-5", "low"),
        ("gemini-2.5-flash", "off"),
        ("claude-sonnet-4-6", "off"),
    ])
    def test_off_runs_as(self, model, runs_as):
        assert R.off_runs_as(model, None) == runs_as

    def test_a_model_with_no_channel_has_no_answer(self):
        assert R.off_runs_as("gpt-4o", None) is None

    async def test_the_models_route_serves_both_lists(self, client, monkeypatch):
        import core.routes.data as data
        from _fakes.fake_http import FakeResponse, install_httpx

        monkeypatch.setattr(data, "load_settings", lambda: {"gemini_key": "k"})
        listed = ["gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-2.5-flash", "gemini-2.5-pro"]

        def handler(url, kwargs):
            if "generativelanguage" in url:
                return FakeResponse({"models": [{"name": f"models/{m}",
                                                 "supportedGenerationMethods": ["generateContent"]}
                                                for m in listed]})
            return FakeResponse({}, status_code=404)

        install_httpx(monkeypatch, handler)
        body = (await client.get("/api/models")).json()

        assert "gemini-3.1-pro-preview" in body["reasoning_without_off"]
        assert "gemini-3-flash-preview" not in body["reasoning_without_off"]
        assert {"gemini-3-flash-preview", "gemini-2.5-pro"} <= set(body["reasoning_off_minimum"])
        assert "gemini-2.5-flash" not in body["reasoning_off_minimum"]
