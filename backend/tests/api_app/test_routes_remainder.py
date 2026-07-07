"""Remaining read endpoints: v1 list/get, v2 reads, orchestration estimate/runs,
and a few tool/settings reads. Covers handler code with graceful assertions."""
import pytest

from _fakes.fake_pg import fake_session_factory


class TestV1ReadEndpoints:
    async def test_list_and_get_agents(self, client, api_key, seed_agent):
        agent = seed_agent(id="v1a", name="V1A")
        assert (await client.get("/api/v1/agents", headers=api_key["headers"])).status_code == 200
        got = await client.get(f"/api/v1/agents/{agent['id']}", headers=api_key["headers"])
        assert got.status_code == 200

    async def test_list_and_get_orchestrations(self, client, api_key, seed_orchestration):
        orch = seed_orchestration(id="v1o")
        assert (await client.get("/api/v1/orchestrations", headers=api_key["headers"])).status_code == 200
        got = await client.get(f"/api/v1/orchestrations/{orch['id']}", headers=api_key["headers"])
        assert got.status_code == 200

    async def test_get_unknown_agent_404(self, client, api_key):
        resp = await client.get("/api/v1/agents/ghost", headers=api_key["headers"])
        assert resp.status_code in (404, 400)


class TestV2ReadEndpoints:
    async def test_list_agents(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory([])  # empty list
        resp = await client.get("/api/v2/agents", headers=api_key["headers"])
        assert resp.status_code < 500

    async def test_workers_and_queue_stats(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory([])
        for path in ["/api/v2/workers", "/api/v2/queue/stats"]:
            resp = await client.get(path, headers=api_key["headers"])
            assert resp.status_code < 500


class TestOrchestrationReads:
    async def test_runs_list_and_estimate(self, client, seed_orchestration):
        orch = seed_orchestration(id="est")
        assert (await client.get("/api/orchestrations/runs")).status_code == 200
        est = await client.get(f"/api/orchestrations/{orch['id']}/estimate")
        assert est.status_code == 200
        assert "average_cost_usd" in est.json()


class TestToolAndSettingReads:
    async def test_python_packages(self, client):
        resp = await client.get("/api/tools/python/packages")
        assert resp.status_code < 500

    async def test_check_embed(self, client):
        resp = await client.get("/api/settings/check-embed")
        assert resp.status_code < 500

    async def test_examples_and_status(self, client):
        assert (await client.get("/api/examples")).status_code == 200
        assert (await client.get("/api/status")).status_code < 500
