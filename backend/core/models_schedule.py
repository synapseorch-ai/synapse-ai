"""
Pydantic models for the Schedules feature.
"""
from typing import Literal
from pydantic import BaseModel

#: The interval, in its unit — cloud's bounds (`api/control/routes/schedules.py`).
INTERVAL_MIN, INTERVAL_MAX = 1, 10_000


class ScheduleInvalid(ValueError):
    """A schedule whose timing cannot run; the message says why, in a sentence (F197)."""


def validate_timing(schedule: dict) -> None:
    """Refuse a schedule that cannot run, in cloud's words (F197).

    The API took an interval of 0 or -5 (a next run in the past, so an enabled
    one fired on every scheduler tick) and answered a cron it could not read
    with a 500. Only the fields its `schedule_type` reads are checked: the form
    clears the others, and a leftover cron on an interval schedule is never read.
    """
    if schedule.get("schedule_type") == "interval":
        value, unit = schedule.get("interval_value"), schedule.get("interval_unit")
        if value is None or not unit:
            raise ScheduleInvalid("An interval schedule needs a value and a unit.")
        if not INTERVAL_MIN <= value <= INTERVAL_MAX:
            raise ScheduleInvalid(f"An interval must be a whole number from {INTERVAL_MIN} to {INTERVAL_MAX:,}.")
        return
    if not (schedule.get("cron_expression") or "").strip():
        raise ScheduleInvalid("A cron schedule needs an expression.")
    from core.scheduler import _utc_now, compute_next_run
    try:
        compute_next_run(schedule, _utc_now())
    except Exception as exc:  # croniter raises a family of its own errors
        raise ScheduleInvalid(f"That is not a valid cron expression: {exc}") from exc


class Schedule(BaseModel):
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    created_at: str
    target_type: Literal["agent", "orchestration"]
    target_id: str
    prompt: str
    schedule_type: Literal["interval", "cron"]
    # Interval fields
    interval_value: int | None = None
    interval_unit: Literal["minutes", "hours", "days"] | None = None
    # Cron fields
    cron_expression: str | None = None
    missed_run_policy: Literal["run_immediately", "skip"] = "skip"
    # Server-computed state -- never trust client-provided values
    last_run_at: str | None = None
    next_run_at: str | None = None


class ScheduleCreate(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    target_type: Literal["agent", "orchestration"]
    target_id: str
    prompt: str
    schedule_type: Literal["interval", "cron"]
    # Interval fields
    interval_value: int | None = None
    interval_unit: Literal["minutes", "hours", "days"] | None = None
    # Cron fields
    cron_expression: str | None = None
    missed_run_policy: Literal["run_immediately", "skip"] = "skip"


class ScheduleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    target_type: Literal["agent", "orchestration"] | None = None
    target_id: str | None = None
    prompt: str | None = None
    schedule_type: Literal["interval", "cron"] | None = None
    interval_value: int | None = None
    interval_unit: Literal["minutes", "hours", "days"] | None = None
    cron_expression: str | None = None
    missed_run_policy: Literal["run_immediately", "skip"] | None = None
