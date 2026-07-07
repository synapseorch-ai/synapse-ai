"""
Orchestration — internal app routes (core/routes/orchestrations.py).

CRUD is tested directly; execution (/run, /resume) is tested by patching
``core.orchestration.engine.OrchestrationEngine`` with a fake whose ``run`` /
``resume_failed`` yield canned lifecycle events, so the SSE assembly (background
task → queue → drain → done sentinel) is verified without engine internals.
"""
import json

import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events


def _sse_json(text: str) -> list[dict]:
    return data_events(text.split("\n\n"))


class _FakeEngine:
    """Stand-in for OrchestrationEngine. Class-level ``events`` drives output."""
    events: list[dict] = []

    def __init__(self, orch=None, server_module=None):
        self.orch = orch

    async def run(self, user_input, run_id, **kwargs):
        for ev in type(self).events:
            yield ev

    @classmethod
    async def resume_failed(cls, run_id, server_module):
        for ev in cls.events:
            yield ev


# ── CRUD ──────────────────────────────────────────────────────────────────────
class TestOrchestrationCrud:
    async def test_list_get_delete(self, client, seed_orchestration):
        orch = seed_orchestration(name="My Orch")
        # get
        resp = await client.get(f"/api/orchestrations/{orch['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "My Orch"
        # delete
        resp = await client.delete(f"/api/orchestrations/{orch['id']}")
        assert resp.status_code == 200
        # gone
        resp = await client.get(f"/api/orchestrations/{orch['id']}")
        assert resp.status_code == 404

    async def test_get_unknown_is_404(self, client):
        resp = await client.get("/api/orchestrations/does-not-exist")
        assert resp.status_code == 404


# ── execution (SSE) ───────────────────────────────────────────────────────────
class TestOrchestrationRun:
    async def test_run_streams_lifecycle_then_done(self, client, seed_orchestration, monkeypatch):
        orch = seed_orchestration()
        import core.orchestration.engine as engine_mod
        _FakeEngine.events = [
            E.orch_start(orch_id=orch["id"]),
            E.step_start("s1", "Step 1"),
            E.step_complete("s1", "Step 1"),
            E.orch_complete(status_str="completed"),
        ]
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", _FakeEngine)

        resp = await client.post(f"/api/orchestrations/{orch['id']}/run",
                                 json={"message": "go"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_json(resp.text)
        types = [e["type"] for e in events]
        assert "orchestration_start" in types
        assert "step_start" in types
        assert "orchestration_complete" in types
        assert types[-1] == "done"  # endpoint appends the done sentinel

    async def test_run_unknown_orch_is_404(self, client):
        resp = await client.post("/api/orchestrations/nope/run", json={"message": "x"})
        assert resp.status_code == 404

    async def test_engine_error_surfaces_as_orchestration_error(self, client, seed_orchestration, monkeypatch):
        orch = seed_orchestration()

        class _BoomEngine(_FakeEngine):
            async def run(self, user_input, run_id, **kwargs):
                raise RuntimeError("step blew up")
                yield  # pragma: no cover

        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", _BoomEngine)
        resp = await client.post(f"/api/orchestrations/{orch['id']}/run",
                                 json={"message": "go"})
        events = _sse_json(resp.text)
        assert any(e["type"] == "orchestration_error" and "blew up" in e.get("error", "")
                   for e in events)


class TestOrchestrationResume:
    async def test_resume_failed_streams_events(self, client, monkeypatch):
        import core.orchestration.engine as engine_mod
        _FakeEngine.events = [E.orch_start(), E.orch_complete()]
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", _FakeEngine)
        resp = await client.post("/api/orchestrations/runs/run_123/resume", json={})
        assert resp.status_code == 200
        types = [e["type"] for e in _sse_json(resp.text)]
        assert "orchestration_complete" in types
        assert types[-1] == "done"
