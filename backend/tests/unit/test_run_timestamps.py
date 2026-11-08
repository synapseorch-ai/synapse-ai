"""A run's timestamps leave the store saying which zone they are in.

The engine stamps `started_at` in UTC with a trailing `Z`. SQLite has no
timezone type, so SQLAlchemy stores the value and hands back a *naive*
datetime; `isoformat()` on that yields "2026-09-16T04:16:57" — no offset, no
`Z`. A browser parses an offset-less string as **local** time, so a run a
minute old renders as hours old for anyone east or west of UTC. Measured in a
browser at UTC+05:30: a fifteen-minute-old run read "5h".

The fix is at the boundary the zone is lost at, not at every reader.
"""
import datetime

import pytest

from core.models_orchestration import OrchestrationRun


def _run(run_id="run_tz", started="2026-09-16T04:16:57Z", ended=None):
    return OrchestrationRun(
        run_id=run_id,
        orchestration_id="orch_tz",
        status="completed",
        started_at=started,
        ended_at=ended,
    )


def _assert_utc(label: str, value: str | None):
    assert value, f"{label} is missing"
    assert value.endswith("Z") or "+00:00" in value, (
        f"{label} came back as {value!r} — no zone, so a browser reads it as local time"
    )
    # And it must still parse to the instant that went in.
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.astimezone(datetime.timezone.utc).hour == 4


@pytest.mark.asyncio
async def test_a_listed_run_says_which_zone_its_times_are_in():
    from core.orchestration.state import SharedState

    state = SharedState(_run())
    await state.checkpoint()

    runs = await SharedState.list_runs(limit=10)
    listed = next(r for r in runs if r["run_id"] == "run_tz")
    _assert_utc("started_at", listed["started_at"])


@pytest.mark.asyncio
async def test_a_restored_run_says_which_zone_its_times_are_in():
    from core.orchestration.state import SharedState

    state = SharedState(_run(run_id="run_tz2", ended="2026-09-16T04:21:20Z"))
    await state.checkpoint()

    restored = (await SharedState.restore("run_tz2")).run
    _assert_utc("started_at", restored.started_at)
    _assert_utc("ended_at", restored.ended_at)
