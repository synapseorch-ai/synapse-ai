"""Synapse AI asks a request's questions in at most two rounds, then goes on and says what it picked (F1600).

The rule: "asking questions should be there but it should be reasonable and ask once or twice … be
careful on not making the builder just assume or guess things". On prod, a three-word ask for a video
got three rounds of three questions each. The cap was five rounds (F1226's `QUESTIONS`), and two questions code asks
itself never counted: an unsaid period (F1210) and an ambiguous model wish (F966).

Now the cap is two rounds, per build request, never per session: a request that saved something ends, and the next
request asks afresh (`carry_request`). Every door that puts a question to the person counts, the planner's and code's
own. Past the cap nothing more is asked: what would have been asked is the planner's pick, said on the card under "What
I assumed" (`test_a_plan_says_what_it_assumed`). The clarify step's own limit (5) stays as a backstop above the cap:
the cap is held where questions are made, so the step is never reached a third time.
"""
from __future__ import annotations

import json
from pathlib import Path


from core.native_builder import build_plan, change_tools as T
from test_an_unsaid_period_is_asked_once_with_its_dates import PLAN as SALES_PLAN, SALES, desk  # noqa: F401
from test_the_builder_asks_before_it_guesses import _call, _check, _run

ROOT = Path(__file__).resolve().parents[2] / "core" / "native_builder"
STEPS = {s["id"]: s for s in json.loads((ROOT / "orchestration.json").read_text())["steps"]}


class TestTheCap:
    def test_it_is_two_rounds(self):
        assert T.QUESTIONS == 2

    async def test_a_third_round_is_refused_and_the_planner_is_told_to_say_its_pick(self, client, desk):
        run = await _run("Make a weekly report of our sales.", **{T.ROUNDS_KEY: 2})
        out = await _call("ask_person", {"question": "Which day should it run?", "about": "when",
                                         "subject": "day"}, run)
        assert "error" in out and "2 questions" in out["error"]
        assert "assumed" in out["error"], "the planner is told where its pick goes: the card's What I assumed"

    async def test_a_second_round_is_still_asked(self, client, desk):
        run = await _run("Make a weekly report of our sales.", **{T.ROUNDS_KEY: 1})
        out = await _call("ask_person", {"question": "Which day should it run?", "about": "when",
                                         "subject": "day"}, run)
        assert out.get("finish") == "ask_person"


class TestCodesOwnQuestionsCount:
    async def test_an_unsaid_period_is_not_asked_past_the_cap(self, client, desk):
        run = await _run(SALES, **{T.ROUNDS_KEY: 2})
        out = await _check(run, SALES_PLAN)
        assert out["outcome"] != "ask", "a third round is never asked, whoever asks it"
        assert "last week" in out["review"].split("## What I assumed", 1)[-1], "the period it took is said"

    async def test_an_unsaid_period_is_still_asked_under_the_cap(self, client, desk):
        run = await _run(SALES)
        out = await _check(run, SALES_PLAN)
        assert out["outcome"] == "ask"

    async def test_a_model_question_is_not_asked_past_the_cap(self, client, desk, monkeypatch):
        async def ambiguous(plan, stronger):
            return {}, [], "Which Claude did you mean: Claude Sonnet 4.6 or Claude Opus 4.5?"
        monkeypatch.setattr(build_plan, "pick_models", ambiguous)
        run = await _run("Make a weekly report of our sales on Claude.", **{T.ROUNDS_KEY: 2})
        out = await _check(run, SALES_PLAN | {"summary": "Each week, the sales report."})
        assert out["outcome"] != "ask"
        assert "## What I assumed" in out["review"]


class TestPerRequestNeverPerSession:
    def test_a_request_that_saved_something_ends_and_the_next_asks_afresh(self):
        from core.orchestration.run_writes import WRITES_KEY

        previous = {T.ROUNDS_KEY: 2, WRITES_KEY: [{"kind": "orchestration", "action": "created"}]}
        assert T.carry_request(previous, [{"step_id": "step_human_clarify"}]) == {}

    def test_a_request_still_open_carries_its_count(self):
        out = T.carry_request({T.ROUNDS_KEY: 1}, [{"step_id": "step_human_clarify"}])
        assert out[T.ROUNDS_KEY] == 2

    def test_the_clarify_steps_limit_stays_a_backstop_above_the_cap(self):
        clarify = STEPS["step_human_clarify"]
        assert clarify["max_iterations"] > T.QUESTIONS and clarify["on_exhausted_step_id"] == "step_plan_rounds"
