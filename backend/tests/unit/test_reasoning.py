"""
Reasoning is the provider's, not a prompt convention.

`core.reasoning` decides two things and these pin both: which models have a
native reasoning channel, and what "low" or "high" means in each one's units.
The arithmetic matters more than it looks — Anthropic and Bedrock refuse a
budget that is not strictly under the response cap, and the shipped cap (8192)
is under the `high` budget, so a plan that returned the budget without the
raised cap would 400 at one effort level and work at the other three.
"""
import types

import pytest

from core import reasoning as R


class TestWhoHasAChannel:
    @pytest.mark.parametrize("model", [
        "claude-sonnet-4-6", "claude-opus-4-5-20251101", "claude-3-7-sonnet-20250219",
        "gemini-2.5-pro", "gemini-2.5-flash", "gemini-3-flash-preview",
        "o3-mini", "gpt-5", "grok-4", "grok-3-mini", "deepseek-reasoner",
        "bedrock.us.anthropic.claude-sonnet-4-20250514-v1:0",
    ])
    def test_native(self, model):
        assert R.supports_native(model)

    @pytest.mark.parametrize("model", [
        # No channel: these reject the parameter rather than ignoring it, which
        # is why the table is a list and not "anything modern".
        "gpt-4o", "gpt-4.1-mini", "claude-3-5-haiku-20241022", "deepseek-chat",
        "mistral", "llama3", "", "gemini-1.5-pro",
    ])
    def test_not_native(self, model):
        assert not R.supports_native(model)
        assert R.plan_for(model, "high", 8192) is None

    def test_a_gateway_model_is_read_but_not_asked(self):
        """We send nothing; we still read what comes back.

        gpt-oss behind our own gateway returns `reasoning_content` unprompted.
        There is nothing to ask for, so `supports_native` is False — but
        dropping the reply's reasoning is what made the panel empty on the one
        model the AI builder actually runs on.
        """
        assert not R.supports_native("synapse.gpt-oss-120b-1-0")
        assert R.reads_native("synapse.gpt-oss-120b-1-0")
        assert R.reads_native("oaic.deepseek-r1")
        assert not R.reads_native("gpt-4o")


class TestTheLadder:
    @pytest.mark.parametrize("effort", R.EFFORT)
    def test_every_effort_produces_a_usable_plan(self, effort):
        plan = R.plan_for("claude-sonnet-4-6", effort, 8192)
        if effort == "off":
            assert plan is None      # nothing to send
            return
        assert plan.budget >= 1024   # Anthropic's own floor
        assert plan.level in ("MINIMAL", "LOW", "MEDIUM", "HIGH")
        assert plan.openai_effort in ("minimal", "low", "medium", "high")

    def test_effort_is_monotonic(self):
        budgets = [R.plan_for("claude-sonnet-4-6", e, 8192).budget for e in ("low", "medium", "high")]
        assert budgets == sorted(budgets) and len(set(budgets)) == 3

    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "gemini-2.5-flash",
                                       "bedrock.us.anthropic.claude-opus-4-5"])
    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_the_budget_always_fits_under_the_cap(self, model, effort):
        """The 400 this module exists to prevent."""
        plan = R.plan_for(model, effort, 8192)
        assert plan.max_output_tokens > plan.budget

    def test_a_generous_caller_cap_is_left_alone(self):
        plan = R.plan_for("claude-sonnet-4-6", "low", 64000)
        assert plan.max_output_tokens == 64000

    def test_unknown_effort_falls_back_rather_than_raising(self):
        """This reads a JSON blob a person or an older version wrote."""
        assert R.normalise("aggressive") == "medium"
        assert R.normalise(None) == "medium"
        assert R.normalise("HIGH") == "high"
        assert R.plan_for("claude-sonnet-4-6", "aggressive", 8192).effort == "medium"


class TestModelsThatCannotStopThinking:
    def test_pro_gets_its_floor_instead_of_an_error(self):
        """Gemini 2.5 Pro refuses a budget of 0. Asking for off is a
        preference, and a model that cannot honour it should still run."""
        plan = R.plan_for("gemini-2.5-pro", "off", 8192)
        assert plan is not None and plan.budget == 128

    def test_flash_can_actually_be_turned_off(self):
        assert R.plan_for("gemini-2.5-flash", "off", 8192) is None


class TestWhatReachesTheWire:
    """The adapters own the shape; these drive them and read what came out."""

    @staticmethod
    def _capture_gemini(monkeypatch):
        """Run `call_gemini` against a fake client and hand back the config it
        actually built — rather than rebuilding it here, which would test the
        test."""
        seen = {}
        part = types.SimpleNamespace(function_call=None, text="ok", thought=None)
        cand = types.SimpleNamespace(finish_reason=types.SimpleNamespace(name="STOP"),
                                     content=types.SimpleNamespace(parts=[part]))
        response = types.SimpleNamespace(
            candidates=[cand],
            usage_metadata=types.SimpleNamespace(prompt_token_count=1, candidates_token_count=1,
                                                 cached_content_token_count=0, thoughts_token_count=40))

        class _Models:
            def generate_content(self, model, contents, config):
                seen["config"] = config
                return response

        from core import llm_providers as llm
        monkeypatch.setattr(llm, "_gemini_client", types.SimpleNamespace(models=_Models()))
        return seen

    async def test_gemini_25_takes_a_budget_and_gemini_3_takes_a_level(self, monkeypatch):
        """The knob changed between generations, and sending the wrong one is
        not an error — it is silence."""
        from core import llm_providers as llm

        seen = self._capture_gemini(monkeypatch)
        plan = R.plan_for("gemini-2.5-flash", "high", 8192)
        await llm.call_gemini("gemini-2.5-flash", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=plan.max_output_tokens, reasoning=plan)
        cfg = seen["config"]
        assert cfg.thinking_config.thinking_budget == 24576
        assert cfg.thinking_config.thinking_level is None
        assert cfg.max_output_tokens == plan.max_output_tokens

        seen = self._capture_gemini(monkeypatch)
        plan3 = R.plan_for("gemini-3-flash-preview", "high", 8192)
        await llm.call_gemini("gemini-3-flash-preview", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=plan3.max_output_tokens, reasoning=plan3)
        cfg3 = seen["config"]
        assert cfg3.thinking_config.thinking_level.value == "HIGH"
        assert cfg3.thinking_config.thinking_budget is None

    async def test_thoughts_are_always_requested(self, monkeypatch):
        """Without this the split in `_extract_gemini_response` has nothing to
        split on, and Gemini's reasoning stays a positional guess."""
        from core import llm_providers as llm
        seen = self._capture_gemini(monkeypatch)
        plan = R.plan_for("gemini-2.5-pro", "medium", 8192)
        await llm.call_gemini("gemini-2.5-pro", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=plan.max_output_tokens, reasoning=plan)
        assert seen["config"].thinking_config.include_thoughts is True

    async def test_gemini_finally_gets_a_response_cap(self, monkeypatch):
        """Every other provider was given `max_output_tokens`; Gemini was
        skipped because it had no hardcoded 4096 to replace, which left it the
        one provider generating without a bound."""
        from core import llm_providers as llm
        seen = self._capture_gemini(monkeypatch)
        await llm.call_gemini("gemini-2.5-flash", [{"role": "user", "content": "q"}], "s", "k",
                              max_tokens=4321)
        assert seen["config"].max_output_tokens == 4321

    async def test_thinking_tokens_are_billed(self, monkeypatch):
        """They are output tokens, reported separately. Leaving them out did
        not merely under-report the count — it under-charged the call."""
        from core import llm_providers as llm
        self._capture_gemini(monkeypatch)
        result = await llm.call_gemini("gemini-2.5-flash", [{"role": "user", "content": "q"}], "s", "k")
        assert result.output_tokens == 1 + 40

    def test_openai_payload_carries_the_effort_string(self):
        from core.llm_providers import _apply_openai_reasoning
        payload = _apply_openai_reasoning({"model": "gpt-5"}, R.plan_for("gpt-5", "low", 8192))
        assert payload["reasoning_effort"] == "low"

    def test_no_plan_means_no_parameter(self):
        from core.llm_providers import _apply_openai_reasoning
        assert "reasoning_effort" not in _apply_openai_reasoning({"model": "gpt-4o"}, None)

    def test_a_400_naming_the_field_drops_it_once(self):
        """An OpenAI-compatible URL is not a promise about a parameter list."""
        from core.llm_providers import _retry_without_reasoning
        payload = {"model": "x", "reasoning_effort": "high"}
        resp = types.SimpleNamespace(text='{"error":{"message":"Unknown parameter: reasoning_effort"}}')
        assert _retry_without_reasoning(payload, resp) is True
        assert "reasoning_effort" not in payload
        # Second time there is nothing left to drop, so the 400 must surface.
        assert _retry_without_reasoning(payload, resp) is False

    def test_a_400_about_something_else_is_left_alone(self):
        from core.llm_providers import _retry_without_reasoning
        payload = {"model": "x", "reasoning_effort": "high"}
        resp = types.SimpleNamespace(text='{"error":{"message":"context length exceeded"}}')
        assert _retry_without_reasoning(payload, resp) is False
        assert payload["reasoning_effort"] == "high"


class TestThinkTags:
    def test_local_models_stop_leaking_their_thinking_into_the_answer(self):
        from core.llm_providers import _split_think_tags
        text, reasoning = _split_think_tags("<think>weighing it up</think>The answer is 42.")
        assert text == "The answer is 42."
        assert reasoning == "weighing it up"

    def test_a_reply_with_no_tags_is_untouched(self):
        from core.llm_providers import _split_think_tags
        assert _split_think_tags("plain answer") == ("plain answer", "")


class TestTheSystemPromptStopsAskingTwice:
    def test_a_native_model_is_told_not_to_write_markers(self):
        from core.tools import _reasoning_protocol
        native = _reasoning_protocol(True)
        assert "do not write `[REASONING]` markers" in native
        assert "Emit a `[REASONING]` block before EVERY tool call" not in native

    def test_everyone_else_keeps_the_convention(self):
        from core.tools import _reasoning_protocol
        markers = _reasoning_protocol(False)
        assert "Emit a `[REASONING]` block before EVERY tool call" in markers

    def test_both_still_teach_the_action_format(self):
        """The JSON shape is how the loop finds a tool call. Only the
        reasoning half is conditional."""
        from core.tools import _reasoning_protocol
        for variant in (_reasoning_protocol(True), _reasoning_protocol(False)):
            assert '{"tool": "<tool_name>", "arguments": {"<key>": "<value>"}}' in variant
