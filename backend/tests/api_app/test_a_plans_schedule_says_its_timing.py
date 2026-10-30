"""D340 F (BLD1): a plan's schedule can run every so often, and says what it does with a time it missed.

A plan's schedule was cron only, and compile wrote "skip" for every missed time, though the Schedules screen sets
both. Now a plan's schedule names its type (cron or interval, with its value and unit) and its missed-run policy, by
the same names and values as the screen and the change ops, each held to the change route's one check of them
(`change_ops._field_issue`, F1267); compile saves them as said, and the card says what happens to a missed time.
"""
from __future__ import annotations

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.native_builder.build_save import save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_schedule", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


def _plan(**schedule) -> dict:
    return {"kind": "create", "title": "Queue check", "summary": "Checks the queue.",
            "steps": [{"id": "check", "type": "llm", "name": "Check it", "does": "Checks the queue",
                       "prompt": "Check the queue.", "next": "end"}],
            "schedule": {"says": "every 15 minutes", **schedule}}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


def _saved(found, run) -> dict:
    [call] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_schedule"]
    return call["args"]


class TestItsTiming:
    async def test_an_interval_and_a_missed_time_run_at_once_are_saved_as_said(self, client, run):
        found = await _examine(run, _plan(schedule_type="interval", interval_value=15, interval_unit="minutes",
                                          missed_run_policy="run_immediately"))
        assert found.errors == [], found.errors
        args = _saved(found, run)
        assert (args["schedule_type"], args["interval_value"], args["interval_unit"]) == ("interval", 15, "minutes")
        assert args["missed_run_policy"] == "run_immediately" and "cron_expression" not in args
        card = render(found.plan)
        assert "**When:** every 15 minutes (every 15 minutes)" in card
        assert "**If a time is missed** (the machine was off): it runs once as soon as it can" in card

    async def test_a_cron_schedule_skips_a_missed_time_as_before(self, client, run):
        found = await _examine(run, _plan(cron="*/15 * * * *"))
        args = _saved(found, run)
        assert (args["schedule_type"], args["cron_expression"], args["missed_run_policy"]) == (
            "cron", "*/15 * * * *", "skip")
        assert "it is skipped, and it runs at the next time" in render(found.plan)

    @pytest.mark.parametrize("schedule, said", [
        ({"cron": "0 7 * * *", "missed_run_policy": "catch_up"},
         "schedule.missed_run_policy: `missed_run_policy` is run_immediately or skip"),
        ({"schedule_type": "interval", "interval_value": 15},
         "schedule.interval_unit: `interval_unit` is minutes, hours or days"),
        ({"schedule_type": "interval", "interval_value": 0, "interval_unit": "hours"},
         "schedule.interval_value: `interval_value` is a whole number above 0"),
        ({"cron": "every morning"}, "schedule.cron: 'every morning' is not five fields"),
    ])
    async def test_what_the_change_route_refuses_goes_back_in_its_words(self, client, run, schedule, said):
        found = await _examine(run, _plan(**schedule))
        assert said in found.errors, found.errors
