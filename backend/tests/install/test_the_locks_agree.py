"""The backend's lock files agree with each other (F1160).

CI2 found requirements-worker.lock pinning wrapt 2.3.0 and requirements-telemetry.lock wrapt 2.5.0: each extra lock
was resolved against requirements.lock alone, so an install of the two together could not resolve, and
`lock-deps.sh --check` (each lock reproducible on its own) never saw it. scripts/lock_agreement.py is the check;
lock-deps.sh runs it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("lock_agreement", ROOT / "scripts" / "lock_agreement.py")
lock_agreement = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lock_agreement)


def test_the_committed_locks_agree():
    assert lock_agreement.conflicts(ROOT / "backend") == []


def _write(folder: Path, name: str, body: str) -> None:
    (folder / name).write_text(body, encoding="utf-8")


def test_two_extras_at_two_versions_are_a_conflict(tmp_path):
    _write(tmp_path, "requirements.lock", "anyio==4.0.0\n")
    _write(tmp_path, "requirements-telemetry.lock", "wrapt==2.5.0\n    # via opentelemetry-instrumentation\n")
    _write(tmp_path, "requirements-worker.lock", "wrapt==2.3.0 ; python_full_version >= '3.11'\n")
    (found,) = lock_agreement.conflicts(tmp_path)
    assert found.startswith("wrapt:") and "2.3.0" in found and "2.5.0" in found


def test_the_same_versions_under_other_markers_agree(tmp_path):
    _write(tmp_path, "requirements.lock", "colorama==0.4.6 ; sys_platform == 'win32'\n")
    _write(tmp_path, "requirements-wheel.lock", "colorama==0.4.6 ; os_name == 'nt'\nRich==13.0\n")
    _write(tmp_path, "requirements-worker.lock", "rich==13.0\n")
    assert lock_agreement.conflicts(tmp_path) == []


def test_knowledge_may_differ_only_in_what_it_owns(tmp_path):
    _write(tmp_path, "requirements.lock", "typer==0.27.1\nclick==8.2.0\n")
    _write(tmp_path, "requirements-knowledge.lock", "typer==0.26.8\nclick==8.1.0\n")
    (found,) = lock_agreement.conflicts(tmp_path)
    assert found.startswith("click:")
