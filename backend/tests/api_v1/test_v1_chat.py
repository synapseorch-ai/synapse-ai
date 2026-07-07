"""V1 external API — agent chat (sync + SSE). Auth via real API key fixture."""
import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events


def _sse_json(text: str) -> list[dict]:
    return data_events(text.split("\n\n"))


class TestV1ChatSync:
    async def test_returns_response_and_agent_metadata(self, client, api_key, seed_agent, monkeypatch):
        agent = seed_agent(name="V1 Agent")
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("hello world")]))
        resp = await client.post("/api/v1/chat",
                                 json={"message": "hi", "agent": agent["name"]},
                                 headers=api_key["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "hello world"
        assert body["agent_id"] == agent["id"]
        assert body["agent_name"] == "V1 Agent"
        assert body["session_id"]  # auto-generated

    async def test_session_id_is_preserved(self, client, api_key, seed_agent, monkeypatch):
        seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.final("x")]))
        resp = await client.post("/api/v1/chat",
                                 json={"message": "hi", "session_id": "sess-fixed"},
                                 headers=api_key["headers"])
        assert resp.json()["session_id"] == "sess-fixed"

    async def test_no_agents_configured_is_400(self, client, api_key):
        # No agent seeded -> _resolve_agent_for_api returns None.
        import core.react_engine as re
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers=api_key["headers"])
        assert resp.status_code == 400

    async def test_agent_error_is_500(self, client, api_key, seed_agent, monkeypatch):
        seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.error("agent failed")]))
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers=api_key["headers"])
        assert resp.status_code == 500

    async def test_no_agent_sessions_is_503(self, client, api_key, seed_agent, monkeypatch):
        seed_agent()
        import core.server as server
        monkeypatch.setattr(server, "agent_sessions", {}, raising=False)
        resp = await client.post("/api/v1/chat", json={"message": "hi"},
                                 headers=api_key["headers"])
        assert resp.status_code == 503


class TestV1ChatStream:
    async def test_session_event_first_then_full_sequence(self, client, api_key, seed_agent, monkeypatch):
        agent = seed_agent(name="Streamer")
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([
            E.status(), E.thinking("hmm"),
            E.tool_execution("search", {"q": "x"}), E.tool_result("search", "found"),
            E.final("final answer"),
        ]))
        resp = await client.post("/api/v1/chat/stream",
                                 json={"message": "hi", "agent": agent["name"]},
                                 headers=api_key["headers"])
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_json(resp.text)
        types = [e["type"] for e in events]
        assert types[0] == "session"  # session emitted first
        assert events[0]["agent_id"] == agent["id"]
        assert "status" in types and "tool_execution" in types
        assert "response" in types and types[-1] == "done"

    async def test_stream_error_is_sanitized(self, client, api_key, seed_agent, monkeypatch):
        seed_agent()
        import core.react_engine as re
        monkeypatch.setattr(re, "run_react_loop", E.gen_from([E.error("raw internal detail")]))
        resp = await client.post("/api/v1/chat/stream", json={"message": "hi"},
                                 headers=api_key["headers"])
        events = _sse_json(resp.text)
        err = next(e for e in events if e["type"] == "error")
        # Raw internal error text is not leaked to API clients.
        assert "raw internal detail" not in err["message"]
