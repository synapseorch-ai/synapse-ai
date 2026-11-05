"""A schedule Synapse AI writes is on its own clock, and its time is said from its cron (F1393, TRI1; BLD1's dry run,
eb's trace; Naveen 11-05: "Fix in v2.0", "Build as planned").

BLD1's dry run: "every morning at 7" in Porto, with the browser on IST, became `30 1 * * *` with Europe/Lisbon, and
ran at 01:30 Lisbon. Since F948 a cron is read on its schedule's own clock (the timezone the person named, else their
own, which build_plan fills), but the planner's rule 5 still said "a five-field cron in UTC", and every reader the
model sees said "(UTC)" whatever the schedule's clock was, so it converted. Another dry run: the card said "Calcutta
time" while the build's summary said "(UTC)" for the same schedule.

Now:
- rule 5 and the change ops' fields say the cron is written on the schedule's own clock, never converted to UTC;
- the card (the review's words and the app's plan card), the change line and the build's summary say the time from
  the cron read on its clock, in code's words (`schedule_clock.cron_words`, the app's `cronWords`);
- a plan or a change whose own words name a time its cron doesn't run at goes back once with both, then is taken;
- the schedules reader, the help and the docs say each schedule's own clock;
- a stored schedule with no timezone is UTC, as every 1.x schedule is (REL3's upgrade read: "Weekly hello"
  `0 9 * * 1` runs at 09:00 UTC before and after), and nothing here changes it.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _planner() -> str:
    from core.native_builder import prompt_lint

    return " ".join(prompt_lint.expanded_places()["agent:agent_native_builder_planner"].split())


# ── the words the model is given ─────────────────────────────────────────────────────────────────────────────────

class TestThePrompt:
    def test_rule_5_writes_the_cron_on_the_schedules_own_clock(self):
        text = _planner()
        rule = text[text.index("5. Add a schedule only when"):text.index("# ", text.index("5. Add a schedule only when"))]
        rule = rule[:rule.index(" 6. ")] if " 6. " in rule else rule
        assert "own clock" in rule and "never converted to UTC" in rule, rule
        assert "cron in UTC" not in rule, rule

    def test_the_change_ops_say_the_cron_is_on_the_schedules_own_clock(self):
        from core.native_builder import change_tools

        source = " ".join(inspect.getsource(change_tools).split())
        for op in ("set_schedule {cron_expression", "create_schedule {runs: {kind: workflow | agent, id}, prompt, "
                                                    "cron_expression"):
            at = source.index(op)
            assert "own clock" in source[at:at + 200], source[at:at + 200]

    def test_the_plans_schedule_field_says_it_too(self):
        from core.native_builder.objects import Schedule

        assert "own clock" in Schedule.model_fields["cron"].description


# ── a cron in words, on its clock ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cron, tz, words", [
    ("0 7 * * *", "Europe/Lisbon", "Daily 07:00 Lisbon time"),
    ("30 1 * * *", None, "Daily 01:30 UTC"),
    ("0 16 * * 5", "America/New_York", "Fridays 16:00 New York time"),
    ("0 9 * * 1-5", "Asia/Kolkata", "Weekdays 09:00 Kolkata time"),
    ("0 8 1 * *", "UTC", "Monthly, 1st 08:00 UTC"),
    ("*/15 * * * *", "Asia/Kolkata", "Every 15 minutes"),
    ("0 7 1-7 * 1", None, "0 7 1-7 * 1"),
])
def test_a_cron_in_words_on_its_clock(cron, tz, words):
    from core.native_builder import schedule_clock

    assert schedule_clock.cron_words(cron, tz) == words


def test_the_words_match_the_apps():
    lists = (ROOT / "frontend/src/components/run/workflowList.ts").read_text()
    assert "const DAYS = ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays'];" in lists
    assert "return `Monthly, ${ordinalDay(Number(dom))} ${time}`;" in lists


@pytest.mark.parametrize("words, times", [
    ("Every morning at 7:00", {(7, 0), (19, 0)}),
    ("every day at 7am", {(7, 0)}),
    ("Fridays at 4 PM", {(16, 0)}),
    ("weekdays at 9.30pm", {(21, 30)}),
    ("every day at noon", {(12, 0)}),
    ("at midnight", {(0, 0)}),
    ("every Monday", set()),
    ("every 15 minutes", set()),
])
def test_the_times_their_words_name(words, times):
    from core.native_builder import schedule_clock

    assert schedule_clock.said_times(words) == times


# ── the plan route: the card says the cron's time; words that disagree go back once ──────────────────────────────

class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _morning(cron: str, says: str, **schedule) -> dict:
    return {"finish": "submit_plan", "kind": "create", "title": "Morning brief", "summary": "Writes the brief.",
            "steps": [{"id": "write", "type": "llm", "name": "Write it", "does": "Writes the morning brief",
                       "prompt": "Write today's brief."}],
            "schedule": {"cron": cron, "says": says, **schedule}}


async def _run(**state):
    from core import builder_access
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_f1393", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})
    await SharedState(run).checkpoint()
    return run


async def _call(tool: str, args: dict, run) -> dict:
    from core.builder_tools import execute_builder_tool_with_events
    from core.orchestration.engine import live

    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _checked(draft, run):
    from core.native_builder import build_plan

    run.shared_state[build_plan.DRAFT_KEY] = draft
    return await _call("check_build", {"draft": draft}, run)


@pytest.fixture(autouse=True)
def _forget():
    from core import builder_access

    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


class TestThePlan:
    async def test_porto_at_7_written_as_utc_goes_back_with_both(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        out = await _checked(_morning("30 1 * * *", "Every morning at 7:00"), run)
        assert "build" not in out["offers"], out
        [said] = [i for i in out["issues"] if "own clock" in i]
        assert "7:00" in said and "`30 1 * * *`" in said and "01:30" in said and "Lisbon time" in said, said

    async def test_it_goes_back_once_then_the_plan_is_taken(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        await _checked(_morning("30 1 * * *", "Every morning at 7:00"), run)
        out = await _checked(_morning("30 1 * * *", "Every morning at 7:00"), run)
        assert out["outcome"] == "review" and not any("own clock" in i for i in out.get("issues") or [])
        assert "**When:** Daily 01:30 (`30 1 * * *`, Lisbon time)" in out["review"], out["review"]

    async def test_a_cron_on_their_clock_is_reviewed_and_the_card_says_it_from_the_cron(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        out = await _checked(_morning("0 7 * * *", "Every morning at 7"), run)
        assert out["outcome"] == "review", out
        assert "**When:** Daily 07:00 (`0 7 * * *`, Lisbon time)" in out["review"], out["review"]

    async def test_a_named_clock_is_the_crons(self, client):
        run = await _run(person_timezone="Asia/Kolkata")
        out = await _checked(_morning("0 16 * * 5", "Fridays at 4 PM", timezone="America/New_York"), run)
        assert out["outcome"] == "review" and "**When:** Fridays 16:00 (`0 16 * * 5`, New York time)" in out["review"]

    async def test_words_with_no_time_are_not_checked(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        out = await _checked(_morning("0 7 * * 1", "Every Monday"), run)
        assert out["outcome"] == "review", out

    def test_the_apps_plan_card_says_the_time_from_the_cron(self):
        plan_ts = (ROOT / "frontend/src/components/assistant/plan.ts").read_text()
        assert "const fromCron = cronOf ? cronWords(cronOf)" in plan_ts
        at = plan_ts.index("const when = ", plan_ts.index("const fromCron = "))
        line = plan_ts[at:plan_ts.index(";", at)]
        assert line.index("fromCron") < line.index("says"), line


# ── the change route: a moved schedule's words and its cron ──────────────────────────────────────────────────────

def _move(cron: str, says: str) -> list[dict]:
    return [{"op": "set_schedule", "target": {"kind": "schedule", "name": "Morning brief"}, "says": says,
             "fields": {"cron_expression": cron}}]


class TestTheChange:
    async def test_a_move_whose_words_and_cron_disagree_goes_back_once(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        out = await _call("submit_change", {"ops": _move("30 1 * * *", "Move it to 7am")}, run)
        assert "own clock" in out["error"] and "7:00" in out["error"] and "01:30" in out["error"], out
        again = await _call("submit_change", {"ops": _move("30 1 * * *", "Move it to 7am")}, run)
        assert again.get("finish") == "submit_change", again

    async def test_a_move_whose_words_and_cron_agree_goes_on(self, client):
        run = await _run(person_timezone="Europe/Lisbon")
        out = await _call("submit_change", {"ops": _move("0 9 * * *", "Move it to 9am")}, run)
        assert out.get("finish") == "submit_change", out

    def test_the_change_line_says_the_crons_time(self):
        from core.native_builder import change_ops as C
        from core.native_builder.change_tools import _words

        line = _words(C.OPS["set_schedule"], {"name": "Morning brief"}, {"cron_expression": "0 7 * * *"}, "",
                      {"cron_expression": "0 7 * * *", "schedule_type": "cron", "timezone": "Europe/Lisbon"})
        assert "Daily 07:00 Lisbon time" in line, line


# ── what the model reads back, and the build's summary ──────────────────────────────────────────────────────────

class TestWhatIsReadBack:
    def test_the_schedules_reader_says_each_schedules_own_clock(self):
        from core import builder_readers

        kolkata = {"schedule_type": "cron", "cron_expression": "0 7 * * *", "timezone": "Asia/Kolkata"}
        assert builder_readers._when(kolkata) == 'Daily 07:00 Kolkata time (cron "0 7 * * *")'
        assert builder_readers._when({"schedule_type": "cron", "cron_expression": "0 9 * * 1"}) == \
            'Mondays 09:00 UTC (cron "0 9 * * 1")'
        assert "Times are UTC." not in inspect.getsource(builder_readers)

    def test_the_builds_summary_says_its_own_clock(self):
        from core.orchestration import run_writes

        state: dict = {}
        run_writes.record(state, {"kind": "schedule", "action": "created", "id": "s1", "name": "Morning brief",
                                  "resource": {"enabled": True, "cron_expression": "0 7 * * *",
                                               "timezone": "Asia/Kolkata"}})
        text = state[run_writes.TEXT_KEY]
        assert "Daily 07:00 Kolkata time" in text and "(UTC)" not in text, text


_UTC_TIMES = re.compile(r"\bin UTC\b|\(UTC\)|Times are UTC|times are in UTC|evaluated in \*\*UTC")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


@pytest.mark.parametrize("name", ["help_topics.json", "docs_topics.json"])
def test_no_help_or_docs_line_says_a_schedules_time_is_utc(name):
    data = json.loads((ROOT / "backend/core/native_builder" / name).read_text())
    said = [s[:160] for s in _strings(data) if re.search(r"(?i)schedul|cron", s) and _UTC_TIMES.search(s)]
    assert said == [], said


# ── the send-back's record is the engine's: no public read shows it (CI2 on FF20 465d0488) ───────────────────────

class TestTheRecordIsTheEngines:
    def test_the_key_is_one_both_routes_write(self):
        from core.native_builder import build_plan, change_tools

        assert build_plan.SCHEDULE_SENT_KEY == change_tools._SCHEDULE_SENT == "_schedule_clock_sent_back"

    def test_no_public_read_shows_it(self):
        from types import SimpleNamespace

        from core.events_public import public_event, public_state
        from core.routes.orchestrations import _public_run_view

        state = {"_schedule_clock_sent_back": True, "order": "A-1"}
        assert public_state(state) == {"order": "A-1"}
        shown = public_event({"type": "orchestration_complete", "run_id": "r1", "status": "completed",
                              "final_state": state})
        assert "_schedule_clock_sent_back" not in json.dumps(shown), shown

        class _Run(SimpleNamespace):
            def model_dump(self, exclude=None):
                return {"run_id": "r1", "status": "completed", "shared_state": dict(state), "step_history": []}

        view = _public_run_view(_Run(status="completed", step_history=[], step_progress={}))
        assert view["shared_state"] == {"order": "A-1"}, view["shared_state"]


# ── no timezone is UTC, as every 1.x schedule is (REL3) ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz", [None, "", "UTC"])
def test_a_stored_schedule_with_no_clock_runs_on_utc(tz):
    from core.scheduler import compute_next_run

    weekly_hello = {"schedule_type": "cron", "cron_expression": "0 9 * * 1",
                    **({"timezone": tz} if tz is not None else {})}
    assert compute_next_run(weekly_hello, datetime(2026, 11, 5, 12, tzinfo=timezone.utc)) == \
        datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
