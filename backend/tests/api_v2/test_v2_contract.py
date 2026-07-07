"""
V2 (distributed) API — HTTP contract tests.

V2 enqueues to Redis/ARQ and reads Postgres. Here the queue is an AsyncMock and
Postgres is a fake session factory, so we verify the *contract*: scale-mode
gating, 202 responses with the right payload/URLs, the enqueue call, auth, and
validation — with zero real infrastructure.
"""
import pytest

from _fakes.fake_pg import fake_session_factory


@pytest.fixture
def v2(scale_app, monkeypatch):
    """scale_app + a working fake PG factory + a neutralized metrics counter."""
    scale_app.state.pg_session_factory = fake_session_factory()
    import core.scale.metrics as metrics
    monkeypatch.setattr(metrics, "record_run_enqueued", lambda **k: None, raising=False)
    return scale_app


class TestScaleGating:
    async def test_v2_requires_scale_mode(self, client, test_app, api_key):
        # test_app leaves app.state.redis = None -> 503.
        resp = await client.post("/api/v2/chat", json={"message": "hi"},
                                 headers=api_key["headers"])
        assert resp.status_code == 503
        assert "scale mode" in resp.json()["detail"].lower()


class TestV2Chat:
    async def test_chat_enqueues_and_returns_202(self, client, v2, api_key):
        resp = await client.post("/api/v2/chat",
                                 json={"message": "hello", "agent": "agent_1"},
                                 headers=api_key["headers"])
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["session_id"]
        assert body["stream_url"] == f"/api/v2/chat/{body['session_id']}/stream"
        assert body["status_url"] == f"/api/v2/chat/{body['session_id']}/status"
        # The job was actually enqueued.
        v2.state.arq_redis.enqueue_job.assert_awaited()
        args, kwargs = v2.state.arq_redis.enqueue_job.call_args
        assert args[0] == "run_agent_chat_job"
        assert kwargs["message"] == "hello"

    async def test_chat_honors_supplied_session_id(self, client, v2, api_key):
        resp = await client.post("/api/v2/chat",
                                 json={"message": "hi", "session_id": "my-session"},
                                 headers=api_key["headers"])
        assert resp.json()["session_id"] == "my-session"

    async def test_chat_requires_auth(self, client, v2):
        resp = await client.post("/api/v2/chat", json={"message": "hi"})
        assert resp.status_code in (401, 403)

    async def test_chat_bad_body_is_422(self, client, v2, api_key):
        resp = await client.post("/api/v2/chat", json={}, headers=api_key["headers"])
        assert resp.status_code == 422


class TestV2OrchestrationRun:
    async def test_run_enqueues_and_returns_202(self, client, v2, api_key):
        resp = await client.post("/api/v2/orchestrations/orch_1/run",
                                 json={"message": "go"}, headers=api_key["headers"])
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"
        assert body["run_id"]
        assert body["stream_url"] == f"/api/v2/orchestrations/runs/{body['run_id']}/stream"
        assert body["status_url"] == f"/api/v2/orchestrations/runs/{body['run_id']}/status"
        v2.state.arq_redis.enqueue_job.assert_awaited()
        args, kwargs = v2.state.arq_redis.enqueue_job.call_args
        assert args[0] == "run_orchestration_job"
        assert kwargs["orch_id"] == "orch_1"

    async def test_run_requires_auth(self, client, v2):
        resp = await client.post("/api/v2/orchestrations/orch_1/run", json={"message": "go"})
        assert resp.status_code in (401, 403)


class TestV2Resume:
    async def test_resume_returns_202(self, client, v2, api_key):
        from _fakes.fake_pg import fake_session_factory, run_row
        # A paused run exists; resume publishes to Redis + enqueues a resume job.
        v2.state.pg_session_factory = fake_session_factory(run_row("run_1", status="paused"))
        resp = await client.post("/api/v2/orchestrations/runs/run_1/resume",
                                 json={"response": {"approved": True}},
                                 headers=api_key["headers"])
        assert resp.status_code == 202
        body = resp.json()
        assert body["run_id"] == "run_1"
        assert body["status"] == "resuming"

    async def test_resume_unknown_run_is_404(self, client, v2, api_key):
        from _fakes.fake_pg import fake_session_factory
        v2.state.pg_session_factory = fake_session_factory(None)
        resp = await client.post("/api/v2/orchestrations/runs/ghost/resume",
                                 json={"response": "yes"}, headers=api_key["headers"])
        assert resp.status_code == 404
