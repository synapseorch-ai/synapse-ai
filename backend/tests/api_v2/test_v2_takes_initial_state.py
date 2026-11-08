"""A self-hosted `/api/v2` run takes `initial_state`, and refuses a field it does not know (F434).

G1c read it: OSS's v2 run body was `message`, `session_id` and the webhook fields with Pydantic's default
`extra="ignore"`, so `{"message": "INV-1047", "initial_state": {"invoice": …}}` answered 202 and the run started with
no invoice in its state — without a word. Cloud's v2 run takes `initial_state` and refuses unknown fields (422), and
OSS's v1 run takes it too (D126). The worker's job always could (`run_orchestration_job(initial_state=…)`); the
route dropped it. v2 chat ignored a misspelt field the same way.
"""
import pytest

from _fakes.fake_pg import fake_session_factory


@pytest.fixture
async def v2(scale_app, monkeypatch, seed_agent):
    await seed_agent(id="agent_1", name="Agent One")
    scale_app.state.pg_session_factory = fake_session_factory()
    import core.scale.metrics as metrics
    monkeypatch.setattr(metrics, "record_run_enqueued", lambda **k: None, raising=False)
    return scale_app


async def test_the_state_reaches_the_job(client, v2, api_key):
    resp = await client.post("/api/v2/orchestrations/orch_1/run",
                             json={"message": "INV-1047", "initial_state": {"invoice": {"total": 41.5}}},
                             headers=api_key["headers"])
    assert resp.status_code == 202, resp.text
    _, kwargs = v2.state.arq_redis.enqueue_job.call_args
    assert kwargs["initial_state"] == {"invoice": {"total": 41.5}}


async def test_no_state_is_none(client, v2, api_key):
    resp = await client.post("/api/v2/orchestrations/orch_1/run", json={"message": "go"}, headers=api_key["headers"])
    assert resp.status_code == 202, resp.text
    _, kwargs = v2.state.arq_redis.enqueue_job.call_args
    assert kwargs["initial_state"] is None


async def test_the_engines_own_keys_are_refused_by_name(client, v2, api_key):
    resp = await client.post("/api/v2/orchestrations/orch_1/run",
                             json={"message": "go", "initial_state": {"_model_override": "x", "ok": 1}},
                             headers=api_key["headers"])
    assert resp.status_code == 422, resp.text
    assert "_model_override" in resp.text
    v2.state.arq_redis.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("path, body", [
    ("/api/v2/orchestrations/orch_1/run", {"message": "go", "intial_state": {"invoice": 1}}),
    ("/api/v2/chat", {"message": "hi", "agent": "agent_1", "sesion_id": "s1"}),
])
async def test_a_field_it_does_not_know_is_refused(client, v2, api_key, path, body):
    resp = await client.post(path, json=body, headers=api_key["headers"])
    assert resp.status_code == 422, resp.text
    assert "intial_state" in resp.text or "sesion_id" in resp.text
