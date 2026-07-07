"""
V2 SSE streaming via the Redis event bridge, backed by fakeredis.

Covers the bridge functions directly (happy path + Last-Event-ID replay) and the
HTTP streaming endpoints end-to-end (fakeredis wired onto app.state).
"""
import pytest

from _fakes import fake_redis_stream as FRS
from _fakes.fake_pg import fake_session_factory


class TestRunEventBridge:
    async def test_yields_events_then_terminates_on_done(self, fake_redis):
        from core.scale.event_bridge import stream_run_events
        await FRS.load_run_events(fake_redis, "run_x", [
            {"type": "orchestration_start", "run_id": "run_x"},
            {"type": "step_complete", "orch_step_id": "s1"},
            {"type": "done"},
        ])
        chunks = await FRS.collect_sse(stream_run_events(fake_redis, "run_x", "0"))
        events = FRS.data_events(chunks)
        types = [e["type"] for e in events]
        assert types == ["orchestration_start", "step_complete", "done"]
        # Each SSE frame carries an id: line for Last-Event-ID reconnect.
        assert any(c.startswith("id:") for c in chunks)

    async def test_last_event_id_replays_only_newer(self, fake_redis):
        from core.scale.event_bridge import stream_run_events
        ids = await FRS.load_run_events(fake_redis, "run_y", [
            {"type": "orchestration_start"},
            {"type": "step_complete", "orch_step_id": "s1"},
            {"type": "done"},
        ])
        # Resume after the first event -> only events 2 and 3 replay.
        chunks = await FRS.collect_sse(stream_run_events(fake_redis, "run_y", ids[0]))
        types = [e["type"] for e in FRS.data_events(chunks)]
        assert "orchestration_start" not in types
        assert types == ["step_complete", "done"]


class TestChatEventBridge:
    async def test_chat_stream_yields_events(self, fake_redis):
        from core.scale.event_bridge import stream_chat_events
        await FRS.load_chat_events(fake_redis, "sess_z", [
            {"type": "status", "message": "working"},
            {"type": "response", "content": "hi"},
            {"type": "done"},
        ])
        events = FRS.data_events(await FRS.collect_sse(stream_chat_events(fake_redis, "sess_z", "0")))
        assert [e["type"] for e in events] == ["status", "response", "done"]


class TestV2StreamEndpoints:
    async def test_run_stream_endpoint(self, client, scale_app, api_key, fake_redis):
        scale_app.state.pg_session_factory = fake_session_factory()
        await FRS.load_run_events(fake_redis, "run_http", [
            {"type": "orchestration_start", "run_id": "run_http"},
            {"type": "done"},
        ])
        resp = await client.get("/api/v2/orchestrations/runs/run_http/stream",
                                headers=api_key["headers"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        types = [e["type"] for e in FRS.data_events(resp.text.split("\n\n"))]
        assert "orchestration_start" in types and "done" in types

    async def test_chat_stream_endpoint(self, client, scale_app, api_key, fake_redis):
        await FRS.load_chat_events(fake_redis, "sess_http", [
            {"type": "response", "content": "hello"},
            {"type": "done"},
        ])
        resp = await client.get("/api/v2/chat/sess_http/stream", headers=api_key["headers"])
        assert resp.status_code == 200
        types = [e["type"] for e in FRS.data_events(resp.text.split("\n\n"))]
        assert "response" in types and "done" in types

    async def test_stream_requires_auth(self, client, scale_app):
        resp = await client.get("/api/v2/chat/sess_http/stream")
        assert resp.status_code in (401, 403)
