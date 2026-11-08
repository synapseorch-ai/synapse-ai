"""Reading a judge's answer, and deciding what it is allowed to have read.

The parser is the part that meets a real model, so most of this is about
awkward-but-plausible replies. The one rule worth stating: an unparseable
answer is **None**, never zero. A zero is a verdict — recording one because a
model wrapped its JSON oddly would flag a regression that never happened and
poison the baseline for every run after it.
"""
import pytest

from core.evals import judge

RUBRIC = {"scale": {"min": 0.0, "max": 10.0}, "criteria": [{"id": "c1", "name": "Sources"}]}


class TestAnUnreadableAnswerIsNotAZero:
    @pytest.mark.parametrize("text", [
        "",
        None,
        "I am unable to evaluate this run.",
        '{"reasoning": "I forgot the score"}',
        "{not json at all",
    ])
    def test_none_rather_than_a_verdict(self, text):
        assert judge.parse_verdict(text, RUBRIC) is None


class TestTheParserSurvivesRealModelOutput:
    def test_a_bare_object(self):
        verdict = judge.parse_verdict('{"score": 8.5, "reasoning": "Good."}', RUBRIC)
        assert verdict["score"] == 8.5 and verdict["reasoning"] == "Good."

    def test_a_markdown_fence(self):
        assert judge.parse_verdict('```json\n{"score": 4}\n```', RUBRIC)["score"] == 4.0

    def test_a_preamble_before_the_json(self):
        text = 'Here is my assessment:\n{"score": 6.2, "reasoning": "Fine"}'
        assert judge.parse_verdict(text, RUBRIC)["score"] == 6.2

    def test_a_brace_inside_a_reasoning_string(self):
        """The failure a naive find('{')/rfind('}') pair hits the moment a
        judge quotes the output it is judging."""
        text = '{"score": 3, "reasoning": "It emitted a } and then {oops}"}'
        verdict = judge.parse_verdict(text, RUBRIC)
        assert verdict["score"] == 3.0
        assert verdict["reasoning"] == "It emitted a } and then {oops}"

    def test_the_conclusion_wins_over_a_demonstrated_example(self):
        """A model that shows the format first puts a parseable object in front
        of the real one. The last verdict-shaped object is the answer."""
        text = 'For example {"score": 0, "reasoning": "bad"}. My verdict: {"score": 7, "reasoning": "real"}'
        assert judge.parse_verdict(text, RUBRIC)["score"] == 7.0

    def test_unrelated_json_before_the_verdict_is_skipped(self):
        text = 'Context {"tokens": {"in": 1}} then {"score": 5}'
        assert judge.parse_verdict(text, RUBRIC)["score"] == 5.0


class TestSubscoreKeysAreCheckedAgainstReality:
    """Found by watching a real judge reply, not by a test.

    A model is free to invent a criterion or step id, mangle one, or echo the
    output contract back as data. An unchecked key is stored verbatim and then
    rendered as a subscore for something the run does not have — observed live
    as `per_step` entries called `"<two sentences>"` and `"score"`.
    """

    def test_an_invented_step_id_is_dropped(self):
        text = '{"score": 8, "steps": {"real": {"score": 7}, "<one sentence>": {"score": 3}}}'
        verdict = judge.parse_verdict(text, RUBRIC, step_ids={"real"})
        assert set(verdict["per_step"]) == {"real"}

    def test_an_invented_criterion_id_is_dropped(self):
        text = '{"score": 8, "criteria": {"c1": {"score": 7}, "made_up": {"score": 3}}}'
        verdict = judge.parse_verdict(text, RUBRIC)
        assert set(verdict["per_criterion"]) == {"c1"}

    def test_the_overall_score_survives_unusable_subscores(self):
        """The verdict is still worth keeping — only the keys are dropped."""
        text = '{"score": 8, "steps": {"nonsense": {"score": 1}}}'
        verdict = judge.parse_verdict(text, RUBRIC, step_ids={"real"})
        assert verdict["score"] == 8.0 and verdict["per_step"] == {}

    def test_whitespace_around_a_key_does_not_lose_it(self):
        text = '{"score": 8, "steps": {" real ": {"score": 7}}}'
        assert judge.parse_verdict(text, RUBRIC, step_ids={"real"})["per_step"]["real"]["score"] == 7.0

    def test_a_rubric_with_no_criteria_accepts_whatever_it_is_given(self):
        """`overall` is what the prompt asks for when there are no criteria, so
        there is no list to check against and filtering would drop everything."""
        text = '{"score": 8, "criteria": {"overall": {"score": 8}}}'
        verdict = judge.parse_verdict(text, {"scale": {"min": 0, "max": 10}})
        assert verdict["per_criterion"]["overall"]["score"] == 8.0


class TestScoresAreClampedToTheRubricScale:
    @pytest.mark.parametrize("given,expected", [(99, 10.0), (-5, 0.0), ("7", 7.0), ("nonsense", 0.0)])
    def test_out_of_range_is_pulled_in(self, given, expected):
        assert judge.parse_verdict('{"score": %s}' % (
            given if isinstance(given, (int, float)) else '"%s"' % given), RUBRIC)["score"] == expected

    def test_a_custom_scale_is_respected(self):
        rubric = {"scale": {"min": 1.0, "max": 5.0}}
        assert judge.parse_verdict('{"score": 9}', rubric)["score"] == 5.0

    def test_subscores_are_clamped_too(self):
        text = '{"score": 5, "criteria": {"c1": {"score": 44}}, "steps": {"s1": {"score": -2}}}'
        verdict = judge.parse_verdict(text, RUBRIC)
        assert verdict["per_criterion"]["c1"]["score"] == 10.0
        assert verdict["per_step"]["s1"]["score"] == 0.0

    def test_a_bare_number_subscore_is_accepted(self):
        """Models shorten `{"score": 8}` to `8` constantly; refusing it would
        throw away a usable verdict over formatting."""
        verdict = judge.parse_verdict('{"score": 5, "criteria": {"c1": 8}}', RUBRIC)
        assert verdict["per_criterion"]["c1"] == {"score": 8.0, "reasoning": ""}


class _Run:
    """The parts of an OrchestrationRun the judge reads."""
    def __init__(self, **kw):
        self.run_id = kw.get("run_id", "run_1")
        self.orchestration_id = kw.get("orchestration_id", "orch_1")
        self.session_id = kw.get("session_id")
        self.status = kw.get("status", "completed")
        self.shared_state = kw.get("shared_state", {})
        self.step_history = kw.get("step_history", [])


class TestFinalOutputIsRederivedNotStored:
    """The engine computes the final output at finalize and only *emits* it.

    The judge has to reach the same text a person was shown, so it mirrors
    `_build_final_response`'s rule rather than inventing a second one.
    """

    def test_the_last_step_with_an_output_key_in_state_wins(self):
        run = _Run(
            shared_state={"draft": "first pass", "report": "the final report"},
            step_history=[
                {"step_id": "s1", "output_key": "draft"},
                {"step_id": "s2", "output_key": "report"},
            ],
        )
        assert judge.final_output_of(run) == "the final report"

    def test_a_key_missing_from_state_is_skipped(self):
        run = _Run(
            shared_state={"draft": "only this"},
            step_history=[
                {"step_id": "s1", "output_key": "draft"},
                {"step_id": "s2", "output_key": "never_written"},
            ],
        )
        assert judge.final_output_of(run) == "only this"

    def test_a_failed_run_reports_its_error_rather_than_nothing(self):
        """A failure is a quality signal; the judge must be able to see why."""
        run = _Run(status="failed", step_history=[{"step_id": "s1", "error": "boom"}])
        assert "boom" in judge.final_output_of(run)

    def test_a_run_with_nothing_to_show_returns_empty(self):
        assert judge.final_output_of(_Run()) == ""


class TestThePromptCarriesTheEvidence:
    def test_step_ids_are_named_so_subscores_can_be_keyed(self):
        run = _Run(
            shared_state={"out": "x"},
            step_history=[{"step_id": "research", "step_name": "Research",
                           "step_type": "agent", "status": "completed", "output_key": "out"}],
        )
        prompt = judge.build_prompt(run, RUBRIC)
        assert '"research"' in prompt
        assert "Sources" in prompt          # the criterion
        assert "0.0 to 10.0" in prompt or "0 to 10" in prompt

    def test_private_state_keys_are_not_shown_to_the_judge(self):
        """`_exec_memory_*` and friends are rendered deliberately elsewhere;
        dumping them raw would spend the budget twice on the same evidence."""
        run = _Run(shared_state={"_exec_memory_s1": "internal", "visible": "shown"})
        prompt = judge.build_prompt(run, RUBRIC)
        assert "shown" in prompt
        assert "internal" not in prompt

    def test_an_empty_rubric_still_asks_for_something(self):
        prompt = judge.build_prompt(_Run(), {"scale": {"min": 0, "max": 10}})
        assert "overall" in prompt.lower()
