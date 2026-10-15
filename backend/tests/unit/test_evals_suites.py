"""Golden sets: a saved list of inputs, re-run and scored together.

The properties worth pinning are the expensive ones. A suite is N full runs
plus N judge calls — the most costly button in the product — so it runs cases
one at a time, it stops when told to, and it reports its tally as it goes
rather than only at the end.
"""
import asyncio
import contextlib
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
    """The stop flag lives in the row, not in a module-level set.

    On a fleet the process serving the click is never the process running the
    suite, so a process-local flag would be a stop button that does nothing and
    reports success. These tests cancel the way the route does — through the
    store — which is the only way that works in both products.
    """

    async def test_a_cancelled_suite_stops_and_says_so(self, fake_llm, monkeypatch):
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=5)

        original = store.update_suite_run

        async def _cancel_after_first(suite_run_id, **values):
            result = await original(suite_run_id, **values)
            if values.get("scored_count") == 1:
                await suites.cancel(suite_run_id)
            return result

        monkeypatch.setattr(store, "update_suite_run", _cancel_after_first)

        result = await suites.run_suite(suite, _server())
        assert result["status"] == "cancelled"
        assert result["scored_count"] < 5

    async def test_what_already_ran_is_kept(self, fake_llm, monkeypatch):
        """Those were real runs that really cost money. Hiding them would make
        the bill unexplainable."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=4)

        original = store.update_suite_run

        async def _cancel_after_first(suite_run_id, **values):
            result = await original(suite_run_id, **values)
            if values.get("scored_count") == 1:
                await suites.cancel(suite_run_id)
            return result

        monkeypatch.setattr(store, "update_suite_run", _cancel_after_first)
        result = await suites.run_suite(suite, _server())

        kept = await store.scores_for_suite_run(result["id"])
        assert len(kept) == result["scored_count"] >= 1

    async def test_cancelling_nothing_says_so(self):
        assert await suites.cancel("esr_nothing") is False

    async def test_a_finished_suite_cannot_be_cancelled(self, fake_llm):
        """The button is gone by then, but a stale tab still has it."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=1)
        result = await suites.run_suite(suite, _server())
        assert await suites.cancel(result["id"]) is False


class TestASecondStartIsRefusedAcrossProcesses:
    async def test_running_for_finds_the_run_in_flight(self, fake_llm, monkeypatch):
        """`running_for` reads the store, so a second Start is refused whichever
        worker picked the first one up."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)

        seen: list[str | None] = []
        original = store.update_suite_run

        async def _observe(suite_run_id, **values):
            result = await original(suite_run_id, **values)
            if values.get("scored_count") == 1:
                seen.append(await suites.running_for(suite["id"]))
            return result

        monkeypatch.setattr(store, "update_suite_run", _observe)
        result = await suites.run_suite(suite, _server())

        assert seen == [result["id"]], "mid-flight, the set reports itself running"
        assert await suites.running_for(suite["id"]) is None, "and stops once it is done"


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


class TestAHostStartsEachCase:
    """F441: a host that meters runs starts each case itself — cloud admits it
    (a hold, a meter with its plan's length), runs it as that run's job and
    charges it. The suite hands the host each case's run id and the way to run
    it; with no host, a case runs as it always has (every test above)."""

    async def test_the_host_runs_each_case_under_its_id(self, fake_llm):
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)
        seen: list[tuple[str, str]] = []

        async def run_case(run_id, session_id, go):
            seen.append((run_id, session_id))
            await go()

        result = await suites.run_suite(suite, _server(), run_case=run_case)

        esr = result["id"]
        assert seen == [(f"suite_{esr}_{i}", f"suite_{esr}") for i in range(3)]
        assert result["status"] == "completed" and result["scored_count"] == 3
        assert result["stop_reason"] == ""
        # Each case ran as the run the host started: its verdict names that run.
        scored = {s["run_id"] for s in await store.scores_for_suite_run(esr)}
        assert scored == {run_id for run_id, _ in seen}

    async def test_a_refused_case_stops_the_suite_and_says_why(self, fake_llm):
        """A wallet that cannot pay for the next case stops the set there, and the
        set's row says so — rather than every later case failing the same way."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)
        calls: list[str] = []

        async def run_case(run_id, session_id, go):
            calls.append(run_id)
            if len(calls) == 2:
                raise suites.CaseRefused("this workspace is out of execution credits")
            await go()

        result = await suites.run_suite(suite, _server(), run_case=run_case)

        assert len(calls) == 2, "nothing after the refused case is started"
        assert result["status"] == "stopped"
        assert result["stop_reason"] == (
            "Stopped before case 2 of 3: this workspace is out of execution credits"
        )
        assert result["scored_count"] == 1 and result["ended_at"]
        assert await suites.running_for(suite["id"]) is None

    async def test_any_other_host_error_is_that_case_failing(self, fake_llm):
        """Only a refusal stops the set. A case that errors is a result, as it is
        without a host."""
        fake_llm.set_default(VERDICT)
        suite, _ = await _setup(case_count=3)
        calls: list[str] = []

        async def run_case(run_id, session_id, go):
            calls.append(run_id)
            if len(calls) == 2:
                raise RuntimeError("the case's job broke")
            await go()

        result = await suites.run_suite(suite, _server(), run_case=run_case)

        assert len(calls) == 3
        assert result["status"] == "completed" and result["scored_count"] == 2
        assert result["stop_reason"] == ""

    async def test_a_case_its_host_cuts_stops_its_run(self, fake_llm, monkeypatch):
        """A host bounds a case — cloud, by its plan's run length — by cancelling
        it. The case's run is pumped in a task of its own, so it used to go on
        running detached once its host had ended it: writing and spending after
        the host had settled its bill."""
        from core.orchestration.engine import OrchestrationEngine

        began, ended = asyncio.Event(), asyncio.Event()

        async def slow_run(self, initial_input, run_id, **_):
            try:
                yield {"type": "orchestration_start", "run_id": run_id}
                began.set()
                await asyncio.sleep(30)
                yield {"type": "orchestration_complete", "run_id": run_id}
            finally:
                ended.set()

        monkeypatch.setattr(OrchestrationEngine, "run", slow_run)
        suite, _ = await _setup(case_count=1, rubric=False)
        stopped_with_its_case: list[bool] = []

        async def run_case(run_id, session_id, go):
            case = asyncio.create_task(go())
            await began.wait()
            case.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await case
            stopped_with_its_case.append(ended.is_set())

        await suites.run_suite(suite, _server(), run_case=run_case)

        assert stopped_with_its_case == [True]


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


class TestASuggestionThatFailedSaysWhy:
    """F512: a model call that failed answered "No usable cases came back." — an empty wallet, a refused key and
    a timeout all read as a model with nothing to suggest. A failure now says what it was; only a reply that holds
    no case is an empty list."""

    ORCH = {"name": "Brief", "description": "d", "steps": []}

    @staticmethod
    def _failing(monkeypatch, exc=None, delay=0.0):
        import core.llm_providers as llm

        async def fake(*_a, **_k):
            if delay:
                await asyncio.sleep(delay)
            if exc is not None:
                raise exc
            return '[{"input": "x"}]'
        monkeypatch.setattr(llm, "generate_response", fake)

    async def test_the_platform_wallet_says_so_in_its_own_words(self, monkeypatch):
        from core.llm_providers import LLMError
        said = "The model wallet is empty. Add balance under Settings, Billing to continue."
        self._failing(monkeypatch, LLMError(f"Model gateway refused the call: {said}", provider="gateway",
                                            status=402))
        with pytest.raises(generate.CasesNotWritten) as caught:
            await generate.propose(self.ORCH, None, {"model": "synapse.nova-pro"})
        assert caught.value.code == "quota_exhausted"
        assert said in str(caught.value) and "Model gateway" not in str(caught.value)

    async def test_a_providers_refusal_is_said_in_our_words(self, monkeypatch):
        from core.llm_providers import LLMError
        from core.model_failures import MODEL_FAILURE
        self._failing(monkeypatch, LLMError("HTTP 429: insufficient_quota sk-proj-abc", provider="openai"))
        with pytest.raises(generate.CasesNotWritten) as caught:
            await generate.propose(self.ORCH, None, {"model": "gpt-4.1-mini"})
        assert caught.value.code == "quota_exhausted"
        assert MODEL_FAILURE["quota_exhausted"] in str(caught.value) and "sk-proj" not in str(caught.value)

    async def test_an_answer_with_nothing_in_it_is_a_failure_too(self, monkeypatch):
        from core.llm_providers import EmptyAnswer
        from core.model_failures import MODEL_FAILURE
        self._failing(monkeypatch, EmptyAnswer("empty"))
        with pytest.raises(generate.CasesNotWritten) as caught:
            await generate.propose(self.ORCH, None, {"model": "gpt-4.1-mini"})
        assert MODEL_FAILURE["model_no_answer"] in str(caught.value)

    async def test_a_model_that_never_answers_says_it_timed_out(self, monkeypatch):
        monkeypatch.setattr(generate, "TIMEOUT_SECONDS", 0.05)
        self._failing(monkeypatch, delay=5)
        with pytest.raises(generate.CasesNotWritten) as caught:
            await generate.propose(self.ORCH, None, {"model": "gpt-4.1-mini"})
        assert caught.value.code == "timeout" and "did not answer" in str(caught.value)

    async def test_a_reply_with_no_case_in_it_is_still_an_empty_list(self, monkeypatch):
        import core.llm_providers as llm

        async def prose(*_a, **_k):
            return "I could not think of any."
        monkeypatch.setattr(llm, "generate_response", prose)
        assert await generate.propose(self.ORCH, None, {"model": "gpt-4.1-mini"}) == []

    async def test_a_reply_with_cases_is_read_as_before(self, monkeypatch):
        self._failing(monkeypatch)
        assert await generate.propose(self.ORCH, None, {"model": "gpt-4.1-mini"}) == [
            {"name": "", "input": "x", "notes": ""}]
