"""
v2 tells an API caller what kind of failure it was, not the provider's words (F295).

The fleet's chat job published a failed turn as `{"type": "error", "message":
str(exc)}` onto the chat stream, and `GET /api/v2/chat/{id}/stream` and `/events`
pass every frame through as it is. So a v2 caller read "Gemini LLM Error: … Your
prepayment credits are depleted …" — or a key refusal, or a failed run's own
last line — which D118 and F231 keep to a member in the app. v1 and the job's
own webhook already said only a fixed line (F133, F231).

Now the job says what kind of failure it was beside the message (F275's `code`
and `retryable`; a failed workflow's `run_id`), and v2 hands an API caller v1's
sentence for it. The stream itself is unchanged for anyone else who reads it.
"""
import json

import pytest

from core.llm_providers import LLMError

WORDS = "Your prepayment credits are depleted. SECRETWORDS"
GENERIC = "The agent encountered an error processing your request."


async def _fleet_turn(monkeypatch, error):
    """Run the fleet's chat job with a loop that fails; return its stream's events."""
    import fakeredis.aioredis

    import core.react_engine as re_mod
    import core.scale.webhook as webhook_mod
    from core.scale import mcp_pool
    from core.scale import worker as worker_mod

    async def history(*_a):
        return []

    async def nothing(*_a, **_kw):
        return None

    async def acquire(*_a, **_kw):
        return object()

    async def failing_loop(request, server_module, should_stop=None, **_kw):
        yield {"type": "status", "message": "working"}
        raise error

    monkeypatch.setattr(worker_mod, "_load_job_settings", nothing)
    monkeypatch.setattr(worker_mod, "_load_chat_history", history)
    monkeypatch.setattr(worker_mod, "_upsert_chat_session", nothing)
    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(re_mod, "run_react_loop", failing_loop)
    monkeypatch.setattr(webhook_mod, "deliver_webhook", nothing)
    monkeypatch.setattr(worker_mod, "CHAT_CANCEL_POLL_SECONDS", 0.01)

    redis = fakeredis.aioredis.FakeRedis()
    with pytest.raises(type(error)):
        await worker_mod.run_agent_chat_job(
            {"redis": redis, "session_factory": object()}, "chat_f295", None, "Say OK.")
    return redis, [json.loads(f[b"data"]) for _, f in await redis.xrange("synapse:chat:chat_f295:events")]


class TestTheFleetJobSaysWhatKind:
    async def test_a_model_failure(self, monkeypatch):
        _redis, events = await _fleet_turn(
            monkeypatch, LLMError(f"Gemini LLM Error: the request was refused. 402 RESOURCE_EXHAUSTED. {WORDS}"))
        (error,) = [e for e in events if e["type"] == "error"]
        assert error["code"] == "quota_exhausted" and error["retryable"] is False

    async def test_a_failed_workflow(self, monkeypatch):
        from core.react_engine import RunFailed

        _redis, events = await _fleet_turn(monkeypatch, RunFailed(f"Orchestration failed. {WORDS}", run_id="run_f295"))
        (error,) = [e for e in events if e["type"] == "error"]
        assert error["run_id"] == "run_f295"


class TestV2SaysOnlyThat:
    @pytest.fixture
    async def failed_stream(self, monkeypatch, test_app):
        redis, _events = await _fleet_turn(
            monkeypatch, LLMError(f"Gemini LLM Error: the request was refused. 402 RESOURCE_EXHAUSTED. {WORDS}"))
        monkeypatch.setattr(test_app.state, "redis", redis, raising=False)
        return redis

    async def test_the_stream(self, client, api_key, failed_stream):
        resp = await client.get("/api/v2/chat/chat_f295/stream", headers=api_key["headers"])
        frames = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
        (error,) = [f for f in frames if f.get("type") == "error"]
        assert "SECRETWORDS" not in resp.text, "the provider's words stay in the app (D118)"
        assert error["message"].startswith(GENERIC) and "credit or quota" in error["message"]
        assert error["code"] == "quota_exhausted" and error["retryable"] is False

    async def test_the_events(self, client, api_key, failed_stream):
        resp = await client.get("/api/v2/chat/chat_f295/events", headers=api_key["headers"])
        assert resp.status_code == 200
        assert "SECRETWORDS" not in resp.text
        (error,) = [e["event"] for e in resp.json()["events"] if e["event"].get("type") == "error"]
        assert error["code"] == "quota_exhausted"

    async def test_a_failed_workflow_names_its_run(self, client, api_key, monkeypatch, test_app):
        from core.react_engine import RunFailed

        redis, _ = await _fleet_turn(monkeypatch, RunFailed(f"Orchestration failed. {WORDS}", run_id="run_f295"))
        monkeypatch.setattr(test_app.state, "redis", redis, raising=False)
        resp = await client.get("/api/v2/chat/chat_f295/events", headers=api_key["headers"])
        assert "SECRETWORDS" not in resp.text
        (error,) = [e["event"] for e in resp.json()["events"] if e["event"].get("type") == "error"]
        assert "run_f295" in error["message"]
