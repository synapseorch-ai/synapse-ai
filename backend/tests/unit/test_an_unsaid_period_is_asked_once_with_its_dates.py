"""F1210 (BLD1; CAP3): a period the person did not pin down is asked once, in code, with its dates on their clock.

"Work out last week's sales by region" is two different weeks: the calendar week before or the seven days to
yesterday. The planner picked one without saying so. Now code finds each relative period in the request that the
message does not define, and before any plan is shown asks one question whose choices carry today's dates on the
person's clock. The answer is a rule each run works out from its own date: it reaches the planner with the question
and the review says it, and the question is never asked again in that build.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from _fakes import seed as S
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, periods
from core.orchestration.engine import live
from core.orchestration.state import SharedState

THURSDAY = date(2026, 10, 29)
SALES = "Work out last week's sales by region from this CSV and put the totals in a note."


@pytest.fixture(autouse=True)
def today(monkeypatch):
    seen: list = []

    def now(timezone):
        seen.append(timezone)
        return THURSDAY

    monkeypatch.setattr(periods, "_now", now)
    return seen


def _rules(text: str) -> list[tuple[str, list[str]]]:
    return [(p.said, [c.today for c in p.choices]) for p in periods.find(text, THURSDAY)]


class TestWhatIsAPeriod:
    def test_last_week_is_two_weeks_with_their_dates(self):
        assert _rules(SALES) == [("last week", ["Mon 19 Oct – Sun 25 Oct", "Thu 22 Oct – Wed 28 Oct",
                                                "whatever the data's newest full week is"])]

    @pytest.mark.parametrize("text, said, first", [
        ("Sum the previous month's invoices", "last month", "Tue 1 Sep – Wed 30 Sep"),
        ("Compare this month with the target", "this month", "Thu 1 Oct – Thu 29 Oct"),
        ("Report on last quarter's churn", "last quarter", "Wed 1 Jul – Wed 30 Sep"),
        ("Totals for last year", "last year", "1 Jan – 31 Dec 2025"),
        ("Plan next week's rota", "next week", "Mon 2 Nov – Sun 8 Nov"),
        ("Errors in the past 7 days", "the past 7 days", "Fri 23 Oct – Thu 29 Oct"),
        ("Signups over the last two weeks", "the past 2 weeks", "Mon 12 Oct – Sun 25 Oct"),
    ])
    def test_each_kind_is_found_with_today_s_dates(self, text, said, first):
        [(found, todays)] = _rules(text)
        assert found == said and todays[0] == first

    def test_recent_is_asked_as_a_span(self):
        [(said, todays)] = _rules("Summarise recent support tickets")
        assert said == "recent" and todays[:2] == ["Fri 23 Oct – Thu 29 Oct", "Wed 30 Sep – Thu 29 Oct"]

    @pytest.mark.parametrize("text", [
        "Last week (Mon 19 Oct to Sun 25 Oct) sales by region",
        "Sales for the last calendar week",
        "Sales over a rolling last week",
        "Sales from 2026-10-19 to 2026-10-25",
        "Last week, Monday to Sunday",
        "Month to date revenue, this month",
    ])
    def test_a_period_the_message_defines_is_not_asked(self, text):
        assert periods.find(text, THURSDAY) == []

    @pytest.mark.parametrize("text", ["Send me a weekly report", "Run it every week", "Each month, email the team",
                                      "Draft a reply to this email"])
    def test_a_schedule_or_no_period_is_not_a_period(self, text):
        assert periods.find(text, THURSDAY) == []

    def test_a_period_said_twice_is_asked_once(self):
        assert [s for s, _ in _rules("Last week's sales and last week's refunds")] == ["last week"]


class TestTheQuestion:
    def test_it_is_on_the_person_s_clock(self, today):
        q = periods.question(SALES, "Europe/London")
        assert today == ["Europe/London"]
        assert q.text.splitlines() == [
            'When you say "last week", which days do you mean?',
            "1. The calendar week before the run's date, Monday to Sunday: today, Mon 19 Oct – Sun 25 Oct",
            "2. The 7 days up to the day before the run: today, Thu 22 Oct – Wed 28 Oct",
            "3. The latest full week in the data itself: today, whatever the data's newest full week is"]

    def test_no_period_no_question(self):
        assert periods.question("Draft a reply to this email", "UTC") is None

    def test_a_number_picks_a_rule_worked_out_from_each_run(self):
        asked = periods.question(SALES, "Europe/London").dump()
        [rule] = periods.meanings(asked, "1", "Europe/London")
        assert rule.startswith('"last week" means the calendar week before the run\'s date, Monday to Sunday, on the '
                               "Europe/London clock, worked out from each run's own date (today that is Mon 19 Oct – "
                               "Sun 25 Oct)")
        assert "never write today's dates into a step" in rule

    def test_their_own_words_are_the_rule(self):
        asked = periods.question(SALES, "UTC").dump()
        [rule] = periods.meanings(asked, "Sunday to Saturday", "UTC")
        assert '"last week" means what the person said: "Sunday to Saturday"' in rule


# ── the build route ───────────────────────────────────────────────────────────────────────────────────────────────

class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def desk():
    from core.store.resources import save_tool

    builder_access.forget_runs()
    await save_tool({"name": "read_sales", "description": "Reads the sales CSV.", "tool_type": "http",
                     "method": "GET", "url": "https://api.sales.example/v1/sales", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}})
    await S.seed_agents([S.make_agent(id="agent_desk", name="Sales desk", tools=["read_sales"])])
    yield
    builder_access.forget_runs()


async def _run(message: str):
    run = OrchestrationRun(run_id="builder_f1210", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": message, "person_timezone": "Europe/London"})
    await SharedState(run).checkpoint()
    return run


async def _check(run, draft):
    run.shared_state[build_plan.DRAFT_KEY] = draft
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": draft}, _Server(), run_id=run.run_id)
    out = json.loads(raw) if isinstance(raw, str) else raw
    run.shared_state[build_plan.CHECKED_KEY] = out
    return out


PLAN = {"finish": "submit_plan", "kind": "create", "title": "Weekly sales by region",
        "summary": "Totals last week's sales by region.",
        "steps": [{"id": "read", "type": "tool", "name": "Read the sales", "does": "Reads the CSV", "tool": "read_sales",
                   "next": "total"},
                  {"id": "total", "type": "agent", "name": "Total by region", "does": "Totals the week by region",
                   "agent": "agent_desk", "prompt": "Total the week's sales by region from {state.read}.",
                   "next": "end"}]}


class TestTheBuildAsksOnce:
    async def test_the_first_plan_waits_for_the_period(self, client, desk):
        run = await _run(SALES)
        out = await _check(run, PLAN)
        assert out["outcome"] == "ask" and out["plan"] is None
        assert "Mon 19 Oct – Sun 25 Oct" in out["question"] and "Thu 22 Oct – Wed 28 Oct" in out["question"]

    async def test_a_planner_s_own_question_comes_after_it(self, client, desk):
        run = await _run(SALES)
        out = await _check(run, {"finish": "ask_person", "question": "Which CSV?"})
        assert out["outcome"] == "ask" and out["question"].startswith('When you say "last week"')

    async def test_the_answer_reaches_the_planner_and_the_review_and_is_never_asked_again(self, client, desk):
        run = await _run(SALES)
        await _check(run, PLAN)
        run.shared_state["human_response"] = {"answer": "1"}
        out = await _check(run, PLAN)
        assert out["outcome"] == "review"
        [entry] = out["asked"]
        assert entry["answer"] == "1" and entry["means"][0].startswith('"last week" means the calendar week before')
        assert "## Dates\n- \"last week\" means the calendar week before the run's date" in out["review"]
        assert "never write" not in out["review"]
        run.shared_state.pop("human_response")
        again = await _check(run, PLAN)
        assert again["outcome"] == "review" and "## Dates" in again["review"]

    async def test_a_request_with_no_period_is_not_asked(self, client, desk):
        run = await _run("Total the sales by region from this CSV and put them in a note.")
        out = await _check(run, PLAN)
        assert out["outcome"] == "review" and "## Dates" not in out["review"]

    async def test_a_plan_it_cannot_build_is_said_without_the_question(self, client, desk):
        run = await _run(SALES)
        out = await _check(run, {"finish": "cannot", "why": "No", "nearest": "Try X"})
        assert out["outcome"] == "said"

    async def test_the_questions_asked_alone_carry_it(self, client, desk):
        # Whatever carries a request's questions into a later builder run (F1226) carries this one: a restated
        # request with the answered question in `asked` is not asked about its dates again.
        run = await _run(SALES)
        await _check(run, PLAN)
        run.shared_state["human_response"] = {"answer": "2"}
        asked = (await _check(run, PLAN))["asked"]
        later = await _run(SALES)
        later.shared_state[build_plan.CHECKED_KEY] = {"asked": asked}
        out = await _check(later, PLAN)
        assert out["outcome"] == "review" and "the 7 days up to the day before the run" in out["review"]
