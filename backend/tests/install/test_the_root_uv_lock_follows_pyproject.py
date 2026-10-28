"""The repo root's uv.lock is pyproject.toml's, kept current (F1172).

`uv sync` and `uv run` at the repo root install from uv.lock, and it had not been re-locked since before Knowledge's
and telemetry's extras: it still held crawl4ai, which F1157 removed. This compares what the lock records as the
project's own requirements with what pyproject.toml asks for, offline; `lock-deps.sh --check` runs
`uv lock --check` as well, for the resolution itself.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[3]


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _asked() -> set[tuple]:
    """pyproject.toml's requirements: (name, extras, specifier, the extra that asks for it)."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    groups = [(None, project.get("dependencies", []))] + list(project.get("optional-dependencies", {}).items())
    out = set()
    for extra, requirements in groups:
        for line in requirements:
            req = Requirement(line)
            out.add((_key(req.name), tuple(sorted(req.extras)), str(req.specifier) or None, extra))
    return out


def _locked() -> set[tuple]:
    """What uv.lock records the project as requiring."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    name = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    (package,) = [p for p in lock["package"] if _key(p["name"]) == _key(name)]
    out = set()
    for dep in package["metadata"]["requires-dist"]:
        marker = re.fullmatch(r"extra == '([^']+)'", dep.get("marker", ""))
        specifier = str(Requirement(f"x{dep['specifier']}").specifier) if dep.get("specifier") else None
        out.add((_key(dep["name"]), tuple(sorted(dep.get("extras", []))), specifier,
                 marker.group(1) if marker else None))
    return out


def test_the_lock_records_what_pyproject_asks_for():
    asked, locked = _asked(), _locked()
    assert locked == asked, {"in pyproject, not in uv.lock": sorted(asked - locked, key=str),
                             "in uv.lock, not in pyproject": sorted(locked - asked, key=str)}


def test_the_lock_holds_no_crawl4ai():
    names = {_key(p["name"]) for p in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]}
    assert "crawl4ai" not in names
