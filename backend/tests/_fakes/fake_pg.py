"""
A minimal async SQLAlchemy-session stand-in for V2 (distributed) tests.

The V2 endpoints read/write Postgres via ``async with session_factory() as s``.
This fake lets contract tests exercise those handlers with no database: SELECTs
return a preset row (or None), and writes (execute/commit/add) are no-ops.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class _FakeResult:
    def __init__(self, row: Any):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        return self._row

    def first(self):
        return self._row

    def all(self):
        return self._row if isinstance(self._row, list) else ([] if self._row is None else [self._row])

    def scalars(self):
        return SimpleNamespace(all=lambda: (self._row or []))


class _FakeSession:
    def __init__(self, row: Any):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _FakeResult(self._row)

    def add(self, *a, **k):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


def fake_session_factory(row: Any = None):
    """Return a callable that yields a fake async session whose queries resolve
    to ``row`` (or None for 'not found')."""
    def _factory():
        return _FakeSession(row)
    return _factory


def run_row(run_id: str = "run_1", **over) -> SimpleNamespace:
    data = dict(
        run_id=run_id, orchestration_id="orch_1", status="running", tenant_id="default",
        current_step_id="s1", waiting_for_human=False, worker_id="worker-a",
        total_cost_usd=0.0, total_tokens_used=0, started_at=None, ended_at=None,
    )
    data.update(over)
    return SimpleNamespace(**data)


def chat_row(session_id: str = "sess_1", **over) -> SimpleNamespace:
    data = dict(
        session_id=session_id, agent_id="agent_1", status="running",
        messages=[], last_message_at=None, worker_id="worker-a",
    )
    data.update(over)
    return SimpleNamespace(**data)
