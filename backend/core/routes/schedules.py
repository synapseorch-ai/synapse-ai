"""
Schedules API: CRUD for schedule definitions and manual trigger.
"""
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from core.models_schedule import ScheduleCreate, ScheduleInvalid, ScheduleUpdate, validate_timing
from core.scheduler import compute_next_run, _utc_now, _iso
from core.store import schedules as schedule_store

router = APIRouter()


def _get_manager(request: Request):
    mgr = getattr(request.app.state, "schedule_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Schedule manager not available")
    return mgr


# -- Checks (F197) -------------------------------------------------------
# The answers are cloud's (`api/control/routes/schedules.py`), so a v1 request
# gets one answer on both products: 422 for timing it cannot run, 404 for a
# target that is not there. Each `detail` is a sentence — the form toasts it.

_TIMING_KEYS = {"schedule_type", "interval_value", "interval_unit", "cron_expression"}
_TARGET_KEYS = {"target_type", "target_id"}


def _check_timing(schedule: dict) -> None:
    try:
        validate_timing(schedule)
    except ScheduleInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _check_target(schedule: dict) -> None:
    """The agent or orchestration must be the workspace's own.

    A schedule whose target does not exist fires into nothing for ever. The AI
    builder's own agents and orchestration are not the workspace's, so they are
    not there either — the builder's `create_schedule` has refused both since
    D115.
    """
    from core.builder_tools import _is_visible
    from core.store.resources import get_agent, get_orchestration

    kind = schedule.get("target_type")
    target_id = schedule.get("target_id") or ""
    load = get_agent if kind == "agent" else get_orchestration
    found = await load(target_id) if target_id else None
    if found is None or not _is_visible(found):
        raise HTTPException(status_code=404, detail=f"No such {kind}: {target_id}.")


# -- List ----------------------------------------------------------------

@router.get("/api/schedules")
async def list_schedules():
    """Return all schedules."""
    return await schedule_store.load()


# -- Create --------------------------------------------------------------

@router.post("/api/schedules")
async def create_schedule(body: ScheduleCreate, request: Request):
    """Create a new schedule. Server computes next_run_at."""
    _get_manager(request)

    schedule = body.model_dump()
    _check_timing(schedule)
    await _check_target(schedule)
    schedule["id"] = f"sched_{uuid.uuid4().hex[:8]}"
    schedule["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    schedule["last_run_at"] = None

    now = _utc_now()
    schedule["next_run_at"] = _iso(compute_next_run(schedule, now))

    await schedule_store.save(schedule)
    return schedule


# -- Get one -------------------------------------------------------------

@router.get("/api/schedules/{schedule_id}")
async def get_schedule(schedule_id: str):
    """Return a single schedule."""
    s = await schedule_store.get(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return s


# -- Full update ----------------------------------------------------------

@router.put("/api/schedules/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleCreate, request: Request):
    """Full replacement update. Server recomputes next_run_at."""
    _get_manager(request)

    existing = await schedule_store.get(schedule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updated = body.model_dump()
    _check_timing(updated)
    await _check_target(updated)
    updated["id"] = schedule_id
    updated["created_at"] = existing.get("created_at", "")
    updated["last_run_at"] = existing.get("last_run_at")

    now = _utc_now()
    updated["next_run_at"] = _iso(compute_next_run(updated, now))

    await schedule_store.save(updated)
    return updated


# -- Partial update (enable/disable, field patch) -------------------------

@router.patch("/api/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, body: ScheduleUpdate, request: Request):
    """Partial update. If re-enabling, recomputes next_run_at from now.

    Checked as a whole only where the patch reaches: its timing, its target, or
    turning it on. Switching a schedule off is never refused, so one saved
    before the checks (F197) can always be stopped.
    """
    _get_manager(request)

    s = await schedule_store.get(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")

    patch = body.model_dump(exclude_none=True)

    was_disabled = not s.get("enabled", True)
    re_enabling = patch.get("enabled") is True and was_disabled

    patched = {**s, **patch}
    retimed = bool(_TIMING_KEYS.intersection(patch))
    if retimed or re_enabling:
        _check_timing(patched)
    if _TARGET_KEYS.intersection(patch):
        await _check_target(patched)
    s = patched

    # If the schedule is being re-enabled or schedule timing changed, recalculate next_run_at
    if re_enabling or retimed:
        now = _utc_now()
        s["next_run_at"] = _iso(compute_next_run(s, now))

    await schedule_store.save(s)
    return s


# -- Delete ---------------------------------------------------------------

@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str):
    """Delete a schedule."""
    if not await schedule_store.delete_one(schedule_id):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "deleted", "id": schedule_id}


# -- Manual trigger --------------------------------------------------------

@router.post("/api/schedules/{schedule_id}/run")
async def run_schedule_now(schedule_id: str, request: Request):
    """Run a schedule now, as a test (fire-and-forget). Returns the run's id.

    Runs a disabled schedule too, and does not move its next run (F72).
    """
    mgr = _get_manager(request)

    run_id = await mgr.trigger_now(schedule_id)
    if run_id is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return {"status": "triggered", "schedule_id": schedule_id, "run_id": run_id}
