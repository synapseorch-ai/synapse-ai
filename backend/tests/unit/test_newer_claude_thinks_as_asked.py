"""A host may list a model whose "off" is truly off, and Claude Haiku 5.5 thinks adaptively (F1594, BED2).

Two gaps found while cloud brought the newer Claude and GPT models onto its gateway (BED2, 11-14):

- A host lists the gateway models that take the effort ladder in `settings["reasoning_models"]`, and only `graded`
  (off runs at low) and `switch` (thinks only at high) were read. Cloud's Claude Opus 4.6 takes adaptive thinking and
  does not think when asked for nothing, so its honest kind is `levels`: off sends nothing, low to high send the
  level. Listed as `levels`, it was read as unlisted, and the effort control did nothing for it.
- Claude Haiku 5.5 is adaptive-only (`budget_tokens` is a 400) and thinks when `thinking` is omitted, like the rest of
  the Claude 5 family. `claude-haiku-5-5` matched neither `_ADAPTIVE_CLAUDE` nor `_BUDGET_CLAUDE`, so a workspace on
  its own Anthropic key got no native thinking for it at all.
"""
from core import reasoning as R

OPUS_4_6 = "synapse.claude-opus-4-6"
LISTED = {OPUS_4_6: R.LEVELS, "synapse.claude-opus-5-5": R.GRADED}


class TestAHostMayListLevels:
    def test_a_levels_listing_is_read(self):
        assert R.listed_kind(OPUS_4_6, LISTED) == R.LEVELS
        assert R.reasoning_kind(OPUS_4_6, LISTED) == R.LEVELS
        assert R.supports_native(OPUS_4_6, LISTED)

    def test_off_sends_nothing_and_offers_an_off(self):
        """The model does not think when asked for nothing, so off is honest: no plan, and the screens offer Off."""
        assert R.plan_for(OPUS_4_6, "off", 4096, LISTED) is None
        assert R.off_runs_as(OPUS_4_6, LISTED) == "off"

    def test_each_level_is_sent(self):
        for effort in ("low", "medium", "high"):
            plan = R.plan_for(OPUS_4_6, effort, 4096, LISTED)
            assert plan is not None and plan.openai_effort == effort, effort

    def test_graded_and_switch_are_read_as_before(self):
        plan = R.plan_for("synapse.claude-opus-5-5", "off", 4096, LISTED)
        assert plan is not None and plan.openai_effort == "low"
        assert R.listed_kind("synapse.claude-opus-4-6", {OPUS_4_6: "nonsense"}) is None


class TestHaiku55ThinksAdaptively:
    def test_it_takes_adaptive_thinking(self):
        assert R.anthropic_thinking("claude-haiku-5-5") == R.ADAPTIVE
        assert R.supports_native("claude-haiku-5-5")

    def test_it_thinks_when_asked_for_nothing_so_off_runs_at_low(self):
        assert R.off_runs_as("claude-haiku-5-5") == "low"
        plan = R.plan_for("claude-haiku-5-5", "off", 4096)
        assert plan is not None and plan.effort == "low"

    def test_haiku_4_5_keeps_its_budget(self):
        assert R.anthropic_thinking("claude-haiku-4-5-20251001") == R.BUDGET
        assert R.off_runs_as("claude-haiku-4-5-20251001") == "off"
