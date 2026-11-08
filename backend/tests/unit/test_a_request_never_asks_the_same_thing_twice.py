"""A request never asks the person the same thing twice, across its runs (F1226, CAP3's stage 1).

Brief 6 ("keep competitors' prices in a knowledge base") asked "has the knowledge base been created yet?" 18 times
in 42 minutes. The cap on questions (QUESTIONS) held inside each run, but each message the person sent started a new
run counting from zero, and nothing told it what had been asked. Now a run starts with the questions its
conversation's request has asked so far (from the newest run, unless that one saved something), `ask_person` refuses
one already asked — the same kind of question about the same thing, or nearly the same words where no subject is
named — and gives back the person's answer, and the cap counts the request, not the run.
"""
from __future__ import annotations

import pytest

from core import builder_tools  # noqa: F401 — the builder's tool modules load in their own order first
from core.native_builder import change_tools as T
from core.orchestration.run_writes import WRITES_KEY

from tests.unit.test_an_unsaid_period_is_asked_once_with_its_dates import desk, today  # noqa: F401 — fixtures
from tests.unit.test_the_change_route_decides_in_code import _call, _run

KB = "the knowledge base 'competitor_pricing'"
ASKED = {"question": "Has the competitor_pricing knowledge base been created yet?", "about": "what", "subject": KB}


async def _ask(run, **args):
    out, _ = await _call("ask_person", {**ASKED, **args}, run)
    return out


# ── within a run, and carried into the next ─────────────────────────────────────────────────────────────────────
class TestTheSameQuestion:
    async def test_asked_again_in_the_same_run_is_refused_with_the_answer(self, client):
        run = await _run(run_id="builder_f1226_a")
        assert (await _ask(run))["finish"] == "ask_person"
        run.shared_state["human_response"] = {"answer": "No, not yet"}
        again = await _ask(run, question="Is the competitor pricing KB there now?")
        assert "You asked the person this already" in again["error"] and "No, not yet" in again["error"]

    async def test_a_new_run_of_the_request_carries_what_was_asked(self, client):
        first = await _run(run_id="builder_f1226_b1")
        await _ask(first)
        first.shared_state["human_response"] = {"answer": "I haven't created it"}
        second = await _run(run_id="builder_f1226_b2", **T.carry_request(first.shared_state))
        refused = await _ask(second, question="What is the ID of the competitor_pricing knowledge base?")
        assert "I haven't created it" in refused["error"], "brief 6's second run: refused, with the answer"

    async def test_another_subject_is_asked(self, client):
        run = await _run(run_id="builder_f1226_c", **{T.ASKED_KEY: [{**ASKED, "carried": True}]})
        out = await _ask(run, question="What time on Monday should it run?", about="when", subject="the schedule")
        assert out["finish"] == "ask_person"

    async def test_the_same_subject_asked_another_way_is_still_refused(self, client):
        run = await _run(run_id="builder_f1226_d", **{T.ASKED_KEY: [{**ASKED, "carried": True}]})
        out = await _ask(run, question="Is the pricing KB ready for me to use now?")
        assert "Don't ask it again" in out["error"]
        assert "already" in out["error"]

    @pytest.mark.parametrize("question, refused", [
        ("Has the competitor_pricing knowledge base been created yet?", True),
        ("has the competitor pricing knowledge base been created yet", True),
        ("Which competitors should it watch?", False),
    ])
    async def test_without_a_subject_the_words_decide(self, client, question, refused):
        run = await _run(run_id=f"builder_f1226_e{int(refused)}{len(question)}",
                         **{T.ASKED_KEY: [{"question": ASKED["question"], "about": "what", "carried": True}]})
        out = await _call("ask_person", {"question": question, "about": "what"}, run)
        assert ("error" in out[0]) is refused


# ── the count is the request's ──────────────────────────────────────────────────────────────────────────────────
async def test_the_cap_counts_the_request_s_rounds_across_its_runs(client):
    run = await _run(run_id="builder_f1226_f", **{T.ROUNDS_KEY: T.QUESTIONS})
    out = await _ask(run, subject="something else", question="One more thing?")
    assert "questions already" in out["error"]


def _shown(n: int) -> list[dict]:
    return [{"step_id": "step_human_clarify", "status": "completed"} for _ in range(n)]


class TestWhatARunCarries:
    def test_a_run_that_saved_something_starts_the_next_afresh(self):
        assert T.carry_request({T.ASKED_KEY: [ASKED], WRITES_KEY: [{"kind": "orchestration"}]}, _shown(3)) == {}

    def test_its_questions_with_their_answers_and_its_rounds(self):
        out = T.carry_request({T.ASKED_KEY: [ASKED], T.ROUNDS_KEY: 2, "human_response": {"answer": "Not yet"}},
                              _shown(3))
        assert out[T.ASKED_KEY] == [{**ASKED, "answer": "Not yet", "carried": True}]
        assert out[T.ROUNDS_KEY] == 5, "two before it, three of its own: a question code asked counts as a round"

    def test_the_build_check_s_asked_whole_so_a_code_question_is_known_by_its_entry(self):
        """F1210's date question (BLD1) lives only in the check's `asked`, its answered entry marked by `means`: a
        restated request carries it whole, so the dates are not asked again."""
        from core.native_builder.build_plan import CHECKED_KEY

        dates = {"question": "Which dates is 'last week'?", "answer": "Mon to Sun", "means": [{"period": "last_week"}]}
        out = T.carry_request({CHECKED_KEY: {"outcome": "review", "asked": [dates]}}, [])
        assert out[CHECKED_KEY] == {"asked": [dates]}, "only `asked` is carried, and each entry whole"

    def test_the_first_run_of_a_conversation_has_none(self):
        assert T.carry_request({}, []) == {} and T.carry_request(None) == {}


# ── with F1210's code-asked dates (BLD1): a restated request is not asked them again ───────────────────────────
async def test_a_restated_request_is_not_asked_its_dates_again(client, desk):
    """F1210 asks once which dates an unsaid period means, and keeps the answer only in the check's `asked`. A restated
    request is a new run; F1226's carry brings `asked` whole, so its plan goes to review with those dates, and the
    question counts as one of the request's rounds."""
    from core.native_builder.build_plan import CHECKED_KEY
    from tests.unit.test_an_unsaid_period_is_asked_once_with_its_dates import PLAN, SALES, _check, _run as _sales

    run = await _sales(SALES)
    assert (await _check(run, PLAN))["outcome"] == "ask", "the first run asks which dates 'last week' is"
    run.shared_state["human_response"] = {"answer": "2"}
    await _check(run, PLAN)
    later = await _sales(SALES)
    later.shared_state.update(T.carry_request(run.shared_state, [{"step_id": "step_human_clarify"}]))
    assert later.shared_state[CHECKED_KEY]["asked"], "the dates' answered entry came with it"
    out = await _check(later, PLAN)
    assert out["outcome"] == "review" and "the 7 days up to the day before the run" in out["review"]
    assert later.shared_state[T.ROUNDS_KEY] == 1


# ── classify says whether a new message continues the request (8a's review of the FF14 check, 10-29) ─────────────
def _staged(**previous) -> dict:
    return {T.CARRY_KEY: T.carry_request(previous, [{"step_id": "step_human_clarify"}])}


class TestOnlyAMessageThatContinuesCarries:
    def test_another_request_asks_afresh(self):
        """test_builder_comes_back's "first ask" then "second ask": classify says it does not continue."""
        state = _staged(**{T.ASKED_KEY: [{"question": "What should it do?", "about": "what", "subject": ""}]})
        assert T.adopt_carry(state, False) is False and T.ASKED_KEY not in state and T.CARRY_KEY not in state

    def test_unsure_or_not_said_asks_afresh(self):
        for verdict in (None,):
            state = _staged(**{T.ASKED_KEY: [ASKED]})
            assert T.adopt_carry(state, verdict) is False and T.ASKED_KEY not in state

    def test_a_restated_request_carries(self):
        """Brief 6's second run: the request said again; classify says it continues."""
        state = _staged(**{T.ASKED_KEY: [ASKED]})
        assert T.adopt_carry(state, True) is True
        assert state[T.ASKED_KEY][0]["subject"] == KB and state[T.ROUNDS_KEY] == 1

    @pytest.mark.parametrize("said", [
        "Set up the pricing KB thing we talked about",          # a paraphrase sharing no subject word
        "हाँ, वही वाला — हर सोमवार कीमतें सहेजो",                 # another language
        "yes, the same one",                                      # a short reply
    ])
    def test_classify_decides_not_the_words(self, said):
        """No word of the subject need appear: classify read the conversation and said it continues."""
        state = {"user_message": said, **_staged(**{T.ASKED_KEY: [ASKED]})}
        assert T.adopt_carry(state, True) is True and state[T.ASKED_KEY][0]["subject"] == KB

    def test_a_message_that_shares_words_but_asks_for_something_else_does_not(self):
        state = {"user_message": "Delete the competitor_pricing knowledge base", **_staged(**{T.ASKED_KEY: [ASKED]})}
        assert T.adopt_carry(state, False) is False and T.ASKED_KEY not in state

    def test_the_dates_carry_with_a_continued_request(self):
        from core.native_builder.build_plan import CHECKED_KEY

        dates = {"question": "Which dates is 'last week'?", "answer": "Mon to Sun", "means": [{"period": "last_week"}]}
        state = {T.CARRY_KEY: T.carry_request({CHECKED_KEY: {"asked": [dates]}}, [])}
        assert T.adopt_carry(state, True) and state[CHECKED_KEY] == {"asked": [dates]}


async def test_classify_adopts_the_carry_only_when_it_says_continues(client):
    """Through `classify_request` itself: the model's `continues` is what decides."""
    for verdict, carried in ((True, True), (False, False), (None, False)):
        run = await _run(run_id=f"builder_f1226_cls{verdict}", user_message="the same one",
                         **_staged(**{T.ASKED_KEY: [ASKED]}))
        args = {"kind": "build", **({"continues": verdict} if verdict is not None else {})}
        await _call("classify_request", args, run)
        assert (T.ASKED_KEY in run.shared_state) is carried and T.CARRY_KEY not in run.shared_state
