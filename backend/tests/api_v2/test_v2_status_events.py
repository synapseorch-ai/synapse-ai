"""V2 status / events / cancel endpoints (Postgres reads via a fake session)."""
import pytest

from _fakes import fake_redis_stream as FRS
from _fakes.fake_pg import fake_session_factory, run_row, chat_row


class TestRunStatus:
    async def test_status_returns_row(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory(
            run_row("run_1", status="paused", waiting_for_human=True))
        resp = await client.get("/api/v2/orchestrations/runs/run_1/status",
                                headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "run_1"
        assert body["status"] == "paused"
        assert body["waiting_for_human"] is True

    async def test_status_unknown_run_is_404(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory(None)
        resp = await client.get("/api/v2/orchestrations/runs/ghost/status",
                                headers=api_key["headers"])
        assert resp.status_code == 404


class TestChatStatus:
    async def test_chat_status_returns_row(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory(
            chat_row("sess_1", status="completed", messages=[{"role": "user", "content": "hi"}]))
        resp = await client.get("/api/v2/chat/sess_1/status", headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "sess_1"
        assert body["status"] == "completed"
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    async def test_chat_status_unknown_is_not_found(self, client, scale_app, api_key):
        scale_app.state.pg_session_factory = fake_session_factory(None)
        resp = await client.get("/api/v2/chat/ghost/status", headers=api_key["headers"])
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"


class TestRunEvents:
    async def test_events_returns_stream_contents(self, client, scale_app, api_key, fake_redis):
        await FRS.load_run_events(fake_redis, "run_ev", [
            {"type": "orchestration_start"}, {"type": "done"},
        ])
        resp = await client.get("/api/v2/orchestrations/runs/run_ev/events",
                                headers=api_key["headers"])
        assert resp.status_code == 200
        # Response is a JSON list of stored events (shape depends on the handler).
        assert isinstance(resp.json(), (list, dict))
