"""F1451 (Naveen, 11-07): a schedule an agent or an MCP client makes is turned on from the chat, on the person's yes.

A schedule made by a tool starts paused (F155, unchanged): nothing runs until a person has read what each run starts
with. Its reply sent them to the Schedules screen even though a tool turns it on. Now:

* `create_schedule`'s reply says what to show the person (the message each run starts with, when it would first run)
  and that their yes turns it on with `turn_on_schedule`.
* Over MCP, `turn_on_schedule` is marked so a client asks its person first; `pause_schedule` is not. The on/off
  `set_schedule_enabled` is the app's own (Synapse AI's Turn on card), never served, since MCP marks a tool, not an
  argument.
* Turning one on works out its next run from now, as the Schedules screen does: one made paused at nine for half past,
  turned on at eleven, kept a next run in the past.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core import builder_access, feature_census
from core.builder_tools import execute_builder_tool
from core.mcp_server import catalog
from core.product_tools import build
from core.store import schedules as schedule_store


@pytest.fixture
def server():
    return types.SimpleNamespace(agent_sessions={})


async def call(tool: str, args: dict, server) -> dict:
    return json.loads(await execute_builder_tool(tool, args, server))


PAST = "2020-01-01T09:30:00Z"


async def _made(server, **more) -> dict:
    await S.seed_orchestrations([S.make_orchestration(id="orch_brief", name="Brief")])
    made = await call("create_schedule", {
        "name": "Mondays", "target_type": "orchestration", "target_id": "orch_brief",
        "prompt": "Brightside, Kestrel and Lumen's pricing pages", "schedule_type": "cron",
        "cron_expression": "0 9 * * 1", "timezone": "Europe/Amsterdam", **more}, server)
    assert made.get("status") == "created", made
    return made


async def _stale(schedule_id: str) -> None:
    """Its next run as it stands after a while paused: in the past."""
    row = await schedule_store.get(schedule_id)
    await schedule_store.save({**row, "next_run_at": PAST})


class TestCreatingOne:
    async def test_it_is_still_made_paused_and_says_what_to_show_and_how_to_turn_it_on(self, server):
        made = await _made(server, enabled=True)
        assert made["enabled"] is False and (await schedule_store.get(made["id"]))["enabled"] is False
        assert made["prompt"] == "Brightside, Kestrel and Lumen's pricing pages"
        assert made["timezone"] == "Europe/Amsterdam"
        note = made["note"]
        assert "turn_on_schedule" in note and made["next_run_at"] in note and made["prompt"] in note, note
        assert "under Schedules" not in note, note


class TestTurningItOn:
    async def test_turning_it_on_works_out_its_next_run_from_now(self, server):
        made = await _made(server)
        await _stale(made["id"])
        on = await call("turn_on_schedule", {"schedule_id": made["id"]}, server)
        row = await schedule_store.get(made["id"])
        assert row["enabled"] is True and row["next_run_at"] > "2026-01-01", row
        assert on["enabled"] is True and on["next_run_at"] == row["next_run_at"]
        assert on["prompt"] == made["prompt"], "what it starts with, said as it is turned on"

    async def test_one_already_on_is_said_and_nothing_is_written(self, server):
        made = await _made(server)
        await call("turn_on_schedule", {"schedule_id": made["id"]}, server)
        heard = []

        async def observer(item):
            heard.append(item["id"])

        schedule_store.set_saved_observer(observer)
        try:
            again = await call("turn_on_schedule", {"schedule_id": made["id"]}, server)
        finally:
            schedule_store.set_saved_observer(None)
        assert again["enabled"] is True and "already on" in again["note"] and heard == []

    async def test_pausing_turns_it_off(self, server):
        made = await _made(server)
        await call("turn_on_schedule", {"schedule_id": made["id"]}, server)
        off = await call("pause_schedule", {"schedule_id": made["id"]}, server)
        assert off["enabled"] is False and (await schedule_store.get(made["id"]))["enabled"] is False

    async def test_an_unknown_schedule_is_refused(self, server):
        for tool in ("turn_on_schedule", "pause_schedule"):
            assert "error" in await call(tool, {"schedule_id": "sched_nope"}, server)

    async def test_the_apps_own_switch_works_out_its_next_run_too(self, server):
        made = await _made(server)
        await _stale(made["id"])
        await call("set_schedule_enabled", {"schedule_id": made["id"], "enabled": True}, server)
        assert (await schedule_store.get(made["id"]))["next_run_at"] > "2026-01-01"


class TestOverMcp:
    @pytest.fixture
    def marks(self):
        return {t.name: t.annotations for t in catalog.builder_tools()}

    def test_turning_on_asks_first_and_pausing_does_not(self, marks):
        assert marks["turn_on_schedule"].destructiveHint is True
        assert marks["pause_schedule"].destructiveHint is False and marks["pause_schedule"].idempotentHint is True

    def test_the_on_off_switch_is_the_apps_own(self):
        assert "set_schedule_enabled" not in catalog.SERVED_BUILDER_TOOLS
        assert "set_schedule_enabled" in build.APP_ONLY
        assert {"turn_on_schedule", "pause_schedule"} <= catalog.SERVED_BUILDER_TOOLS

    def test_an_agent_may_name_each_and_the_census_says_them(self):
        assert {"turn_on_schedule", "pause_schedule", "set_schedule_enabled"} <= builder_access.ANYWHERE
        [row] = [s for s in feature_census.current() if s.id == "schedules"]
        assert {"turn_on_schedule", "pause_schedule"} <= set(row.tools)
