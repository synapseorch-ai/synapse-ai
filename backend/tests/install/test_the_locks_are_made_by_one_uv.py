"""The backend's locks are made, and checked, by one version of uv (F1170).

uv writes the same pins with differently ordered markers from one version to the next: the fix-forward 6 gate's
uv 0.13.6 wrote colorama's marker one way and the laptop's 0.11.8 the other, so `lock-deps.sh --check` called an
unchanged lock stale. lock-deps.sh names the uv it needs and refuses any other, and every CI job that runs it sets
up that uv.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "lock-deps.sh"


def _pinned() -> str:
    found = re.search(r'^UV_VERSION="([0-9.]+)"$', SCRIPT.read_text(encoding="utf-8"), re.M)
    assert found, "lock-deps.sh names the uv it needs"
    return found.group(1)


def test_the_script_refuses_another_uv():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'uv --version.*\n(.*\n){0,4}.*"\$UV_VERSION"', text), "lock-deps.sh compares uv's version"


def _jobs_running_the_script():
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for job in re.split(r"\n  (?=[A-Za-z0-9_-]+:\n)", workflow.read_text(encoding="utf-8")):
            if "lock-deps.sh" in job:
                yield workflow.name, job


def test_some_ci_job_runs_the_script():
    assert list(_jobs_running_the_script())


@pytest.mark.parametrize("workflow,job", list(_jobs_running_the_script()),
                         ids=lambda v: v if v.endswith(".yml") else v.split(":", 1)[0].strip())
def test_every_ci_job_that_runs_it_sets_up_that_uv(workflow, job):
    setup = re.search(r"uses: astral-sh/setup-uv@\S+\n((?:\s+.*\n){0,4})", job)
    assert setup, f"{workflow}: a job running lock-deps.sh sets up uv"
    version = re.search(r'version:\s*"?([0-9.]+)"?', setup.group(1))
    assert version and version.group(1) == _pinned(), (
        f"{workflow}: setup-uv must pin uv {_pinned()}, the version lock-deps.sh requires")
