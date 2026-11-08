"""
Pydantic models for the Schedules feature.
"""
from typing import Literal
from pydantic import BaseModel

#: The interval, in its unit — cloud's bounds (`api/control/routes/schedules.py`).
INTERVAL_MIN, INTERVAL_MAX = 1, 10_000


class ScheduleInvalid(ValueError):
    """A schedule whose timing cannot run; the message says why, in a sentence (F197)."""


def _is_timezone(name: str) -> bool:
    """A place's timezone ("America/New_York"), or UTC. Not an abbreviation or a fixed offset: "EST" is a zone with
    no summer time, an hour late all summer for Eastern, and `Etc/GMT+5`'s sign runs backwards (SAI2's re-check, 4)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    text = str(name).strip()
    if text.upper() == "UTC":
        return True
    if "/" not in text or text.split("/")[0] in ("Etc", "SystemV"):
        return False
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


#: What a name that is not a place's timezone is told.
TIMEZONE_HINT = "write the place, like America/New_York, or UTC"


def validate_timing(schedule: dict) -> None:
    """Refuse a schedule that cannot run, in cloud's words (F197).

    The API took an interval of 0 or -5 (a next run in the past, so an enabled
    one fired on every scheduler tick) and answered a cron it could not read
    with a 500. Only the fields its `schedule_type` reads are checked: the form
    clears the others, and a leftover cron on an interval schedule is never read.
    """
    if (tz := schedule.get("timezone")) and not _is_timezone(tz):
        raise ScheduleInvalid(f"'{tz}' is not a place's timezone: {TIMEZONE_HINT}.")
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
    #: The clock its cron is read on, as a name like America/New_York; none is UTC (F948, Naveen 10-23).
    timezone: str | None = None
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
    #: The clock its cron is read on, as a name like America/New_York; none is UTC (F948, Naveen 10-23).
    timezone: str | None = None
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
    timezone: str | None = None
    missed_run_policy: Literal["run_immediately", "skip"] | None = None
