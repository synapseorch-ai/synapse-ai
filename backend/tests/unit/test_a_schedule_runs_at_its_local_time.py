"""
A schedule runs at its person's local time, across the clocks changing (F948, SAI2-P3, D300; Naveen 10-23).

Measured (SAI2-P3's after-batch, bld-05 on Claude Code Haiku): the card said "Every Friday at 4:00 PM Eastern Time",
and the schedule ran at 16:00 UTC, which is noon in New York; the scheduler ran every cron in UTC, and a schedule had no
timezone. Naveen: no timezone picker per schedule; people mean their own timezone, unless they name one, and the
schedule must understand that.

A schedule's definition now holds `timezone` (a name like America/New_York; none is UTC, as every schedule before
this was). `compute_next_run` reads its cron on that clock and answers in UTC, so OSS's tick and cloud's armed job
(which compute through it) both fire at the local time, summer and winter. The clocks' two awkward hours, by one rule:
a time that does not exist that day (2:30 when the clocks jump from 2:00 to 3:00) runs at the first minute that
does; a time that happens twice (1:30 when they fall back) runs once, the first time.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool
from core.models_schedule import ScheduleCreate, ScheduleInvalid, ScheduleUpdate, validate_timing
from core.scheduler import compute_next_run
from core.store import schedules as schedule_store


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _cron(expr: str, tz: str | None = None) -> dict:
    return {"schedule_type": "cron", "cron_expression": expr, **({"timezone": tz} if tz is not None else {})}


@pytest.mark.parametrize("expr, tz, start, want", [
    # bld-05: 4 PM in New York, in summer time and in winter time.
    ("0 16 * * 5", "America/New_York", _utc(2026, 10, 23, 12), _utc(2026, 10, 23, 20)),
    ("0 16 * * 5", "America/New_York", _utc(2026, 11, 3, 12), _utc(2026, 11, 6, 21)),
    # The day it moves: Friday 10 PM in New York is Saturday in UTC.
    ("0 22 * * 5", "America/New_York", _utc(2026, 10, 23, 12), _utc(2026, 10, 24, 2)),
    # Ahead of UTC, and a half-hour offset.
    ("0 7 * * 1", "Asia/Tokyo", _utc(2026, 10, 23, 12), _utc(2026, 10, 25, 22)),
    ("0 9 * * *", "Asia/Kolkata", _utc(2026, 10, 23, 12), _utc(2026, 10, 24, 3, 30)),
    # No timezone, or UTC: as every schedule ran before.
    ("0 16 * * 5", None, _utc(2026, 10, 23, 12), _utc(2026, 10, 23, 16)),
    ("0 16 * * 5", "UTC", _utc(2026, 10, 23, 12), _utc(2026, 10, 23, 16)),
    ("0 16 * * 5", "", _utc(2026, 10, 23, 12), _utc(2026, 10, 23, 16)),
])
def test_the_next_run_is_the_local_time_in_utc(expr, tz, start, want):
    assert compute_next_run(_cron(expr, tz), start) == want


def test_it_keeps_its_local_time_across_the_change_of_the_clocks():
    """Every day at 9 in Amsterdam: 07:00 UTC in summer, 08:00 UTC from the night the clocks go back (25 Oct 2026)."""
    schedule, at, runs = _cron("0 9 * * *", "Europe/Amsterdam"), _utc(2026, 10, 23, 12), []
    for _ in range(4):
        at = compute_next_run(schedule, at)
        runs.append(at)
    assert runs == [_utc(2026, 10, 24, 7), _utc(2026, 10, 25, 8), _utc(2026, 10, 26, 8), _utc(2026, 10, 27, 8)]


def test_a_time_the_clocks_skip_runs_at_the_first_minute_that_exists():
    """2:30 on 14 March 2027 does not exist in New York (2:00 → 3:00): it runs at 3:00 EDT, and the next day at 2:30."""
    schedule = _cron("30 2 * * *", "America/New_York")
    first = compute_next_run(schedule, _utc(2027, 3, 13, 12))
    assert first == _utc(2027, 3, 14, 7)
    assert compute_next_run(schedule, first) == _utc(2027, 3, 15, 6, 30)


def test_a_time_the_clocks_repeat_runs_once():
    """1:30 on 1 November 2026 happens twice in New York (EDT, then EST): it runs the first time only."""
    schedule = _cron("30 1 * * *", "America/New_York")
    first = compute_next_run(schedule, _utc(2026, 10, 31, 12))
    assert first == _utc(2026, 11, 1, 5, 30)
    assert compute_next_run(schedule, first) == _utc(2026, 11, 2, 6, 30)


def test_an_interval_schedule_has_no_clock():
    every = {"schedule_type": "interval", "interval_value": 2, "interval_unit": "hours", "timezone": "Asia/Tokyo"}
    assert compute_next_run(every, _utc(2026, 10, 23, 12)) == _utc(2026, 10, 23, 14)


def test_the_models_keep_the_timezone_and_refuse_one_that_is_not():
    body = {"name": "n", "target_type": "orchestration", "target_id": "o", "prompt": "p", "schedule_type": "cron",
            "cron_expression": "0 9 * * *", "timezone": "Europe/Amsterdam"}
    assert ScheduleCreate.model_validate(body).model_dump()["timezone"] == "Europe/Amsterdam"
    assert ScheduleUpdate.model_validate({"timezone": "Asia/Tokyo"}).timezone == "Asia/Tokyo"
    with pytest.raises(ScheduleInvalid, match="not a place's timezone"):
        validate_timing({**body, "timezone": "Mars/Olympus"})
    validate_timing({**body, "timezone": None})


async def _call(tool: str, args: dict) -> dict:
    return json.loads(await execute_builder_tool(tool, args, SimpleNamespace(agent_sessions={})))


async def test_the_builders_schedule_tools_keep_and_move_its_timezone():
    await S.seed_orchestrations([S.make_orchestration(id="orch_report", name="Weekly report")])
    made = await _call("create_schedule", {
        "name": "Fridays", "target_type": "orchestration", "target_id": "orch_report", "prompt": "this week",
        "schedule_type": "cron", "cron_expression": "0 16 * * 5", "timezone": "America/New_York"})
    saved = await schedule_store.get(made["id"])
    assert saved["timezone"] == "America/New_York"
    assert saved["next_run_at"].endswith("T20:00:00Z") or saved["next_run_at"].endswith("T21:00:00Z")
    # Only its timezone moves: the next run is worked out again.
    moved = await _call("update_schedule", {"schedule_id": made["id"], "timezone": "UTC"})
    after = await schedule_store.get(made["id"])
    assert moved["changed"] == ["timezone"] and after["next_run_at"].endswith("T16:00:00Z")
    bad = await _call("update_schedule", {"schedule_id": made["id"], "timezone": "Mars/Olympus"})
    assert "not a place's timezone" in bad["error"]
    assert (await schedule_store.get(made["id"]))["timezone"] == "UTC"


def test_the_builders_tools_tell_the_model_what_the_timezone_is():
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    for name in ("create_schedule", "update_schedule"):
        [tool] = [t for t in BUILDER_TOOL_SCHEMAS if t["function"]["name"] == name]
        props = tool["function"]["parameters"]["properties"]
        assert "timezone" in props and "America/New_York" in props["timezone"]["description"]
        assert "in UTC" not in props["cron_expression"]["description"]


# ── the build route (Part B): the person's own clock unless they name one ────────────────────────────────────────

def test_the_panel_sends_the_persons_timezone_and_the_run_keeps_a_real_one():
    import inspect
    from pathlib import Path

    from core.routes import builder

    assert builder.BuilderChatRequest(message="m", timezone="Europe/Amsterdam").timezone == "Europe/Amsterdam"
    assert builder._person_timezone("Europe/Amsterdam") == "Europe/Amsterdam"
    assert builder._person_timezone("Mars/Olympus") is None and builder._person_timezone(None) is None
    assert '"person_timezone": _person_timezone(request.timezone)' in inspect.getsource(builder.prepare_builder_run)
    # Each Synapse AI chat sends its own turns (TABS2): the body is built in the chat's store.
    panel = (Path(__file__).resolve().parents[3] / "frontend/src/components/app/builderChats.ts").read_text()
    assert "timezone: Intl.DateTimeFormat().resolvedOptions().timeZone" in panel


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _weekly(**schedule):
    return {"finish": "submit_plan", "kind": "create", "title": "Weekly report", "summary": "Writes the report.",
            "steps": [{"id": "write", "type": "llm", "name": "Write it", "does": "Writes the weekly report",
                       "prompt": "Write this week's report."}],
            "schedule": {"cron": "0 16 * * 5", "says": "Every Friday at 4 PM", **schedule}}


async def _checked(draft, **state):
    from core import builder_access
    from core.builder_tools import execute_builder_tool_with_events
    from core.models_orchestration import OrchestrationRun
    from core.native_builder import build_plan
    from core.orchestration.engine import live
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_tz", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": draft}, _Server(), run_id=run.run_id)
    builder_access.forget_runs()
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_a_schedule_with_no_timezone_named_runs_on_the_persons_clock(client):
    out = await _checked(_weekly(), person_timezone="Europe/Amsterdam")
    assert out["outcome"] == "review" and out["plan"]["schedule"]["timezone"] == "Europe/Amsterdam"
    assert "(`0 16 * * 5`, Amsterdam time)" in out["review"]


async def test_a_timezone_the_person_named_wins(client):
    out = await _checked(_weekly(timezone="America/New_York"), person_timezone="Europe/Amsterdam")
    assert out["plan"]["schedule"]["timezone"] == "America/New_York"
    assert "New York time" in out["review"] and "Amsterdam" not in out["review"]


async def test_a_timezone_that_is_not_one_goes_back_to_the_planner(client):
    out = await _checked(_weekly(timezone="Eastern"), person_timezone="Europe/Amsterdam")
    assert "build" not in out["offers"]
    assert any("Eastern" in i and "America/New_York" in i for i in out["issues"])


def test_the_saved_schedule_carries_its_timezone():
    from core.native_builder import build_save
    from core.native_builder.compile import compile_plan
    from core.native_builder.objects import Plan

    plan = Plan.from_draft({k: v for k, v in _weekly(timezone="America/New_York").items() if k != "finish"})
    assert compile_plan(plan).schedule["timezone"] == "America/New_York"
    assert "timezone" in build_save._SCHEDULE_ARGS


def test_the_card_says_the_schedules_clock_in_the_reviews_words():
    from pathlib import Path

    plan_ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/plan.ts").read_text()
    assert "zoneWords(view.plan.schedule?.timezone)" in plan_ts


async def test_the_planner_reads_the_persons_clock_and_render_and_the_lists_say_it_alike(client):
    from pathlib import Path

    from core.native_builder.render import zone_words

    pack = await _pack(person_timezone="Asia/Kolkata")
    assert pack["person_timezone"] == "Asia/Kolkata"
    assert (await _pack())["person_timezone"] == "UTC"
    assert zone_words("America/New_York") == "New York time" and zone_words(None) == zone_words("UTC") == "UTC"
    lists = (Path(__file__).resolve().parents[3] / "frontend/src/components/run/workflowList.ts").read_text()
    assert "return `${tz.split('/').pop()!.replace(/_/g, ' ')} time`;" in lists


async def _pack(**state):
    from core import builder_access
    from core.builder_tools import execute_builder_tool_with_events
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import live
    from core.orchestration.state import SharedState

    run = OrchestrationRun(run_id="builder_pack", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("read_plan_context", {}, _Server(), run_id=run.run_id)
    builder_access.forget_runs()
    return json.loads(raw) if isinstance(raw, str) else raw


# ── the change route: "move it to 9:00" is on the schedule's clock, else the person's ─────────────────────────────

async def _change(ops, **state):
    from core import builder_access
    from core.builder_tools import execute_builder_tool_with_events
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.engine import live
    from core.orchestration.state import SharedState
    from core.product_tools import hooks
    from core.product_tools.roles import Role

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    run = OrchestrationRun(run_id="builder_chg_tz", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, **state})
    await SharedState(run).checkpoint()
    try:
        with live(run):
            raw, _ = await execute_builder_tool_with_events(
                "check_change", {"draft": {"finish": "submit_change", "ops": ops}}, _Server(), run_id=run.run_id)
    finally:
        hooks.set_role_source(None)
        builder_access.forget_runs()
    return json.loads(raw)


async def _nightly(**kw):
    item = {"id": "sched_n", "name": "Every night at 2", "enabled": True, "target_type": "orchestration",
            "target_id": "orch_x", "prompt": "Go", "schedule_type": "cron", "cron_expression": "0 2 * * *", **kw}
    await schedule_store.save(item)


MOVE = [{"op": "set_schedule", "target": {"kind": "schedule", "id": "sched_n"}, "fields": {"cron_expression": "0 9 * * *"}}]


async def test_a_moved_schedule_with_no_clock_takes_the_persons(client):
    await _nightly()
    out = await _change(MOVE, person_timezone="Europe/Amsterdam")
    [[tool, args]] = out["calls"]
    assert tool == "update_schedule" and args["timezone"] == "Europe/Amsterdam"


async def test_a_moved_schedule_keeps_its_own_clock(client):
    await _nightly(timezone="America/New_York")
    out = await _change(MOVE, person_timezone="Europe/Amsterdam")
    [[_, args]] = out["calls"]
    assert "timezone" not in args or args["timezone"] == "America/New_York"


async def test_a_clock_the_person_names_for_the_move_wins(client):
    await _nightly(timezone="America/New_York")
    named = [{**MOVE[0], "fields": {"cron_expression": "0 9 * * *", "timezone": "Asia/Tokyo"}}]
    out = await _change(named, person_timezone="Europe/Amsterdam")
    [[_, args]] = out["calls"]
    assert args["timezone"] == "Asia/Tokyo"


def test_an_hourly_schedule_runs_through_the_repeated_hour():
    """SAI2's re-check: "every hour" in New York on 1 November 2026 runs at 1:00 EDT, 1:00 EST and 2:00 EST."""
    schedule, at, runs = _cron("0 * * * *", "America/New_York"), _utc(2026, 11, 1, 4, 30), []
    for _ in range(4):
        at = compute_next_run(schedule, at)
        runs.append(at)
    assert runs == [_utc(2026, 11, 1, 5), _utc(2026, 11, 1, 6), _utc(2026, 11, 1, 7), _utc(2026, 11, 1, 8)]
    every5 = _cron("*/30 * * * *", "America/New_York")
    assert compute_next_run(every5, _utc(2026, 11, 1, 5, 40)) == _utc(2026, 11, 1, 6)


@pytest.mark.parametrize("name", ["EST", "MST", "HST", "Etc/GMT+5", "Eastern", "GMT"])
def test_an_abbreviation_or_a_fixed_offset_is_not_a_place(name):
    """SAI2's re-check, 4: "EST" is a zone with no summer time, so Eastern would run an hour late all summer."""
    with pytest.raises(ScheduleInvalid, match="write the place, like America/New_York"):
        validate_timing({**_cron("0 9 * * *", name)})


def test_utc_in_any_case_is_utc():
    validate_timing(_cron("0 9 * * *", "utc"))
    assert compute_next_run(_cron("0 9 * * *", "utc"), _utc(2026, 10, 23, 12)) == _utc(2026, 10, 24, 9)
