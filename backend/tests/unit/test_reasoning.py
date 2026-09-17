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
        # F9: Claude 5 and the 4.x releases after the table was written, and the
        # cross-region profiles other than us.
        "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8",
        "bedrock.anthropic.claude-haiku-4-5-20251001-v1:0",
        "bedrock.eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "bedrock.global.anthropic.claude-sonnet-4-20250514-v1:0",
        "bedrock.apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
    ])
    def test_native(self, model):
        assert R.supports_native(model)

    @pytest.mark.parametrize("model", [
        "bedrock.us.anthropic.claude-opus-4-7", "bedrock.anthropic.claude-opus-5",
        "bedrock.eu.anthropic.claude-sonnet-4-6", "bedrock.global.anthropic.claude-sonnet-5",
    ])
    def test_adaptive_claude_on_bedrock_keeps_the_convention(self, model):
        """These reject `budget_tokens`, and the Converse shape for adaptive
        thinking is not documented where this was checked — so they keep the
        prompt convention rather than a request that 400s (F9)."""
        assert not R.supports_native(model)

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


class TestModelsADeploymentHasMeasured:
    """`settings["reasoning_models"]` names gateway models that take the ladder.

    Only the deployment serving them knows which do, and how — Bedrock accepts
    `reasoning_effort` for any model and ignores it for most, so the list is
    measured, not inferred (cloud's `scripts/probe-bedrock-reasoning.py`,
    2026-09-14). Two shapes came out of that measurement and they need
    different policies, which is why the value is a kind and not a flag.
    """

    LISTED = {
        "synapse.gpt-oss-120b-1-0": R.GRADED,
        "synapse.qwen3-32b": R.SWITCH,
    }

    def test_a_listed_model_is_asked(self):
        assert R.supports_native("synapse.gpt-oss-120b-1-0", self.LISTED)
        plan = R.plan_for("synapse.gpt-oss-120b-1-0", "high", 4096, self.LISTED)
        assert plan is not None and plan.openai_effort == "high"
        # Unlisted, the same model stays read-only, exactly as before.
        assert R.plan_for("synapse.gpt-oss-120b-1-0", "high", 4096) is None

    def test_a_graded_model_cannot_be_asked_for_off(self):
        """gpt-oss refuses `minimal`; asked for off it runs at its floor."""
        plan = R.plan_for("synapse.gpt-oss-120b-1-0", "off", 4096, self.LISTED)
        assert plan is not None and plan.effort == "low" and plan.openai_effort == "low"

    @pytest.mark.parametrize("effort", ["off", "low", "medium"])
    def test_a_switch_model_below_high_is_not_native(self, effort):
        """qwen3-32b thought at "high" and not at all at low or medium.

        None, not a plan: a plan would drop the `[REASONING]` prompt
        convention for a turn in which the model does not think either — an
        empty panel where there used to be one.
        """
        assert R.plan_for("synapse.qwen3-32b", effort, 4096, self.LISTED) is None

    def test_a_switch_model_at_high_is_asked(self):
        plan = R.plan_for("synapse.qwen3-32b", "high", 4096, self.LISTED)
        assert plan is not None and plan.openai_effort == "high"

    def test_the_kind_is_served_for_the_screens(self):
        assert R.reasoning_kind("claude-sonnet-4-6") == R.LEVELS
        assert R.reasoning_kind("synapse.qwen3-32b", self.LISTED) == R.SWITCH
        assert R.reasoning_kind("synapse.gpt-oss-120b-1-0", self.LISTED) == R.GRADED
        assert R.reasoning_kind("synapse.deepseek-r1", self.LISTED) is None
        assert R.reasoning_kind("gpt-4o") is None

    async def test_the_dispatcher_sends_what_the_deployment_listed(self, monkeypatch):
        """Through `generate_response_ex`, to the gateway adapter's `reasoning`."""
        from core import llm_providers as llm
        from core import usage_tracker

        seen = []

        async def gateway(model, messages, system, settings, **kwargs):
            seen.append(kwargs.get("reasoning"))
            return llm.LLMResult("ok")

        async def no_usage(**_kwargs):
            return None

        monkeypatch.setattr(llm, "call_model_gateway", gateway)
        monkeypatch.setattr(usage_tracker, "log_usage", no_usage)
        settings = {"reasoning_models": self.LISTED}

        await llm.generate_response_ex("q", "s", "cloud", "synapse.qwen3-32b", settings,
                                       reasoning_effort="high")
        await llm.generate_response_ex("q", "s", "cloud", "synapse.qwen3-32b", settings,
                                       reasoning_effort="medium")
        await llm.generate_response_ex("q", "s", "cloud", "synapse.qwen3-32b", {},
                                       reasoning_effort="high")
        assert seen[0] is not None and seen[0].openai_effort == "high"
        assert seen[1] is None, "a switch model below high sends nothing"
        assert seen[2] is None, "unlisted, a gateway model is still only read"

    def test_every_caller_hands_over_the_list(self):
        """Two call sites decide native reasoning; one forgetting the list would
        send effort while the other kept teaching the prompt convention, or the
        reverse. A missing fourth argument is the whole failure, so count them."""
        import ast
        import pathlib

        import core

        short = []
        for path in pathlib.Path(core.__file__).parent.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in ("plan_for", "_plan_for") and len(node.args) + len(node.keywords) < 4:
                    short.append(f"{path.name}:{node.lineno}")
        assert short == [], short

    def test_a_malformed_setting_is_ignored_not_raised(self):
        """A settings value is JSON somebody wrote; a turn is no place to find a typo."""
        for junk in (None, "", [], "synapse.qwen3-32b", {"synapse.qwen3-32b": "sometimes"}):
            assert R.plan_for("synapse.qwen3-32b", "high", 4096, junk) is None
            assert R.reasoning_kind("synapse.qwen3-32b", junk) is None


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


class TestClaudeThinkingModes:
    """F9: which Claude models take `budget_tokens` and which take adaptive
    thinking with an effort — from the claude-api reference, not memory."""

    @pytest.mark.parametrize("model", [
        "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
        "claude-opus-4-6", "claude-sonnet-4-6", "claude-fable-5-1",
    ])
    def test_adaptive(self, model):
        assert R.anthropic_thinking(model) == R.ADAPTIVE

    @pytest.mark.parametrize("model", [
        "claude-haiku-4-5", "claude-sonnet-4-5-20250929", "claude-opus-4-5-20251101",
        "claude-sonnet-4-20250514", "claude-3-7-sonnet-20250219",
    ])
    def test_budget(self, model):
        assert R.anthropic_thinking(model) == R.BUDGET

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"])
    def test_off_runs_at_low_where_thinking_is_on_by_default(self, model):
        """Omitting `thinking` runs adaptive on these, and the reference warns
        against disabling it on Opus 5 — so "off" is the lowest effort."""
        plan = R.plan_for(model, "off", 8192)
        assert plan is not None and plan.effort == "low"

    def test_off_is_off_where_omitting_means_no_thinking(self):
        assert R.plan_for("claude-opus-4-8", "off", 8192) is None


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

    def test_novas_thinking_tags_are_folded_too(self):
        """F32: Amazon Nova spells them `<thinking>`, which the `<think>` check missed."""
        from core.llm_providers import _split_think_tags
        text, reasoning = _split_think_tags(
            "<thinking>The user wants a count.</thinking>\n\nThere are 3."
        )
        assert text == "There are 3."
        assert reasoning == "The user wants a count."

    async def test_every_provider_folds_them_not_only_ollama(self, monkeypatch):
        """F32: only the Ollama branch split inline tags, so Nova on the gateway
        (and on an own-key Bedrock) showed its thinking as the answer — in chat,
        and written into state by an LLM or evaluator step."""
        from core import llm_providers as llm
        from core import usage_tracker

        async def gateway(model, messages, system, settings, **kwargs):
            return llm.LLMResult("<thinking>check the ledger</thinking>Your balance is $4.")

        async def no_usage(**_kwargs):
            return None

        monkeypatch.setattr(llm, "call_model_gateway", gateway)
        monkeypatch.setattr(usage_tracker, "log_usage", no_usage)

        result = await llm.generate_response_ex("q", "s", "cloud", "synapse.nova-pro-v1-0", {})
        assert result.text == "Your balance is $4."
        assert result.reasoning == "check the ledger"


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
