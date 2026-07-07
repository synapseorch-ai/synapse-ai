"""V1 external API — orchestration run / run-stream / resume."""
import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events


def _sse_json(text: str) -> list[dict]:
    return data_events(text.split("\n\n"))


class TestV1OrchestrationRunSync:
    async def test_completed_run_returns_final(self, client, api_key, seed_orchestration, monkeypatch):
        orch = seed_orchestration()
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(orch_id=orch["id"]),
            E.step_start(), E.step_complete(),
            {"type": "orchestration_complete", "status": "completed", "final_state": {"k": "v"}},
            E.final("orchestration done", data={"step_history": [{"step": "s1"}]}),
        ]))
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["response"] == "orchestration done"
        assert body["shared_state"] == {"k": "v"}
        assert body["step_history"] == [{"step": "s1"}]

    async def test_paused_run_returns_human_input(self, client, api_key, seed_orchestration, monkeypatch):
        orch = seed_orchestration()
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(orch_id=orch["id"]),
            E.human_input_required(step_id="approve", prompt="OK to proceed?",
                                   fields=[{"name": "confirm"}]),
            E.final("should not be reached"),
        ]))
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "paused"
        assert body["run_id"]
        hir = body["human_input_required"]
        assert hir["step_id"] == "approve"
        assert hir["prompt"] == "OK to proceed?"
        assert hir["fields"] == [{"name": "confirm"}]

    async def test_unknown_orchestration_is_404(self, client, api_key):
        resp = await client.post("/api/v1/orchestrations/nope/run",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 404

    async def test_requires_auth(self, client, seed_orchestration):
        orch = seed_orchestration()
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run",
                                 json={"message": "go"})
        assert resp.status_code in (401, 403)


class TestV1OrchestrationRunStream:
    async def test_stream_emits_events_and_done(self, client, api_key, seed_orchestration, monkeypatch):
        orch = seed_orchestration()
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(orch_id=orch["id"]), E.step_start(), E.step_complete(),
            E.orch_complete(),
        ]))
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 200
        types = [e["type"] for e in _sse_json(resp.text)]
        assert "orchestration_start" in types
        assert "orchestration_complete" in types
        assert types[-1] == "done"

    async def test_stream_stops_at_human_input(self, client, api_key, seed_orchestration, monkeypatch):
        orch = seed_orchestration()
        import core.orchestration.engine as engine_mod
        monkeypatch.setattr(engine_mod, "OrchestrationEngine", E.fake_engine([
            E.orch_start(orch_id=orch["id"]),
            E.human_input_required(prompt="Approve?"),
            E.orch_complete(),  # must not be streamed after human input
        ]))
        resp = await client.post(f"/api/v1/orchestrations/{orch['id']}/run/stream",
                                 json={"message": "go"}, headers=api_key["headers"])
        types = [e["type"] for e in _sse_json(resp.text)]
        assert "human_input_required" in types
        assert types[-1] == "done"
        assert "orchestration_complete" not in types
