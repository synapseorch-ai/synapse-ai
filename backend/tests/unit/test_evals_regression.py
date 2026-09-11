"""What counts as a regression, and what a baseline is allowed to be made of.

These are the rules a customer is asked to trust, so they are pure functions
and these tests stand nothing up. The two properties worth stating: a rolling
baseline refuses to exist until it has enough samples to mean anything, and the
absolute floor keeps working while it does not — so a brand-new orchestration
is guarded from its first run rather than from its fourth.
"""
import pytest

from core.evals import regression


class TestBaselineRefusesToBeOneOpinion:
    def test_too_few_samples_is_not_a_baseline(self):
        for count in range(regression.MIN_BASELINE_SAMPLES):
            assert regression.baseline([8.0] * count, 10) is None

    def test_enough_samples_is(self):
        assert regression.baseline([8.0] * regression.MIN_BASELINE_SAMPLES, 10) == 8.0

    def test_only_the_window_counts(self):
        # The window is what "recent" means; older runs must not drag it.
        assert regression.baseline([0.0, 0.0, 9.0, 9.0, 9.0], window=3) == 9.0

    def test_a_window_of_zero_has_no_baseline(self):
        assert regression.baseline([9.0] * 10, 0) is None


class TestTheTwoRules:
    def test_floor_fires_below_the_line(self):
        assert regression.verdict(5.9, None, floor=6.0) == (True, "floor")

    def test_floor_does_not_fire_on_the_line(self):
        assert regression.verdict(6.0, None, floor=6.0) == (False, None)

    def test_drop_fires_below_the_baseline_by_more_than_the_delta(self):
        assert regression.verdict(7.3, 8.875, drop=1.5) == (True, "drop")

    def test_drop_does_not_fire_within_the_delta(self):
        assert regression.verdict(7.5, 8.875, drop=1.5) == (False, None)

    def test_exactly_the_delta_below_is_not_a_regression(self):
        """Both rules are strict, and consistently so: a score *on* the floor
        passes, and a score exactly `drop` below the baseline passes. Worth
        pinning because the wording ("more than 1.5 below") is the kind of
        thing a later edit turns into `<=` while believing it a typo fix."""
        assert regression.verdict(8.0, 9.0, drop=1.0) == (False, None)
        assert regression.verdict(7.99, 9.0, drop=1.0) == (True, "drop")

    def test_floor_wins_when_both_would_fire(self):
        # The simpler, more alarming fact is the one to report.
        assert regression.verdict(2.0, 9.0, floor=6.0, drop=1.5) == (True, "floor")

    def test_drop_is_silent_without_a_baseline(self):
        """The half that protects a new orchestration from false alarms."""
        assert regression.verdict(0.0, None, drop=1.5) == (False, None)

    def test_floor_still_guards_a_new_orchestration(self):
        """...and the half that stops "new" meaning "unguarded"."""
        assert regression.verdict(0.0, None, floor=6.0) == (True, "floor")

    def test_both_rules_off_never_flags(self):
        assert regression.verdict(0.0, 9.0, floor=None, drop=None) == (False, None)


class TestDescribeSaysWhatActuallyFired:
    """The screen must not paraphrase the rule in its own words.

    A dashboard that explains a verdict differently from the way it was reached
    is worse than one that explains nothing, because it will be believed.
    """

    def test_floor_names_the_floor(self):
        assert "below the floor of 6.0" in regression.describe("floor", 5.0, None, 6.0, 1.5)

    def test_drop_names_the_average_and_the_delta(self):
        text = regression.describe("drop", 7.3, 8.875, 6.0, 1.5)
        assert "1.5" in text and "8.9" in text

    def test_no_rule_just_reports_the_score(self):
        assert regression.describe(None, 9.0, 8.0, 6.0, 1.5) == "Scored 9.0."


class TestAgreement:
    """The judge's number is only worth something if it can be checked."""

    SCORES = [{"run_id": "a", "score": 9.0}, {"run_id": "b", "score": 2.0}]

    def test_runs_without_a_thumb_are_not_counted(self):
        assert regression.agreement(self.SCORES, {}, 10.0) == {
            "compared": 0, "agreed": 0, "rate": None,
        }

    def test_a_rate_is_never_reported_without_its_denominator(self):
        result = regression.agreement(self.SCORES, {"a": [{"verdict": 1}]}, 10.0)
        assert result["compared"] == 1 and result["rate"] == 1.0

    def test_same_side_of_the_midpoint_is_agreement(self):
        feedback = {"a": [{"verdict": 1}], "b": [{"verdict": -1}]}
        assert regression.agreement(self.SCORES, feedback, 10.0)["rate"] == 1.0

    def test_opposite_sides_is_disagreement(self):
        feedback = {"a": [{"verdict": -1}], "b": [{"verdict": 1}]}
        assert regression.agreement(self.SCORES, feedback, 10.0)["rate"] == 0.0

    def test_thumbs_are_summed_so_a_split_room_still_has_an_opinion(self):
        feedback = {"a": [{"verdict": 1}, {"verdict": 1}, {"verdict": -1}]}
        assert regression.agreement(self.SCORES, feedback, 10.0)["agreed"] == 1

    def test_a_dead_heat_is_no_opinion_rather_than_a_disagreement(self):
        feedback = {"a": [{"verdict": 1}, {"verdict": -1}]}
        assert regression.agreement(self.SCORES, feedback, 10.0)["compared"] == 0

    def test_the_midpoint_follows_the_rubric_scale(self):
        """A score of 3 is positive on a 0-5 scale and negative on 0-10."""
        scores = [{"run_id": "a", "score": 3.0}]
        feedback = {"a": [{"verdict": 1}]}
        assert regression.agreement(scores, feedback, 5.0)["rate"] == 1.0
        assert regression.agreement(scores, feedback, 10.0)["rate"] == 0.0
