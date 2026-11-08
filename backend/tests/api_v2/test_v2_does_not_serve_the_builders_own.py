"""v2 neither lists, serves nor runs the AI builder's own orchestration, nor reads its runs (F96).

F162 and G's `e174f9c` closed this on v1, the CLI and MCP: the builder's seeded
orchestration is its own machinery, not workspace content, and its runs' ids
(`run_orch_native_builder_<ms>_…`) are guessable. v2 (scale mode) read the
orchestrations table with no check at all: it listed the builder, served its
definition, queued it — the worker then ran it — and answered its run routes.
"""
import asyncio

import pytest

from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

BUILDER_RUN = f"run_{NATIVE_BUILDER_ORCH_ID}_1790000000000_abcdef"


@pytest.fixture
async def v2(scale_app, monkeypatch):
    from core.store.engine import get_store
    from core.store.models import OrchestrationDB, OrchestrationRunDB
    sf = await get_store()
    async with sf() as s:
        # A builder run, paused on a question: what status and resume would answer.
        s.add(OrchestrationRunDB(run_id=BUILDER_RUN, orchestration_id=NATIVE_BUILDER_ORCH_ID,
                                 status="paused", waiting_for_human=True, shared_state={}, step_history=[]))
        s.add(OrchestrationDB(id=NATIVE_BUILDER_ORCH_ID, name="AI Builder", description="",
                              definition={"id": NATIVE_BUILDER_ORCH_ID, "name": "AI Builder",
                                          "system": "native_builder", "steps": []}))
        s.add(OrchestrationDB(id="orch_mine", name="Mine", description="",
                              definition={"id": "orch_mine", "name": "Mine", "steps": []}))
        s.add(OrchestrationDB(id="orch_retired", name="Retired", description="", is_active=False,
                              definition={"id": "orch_retired", "name": "Retired", "steps": []}))
        await s.commit()
    scale_app.state.pg_session_factory = sf
    import core.scale.metrics as metrics
    monkeypatch.setattr(metrics, "record_run_enqueued", lambda **k: None, raising=False)
    return scale_app


class TestTheBuildersOwn:
    async def test_the_list_leaves_it_out(self, client, v2, api_key):
        ids = [o["id"] for o in (await client.get("/api/v2/orchestrations", headers=api_key["headers"])).json()]
        assert "orch_mine" in ids
        assert NATIVE_BUILDER_ORCH_ID not in ids and "orch_retired" not in ids

    async def test_its_definition_is_not_served(self, client, v2, api_key):
        got = await client.get(f"/api/v2/orchestrations/{NATIVE_BUILDER_ORCH_ID}", headers=api_key["headers"])
        assert got.status_code == 404
        assert (await client.get("/api/v2/orchestrations/orch_mine", headers=api_key["headers"])).status_code == 200

    async def test_it_is_not_queued(self, client, v2, api_key):
        run = await client.post(f"/api/v2/orchestrations/{NATIVE_BUILDER_ORCH_ID}/run", json={"message": "go"},
                                headers=api_key["headers"])
        assert run.status_code == 404
        v2.state.arq_redis.enqueue_job.assert_not_awaited()

    @pytest.mark.parametrize("method, path, body", [
        ("GET", "status", None), ("GET", "stream", None), ("GET", "events", None),
        ("POST", "cancel", None), ("POST", "resume", {"response": "yes"}),
    ])
    async def test_its_runs_are_not_answered(self, client, v2, api_key, method, path, body):
        # Bounded: served, the stream tails a run nobody will finish.
        resp = await asyncio.wait_for(client.request(
            method, f"/api/v2/orchestrations/runs/{BUILDER_RUN}/{path}", json=body,
            headers=api_key["headers"]), 5)
        assert resp.status_code == 404, resp.text

    async def test_a_workspace_orchestration_still_runs(self, client, v2, api_key):
        run = await client.post("/api/v2/orchestrations/orch_mine/run", json={"message": "go"},
                                headers=api_key["headers"])
        assert run.status_code == 202, run.text
        run_id = run.json()["run_id"]
        status = await client.get(f"/api/v2/orchestrations/runs/{run_id}/status", headers=api_key["headers"])
        assert status.status_code == 200
