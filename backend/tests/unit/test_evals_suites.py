"""Golden sets: a saved list of inputs, re-run and scored together.

The properties worth pinning are the expensive ones. A suite is N full runs
plus N judge calls — the most costly button in the product — so it runs cases
one at a time, it stops when told to, and it reports its tally as it goes
rather than only at the end.
"""
import types

import pytest

from _fakes import seed as S
from core.evals import generate, suites
from core.evals import store as evals_store
from core.store import evals as store

VERDICT = '{"score": 7.0, "reasoning": "Fine.", "criteria": {}, "steps": {}}'


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _setup(case_count=3, rubric=True):
    rub = await evals_store.save_rubric({
        "name": "Quality", "criteria": [{"name": "Overall"}],
        "scale": {"min": 0, "max": 10},
    }) if rubric else None

    orch = S.make_orchestration(
        id="orch_suite", entry_step_id="p",
        steps=[{"id": "p", "name": "Say", "type": "print", "print_content": "hi",
                "output_key": "final", "next_step_id": None}],
        rubric_id=rub["id"] if rub else "",
    )
    await S.seed_orchestrations([orch])

    suite = await evals_store.save_suite({
        "name": "Regression set", "orchestration_id": "orch_suite",
        "cases": [{"name": f"case {i}", "input": f"input {i}"} for i in range(case_count)],
    })
    return suite, rub


class TestASuiteScoresEveryCase:
    async def test_each_case_produces_a_score_tagged_with_the_suite(self, fake_llm):
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)

        result = await suites.run_suite(suite, _server())

        assert result["status"] == "completed"
        assert result["case_count"] == 3
        assert result["scored_count"] == 3
        assert result["mean_score"] == 7.0
        assert len(await store.scores_for_suite_run(result["id"])) == 3

    async def test_a_suite_score_is_also_an_ordinary_run_score(self, fake_llm):
        """A suite run is not a separate kind of execution — each case is a
        real run, and its verdict shows up wherever any run's verdict does.

        This is what makes the feature small: the suite starts runs and keeps a
        tally, and the ordinary terminal hook does all the judging."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=1)

        result = await suites.run_suite(suite, _server())
        by_suite = await store.scores_for_suite_run(result["id"])
        assert len(by_suite) == 1

        # The same verdict, reachable as the run's own score.
        by_run = await store.scores_for_run(by_suite[0]["run_id"])
        assert [s["id"] for s in by_run] == [by_suite[0]["id"]]

    async def test_the_rubric_version_is_recorded_on_the_suite_run(self, fake_llm):
        """So "did this set get better" cannot silently compare across a rubric
        change, exactly as the per-run trend cannot."""
        fake_llm.set_default(VERDICT)
        suite, rubric = await _setup(case_count=1)

        result = await suites.run_suite(suite, _server())
        assert result["rubric_id"] == rubric["id"]
        assert result["rubric_version"] == rubric["version"]


class TestTheTallyIsWrittenAsItGoes:
    async def test_progress_is_readable_before_the_suite_finishes(self, fake_llm):
        """A suite takes minutes. One that recorded only at the end would show
        nothing while it ran, and nothing at all if the process died."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)

        seen: list[int] = []
        original = store.update_suite_run

        async def _spy(suite_run_id, **values):
            if "scored_count" in values:
                seen.append(values["scored_count"])
            return await original(suite_run_id, **values)

        store.update_suite_run = _spy
        try:
            await suites.run_suite(suite, _server())
        finally:
            store.update_suite_run = original

        # One update per case as it lands, not a single write at the end.
        assert seen[:3] == [1, 2, 3]


class TestCancellation:
    async def test_a_cancelled_suite_stops_and_says_so(self, fake_llm, monkeypatch):
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=5)

        run_id_holder = {}
        original = store.create_suite_run

        async def _capture(values):
            run_id_holder["id"] = values["id"]
            return await original(values)

        monkeypatch.setattr(store, "create_suite_run", _capture)

        # Cancel as soon as the first case has been scored.
        original_update = store.update_suite_run

        async def _cancel_after_first(suite_run_id, **values):
            if values.get("scored_count") == 1:
                suites.cancel(suite_run_id)
            return await original_update(suite_run_id, **values)

        monkeypatch.setattr(store, "update_suite_run", _cancel_after_first)

        result = await suites.run_suite(suite, _server())
        assert result["status"] == "cancelled"
        assert result["scored_count"] < 5

    async def test_what_already_ran_is_kept(self, fake_llm, monkeypatch):
        """Those were real runs that really cost money. Hiding them would make
        the bill unexplainable."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=4)

        original_update = store.update_suite_run

        async def _cancel_after_first(suite_run_id, **values):
            if values.get("scored_count") == 1:
                suites.cancel(suite_run_id)
            return await original_update(suite_run_id, **values)

        monkeypatch.setattr(store, "update_suite_run", _cancel_after_first)
        result = await suites.run_suite(suite, _server())

        assert len(await store.scores_for_suite_run(result["id"])) == result["scored_count"] >= 1


class TestASuiteWithoutARubricScoresNothingButStillRuns:
    async def test_it_completes_with_no_scores(self, fake_llm):
        """Worth being explicit: the runs still happen and still cost money.
        The set is just not measuring anything, which is a thing to be able to
        see rather than a crash."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=2, rubric=False)

        result = await suites.run_suite(suite, _server())
        assert result["status"] == "completed"
        assert result["scored_count"] == 0 and result["mean_score"] is None


class TestTheEstimateIsShownBeforeAnythingIsSpent:
    def test_it_counts_runs_and_judge_calls(self):
        suite = {"cases": [{"input": "a"}, {"input": "b"}, {"input": "c"}]}
        assert suites.estimate(suite) == {"cases": 3, "runs": 3, "judge_calls": 3}

    def test_an_empty_set_costs_nothing(self):
        assert suites.estimate({"cases": []})["runs"] == 0


class TestCaseGenerationProposesAndSavesNothing:
    """A generator that wrote straight to the set would put model-authored text
    into the thing the model is later measured against."""

    def test_a_json_array_is_read(self):
        text = '[{"name": "Ordinary", "input": "semiconductors", "notes": "the easy one"}]'
        cases = generate.parse_cases(text, 5)
        assert cases == [{"name": "Ordinary", "input": "semiconductors", "notes": "the easy one"}]

    def test_a_fenced_array_is_read(self):
        cases = generate.parse_cases('```json\n[{"input": "x"}]\n```', 5)
        assert len(cases) == 1 and cases[0]["input"] == "x"

    def test_an_array_after_a_preamble_is_read(self):
        cases = generate.parse_cases('Here you go:\n[{"input": "x"}]', 5)
        assert len(cases) == 1

    def test_a_case_with_no_input_is_dropped(self):
        assert generate.parse_cases('[{"name": "empty", "input": "  "}]', 5) == []

    def test_prose_yields_nothing_rather_than_a_guess(self):
        assert generate.parse_cases("I could not think of any.", 5) == []

    def test_the_count_is_a_ceiling(self):
        text = "[" + ",".join('{"input": "%d"}' % i for i in range(20)) + "]"
        assert len(generate.parse_cases(text, 3)) == 3

    def test_the_prompt_names_the_rubric_criteria(self):
        """The point of generating against a rubric is cases that stress it."""
        prompt = generate.build_prompt(
            {"name": "Brief", "description": "d", "steps": []},
            {"criteria": [{"name": "Sources", "guidance": "At least three"}]},
            5,
        )
        assert "Sources" in prompt and "At least three" in prompt
