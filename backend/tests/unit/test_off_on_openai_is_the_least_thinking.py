"""Reasoning effort "Off" on an OpenAI reasoning model is the least it can think (F293).

Off sent nothing to every OpenAI model, so each ran at its own default — and
because nothing was sent, the turn was also taught the `[REASONING]` text
convention on top (a plan is what switches that off). F269 closed the same gap
for Gemini; this is OpenAI's half, measured 2026-09-30 with Naveen's key on the
bat-and-ball question (output tokens / of them reasoning, three calls each):

    model          nothing sent                 the least it accepts
    gpt-5-nano     269–396 / 256–384            minimal: 12–13 / 0
    gpt-5-mini     77–141 / 64–128              minimal: 13 / 0
    o4-mini        86–150 / 64–128              low: 22–86 / 0–64 (none, minimal: 400)
    gpt-5.4        7 / 0 (default is none)      none: 7 / 0 (minimal: 400)
    gpt-5.4-mini   7 / 0                        none: 7–13 / 0
    gpt-5.4-nano   7 / 0                        none: 7 / 0

So Off sends `minimal` to gpt-5 and its mini and nano, `none` to gpt-5.x (its
default already, said so the convention is dropped too), and the o-series,
whose least is Low, runs Off as Low — the screens then offer no Off (F65).
"""
import pytest

from core import reasoning as R


class TestOffOnOpenAI:
    @pytest.mark.parametrize("model", ["gpt-5", "gpt-5-mini", "gpt-5-nano"])
    def test_gpt_5_is_sent_minimal(self, model):
        plan = R.plan_for(model, "off", 8192, None)
        assert plan is not None, "sending nothing is its default effort, not off"
        assert plan.openai_effort == "minimal" and plan.effort == "off"

    @pytest.mark.parametrize("model", ["gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5"])
    def test_gpt_5_point_x_is_sent_none(self, model):
        plan = R.plan_for(model, "off", 8192, None)
        assert plan is not None, "a plan is what stops the [REASONING] convention being taught"
        assert plan.openai_effort == "none" and plan.effort == "off"

    @pytest.mark.parametrize("model", ["o4-mini", "o3", "o1"])
    def test_the_o_series_runs_off_as_low(self, model):
        plan = R.plan_for(model, "off", 8192, None)
        assert plan.openai_effort == "low" and plan.effort == "low"

    def test_low_and_up_are_unchanged(self):
        for model in ("gpt-5-mini", "gpt-5.4", "o4-mini"):
            for effort in ("low", "medium", "high"):
                assert R.plan_for(model, effort, 8192, None).openai_effort == effort

    @pytest.mark.parametrize("model", ["gpt-4o", "grok-4", "deepseek-reasoner"])
    def test_other_models_are_untouched(self, model):
        assert R.plan_for(model, "off", 8192, None) is None


class TestTheScreensAreTold:
    @pytest.mark.parametrize("model, runs_as", [
        ("gpt-5-mini", "minimum"), ("gpt-5-nano", "minimum"),
        ("gpt-5.4", "off"), ("gpt-5.4-mini", "off"),
        ("o4-mini", "low"), ("o3", "low"),
    ])
    def test_off_runs_as(self, model, runs_as):
        assert R.off_runs_as(model) == runs_as


class TestWhatIsSent:
    @pytest.mark.parametrize("model, sent", [("gpt-5-mini", "minimal"), ("gpt-5.4", "none"), ("o4-mini", "low")])
    async def test_the_request_carries_it(self, monkeypatch, model, sent):
        import core.llm_providers as llm
        from _fakes.fake_http import FakeResponse, install_httpx
        from core import usage_tracker
        bodies = []

        async def nothing(**_kw):
            return None

        def handler(url, kwargs):
            bodies.append(kwargs.get("json") or {})
            return FakeResponse({"usage": {"prompt_tokens": 5, "completion_tokens": 3},
                                 "choices": [{"message": {"content": "10 cents"}, "finish_reason": "stop"}]})

        monkeypatch.setattr(usage_tracker, "log_usage", nothing)
        install_httpx(monkeypatch, handler)
        await llm.generate_response_ex("q", "s", "cloud", model, {"openai_key": "k"}, reasoning_effort="off")
        assert bodies[-1].get("reasoning_effort") == sent
