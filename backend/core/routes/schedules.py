"""
Schedules API: CRUD for schedule definitions and manual trigger.
"""
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from core.models_schedule import ScheduleCreate, ScheduleUpdate
from core.scheduler import compute_next_run, _utc_now, _iso
from core.store import schedules as schedule_store

router = APIRouter()


def _get_manager(request: Request):
    mgr = getattr(request.app.state, "schedule_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="Schedule manager not available")
    return mgr


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
    """Partial update. If re-enabling, recomputes next_run_at from now."""
    _get_manager(request)

    s = await schedule_store.get(schedule_id)
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")

    patch = body.model_dump(exclude_none=True)

    was_disabled = not s.get("enabled", True)
    re_enabling = patch.get("enabled") is True and was_disabled

    # Apply patch fields
    for k, v in patch.items():
        s[k] = v

    # If the schedule is being re-enabled or schedule timing changed, recalculate next_run_at
    timing_keys = {"schedule_type", "interval_value", "interval_unit", "cron_expression"}
    if re_enabling or timing_keys.intersection(patch.keys()):
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
