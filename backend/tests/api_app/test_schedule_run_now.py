"""
Run Now runs the schedule, as a test, and names the run it started (F72).

It fired `_execute_schedule`, which returned at once for a disabled schedule —
so Run Now on a paused schedule said `triggered` and did nothing — and for an
enabled one it advanced `next_run_at`, so testing a schedule moved its next
real run. The `run_id` it returned was a guess built from the clock, not the
logger's, and led nowhere. Cloud's Run Now already runs regardless of enabled
and leaves the timetable alone; this matches it.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.store import schedules as schedule_store


@pytest.fixture
async def manager(test_app, monkeypatch):
    import core.react_engine as re_mod
    import core.schedule_logger as logger_mod
    from core.scheduler import ScheduleManager

    runs = {"prompts": [], "log_run_ids": []}

    async def step(**kw):
        runs["prompts"].append(kw["message"])
        runs["step_run_id"] = kw.get("run_id")
        yield {"type": "final", "response": "ran"}

    real = logger_mod.ScheduleLogger

    class Recording(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            runs["log_run_ids"].append(self.run_id)

    monkeypatch.setattr(re_mod, "run_agent_step", step)
    monkeypatch.setattr(logger_mod, "ScheduleLogger", Recording)
    mgr = ScheduleManager()
    mgr._server_module = test_app.state.server_module
    test_app.state.schedule_manager = mgr
    yield runs
    # The run is fire-and-forget; let it finish inside this test's event loop.
    await asyncio.gather(*mgr._manual_runs, return_exceptions=True)


async def _schedule(sid, enabled, next_run_at=None):
    await schedule_store.save({
        "id": sid, "name": "Nightly", "target_type": "agent", "target_id": "a1", "prompt": "do it",
        "enabled": enabled, "schedule_type": "interval", "interval_value": 5, "interval_unit": "minutes",
        "next_run_at": next_run_at,
    })


async def _ran(runs):
    for _ in range(100):
        if runs["prompts"]:
            return True
        await asyncio.sleep(0.02)
    return False


async def test_a_disabled_schedule_runs(client, manager):
    await _schedule("sched_off", enabled=False)

    resp = await client.post("/api/schedules/sched_off/run")

    assert resp.status_code == 200
    assert await _ran(manager)


async def test_the_run_id_is_the_runs(client, manager):
    await _schedule("sched_id", enabled=True)

    body = (await client.post("/api/schedules/sched_id/run")).json()
    await _ran(manager)

    assert manager["log_run_ids"] == [body["run_id"]]
    assert manager["step_run_id"] == body["run_id"]


async def test_the_timetable_is_left_alone(client, manager):
    later = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    await _schedule("sched_keep", enabled=True, next_run_at=later.isoformat())

    await client.post("/api/schedules/sched_keep/run")
    await _ran(manager)
    await asyncio.sleep(0.05)

    s = await schedule_store.get("sched_keep")
    assert datetime.fromisoformat(s["next_run_at"]) == later
    assert not s.get("last_run_at")
