"""F1563: a schedule's timezone is read even where the computer has no timezone database.

Naveen (2026-11-12) could not save a schedule: "'Asia/Calcutta' is not a place's timezone". His browser names India's
zone by its older IANA name, which every timezone database still carries. The official Docker image
(synapseorchai/synapse-ai:2.0.5, python:*-slim) has no system database at /usr/share/zoneinfo and did not install the
`tzdata` package, so `ZoneInfo` found no zone at all there: every schedule in a place's local time was refused, and a
local-time cron could not compute its next run. Installs that worked only had `tzdata` by chance (on Windows, through
pandas and psycopg). `tzdata` is now a dependency of its own, so the zone is read from the package wherever the system
has none.

Each check runs in a child Python with `PYTHONTZPATH` empty, which is how such a computer looks to `zoneinfo`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent


def _without_a_system_database(code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONTZPATH="")
    return subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120
    )


@pytest.mark.parametrize("zone", ["Asia/Calcutta", "Asia/Kolkata", "America/New_York", "Europe/Kiev", "UTC"])
def test_a_browsers_timezone_is_a_place_with_no_system_database(zone: str) -> None:
    done = _without_a_system_database(
        f"from core.models_schedule import _is_timezone; print(_is_timezone({zone!r}))"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True", f"{zone} refused with no system timezone database"


def test_a_schedule_in_local_time_saves_and_finds_its_next_run_with_no_system_database() -> None:
    done = _without_a_system_database(
        "from core.models_schedule import validate_timing\n"
        "validate_timing({'timezone': 'Asia/Calcutta', 'schedule_type': 'interval',"
        " 'interval_value': 2, 'interval_unit': 'hours'})\n"
        "validate_timing({'timezone': 'Asia/Calcutta', 'schedule_type': 'cron', 'cron_expression': '30 9 * * 1-5'})\n"
        "print('saved')\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "saved"


def test_tzdata_is_a_dependency_of_its_own() -> None:
    """Not only a pin, and not only on Windows through another package."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {d.split(";")[0].split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip().lower()
             for d in project["dependencies"]}
    assert "tzdata" in names
    entry = next(d for d in project["dependencies"] if d.lower().startswith("tzdata"))
    assert ";" not in entry, "tzdata is needed on every platform, not behind a marker"
