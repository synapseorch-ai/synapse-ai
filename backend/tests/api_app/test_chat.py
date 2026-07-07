"""
Agent chat — internal app routes: POST /chat and POST /chat/stream.

Two layers:
  * Contract layer  — patches ``core.routes.chat.run_react_loop`` with a canned
    event stream to exercise every branch of the handler + SSE serializer.
  * Engine layer    — drives the REAL run_react_loop -> run_agent_step with the
    fake LLM, proving the LLM interception works through the whole ReAct stack.
"""
import json

import pytest

from _fakes import engine_events as E
from _fakes.fake_redis_stream import data_events


def _sse_json(text: str) -> list[dict]:
    """Parse SSE 'data:' JSON payloads from a streamed body."""
    return data_events(text.split("\n\n"))


# ── /chat (non-streaming) ─────────────────────────────────────────────────────
class TestChatSync:
    async def test_returns_final_response(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop",
                            E.gen_from([E.status(), E.final("Hello there", intent="chat")]))
        resp = await client.post("/chat", json={"message": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "Hello there"
        assert body["intent"] == "chat"

    async def test_error_event_becomes_response(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop",
                            E.gen_from([E.status(), E.error("model exploded")]))
        resp = await client.post("/chat", json={"message": "hi"})
        assert resp.status_code == 200
        assert resp.json()["response"] == "model exploded"

    async def test_no_final_event_returns_fallback(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([E.status()]))
        resp = await client.post("/chat", json={"message": "hi"})
        assert resp.status_code == 200
        assert "completed" in resp.json()["response"].lower()

    async def test_no_agents_connected_returns_500(self, client, monkeypatch):
        import core.server as server
        monkeypatch.setattr(server, "agent_sessions", {}, raising=False)
        resp = await client.post("/chat", json={"message": "hi"})
        assert resp.status_code == 500
        assert "no agents" in resp.json()["detail"].lower()

    async def test_missing_message_is_422(self, client):
        resp = await client.post("/chat", json={})
        assert resp.status_code == 422

    async def test_data_and_tool_name_passed_through(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([
            E.final("Weather is sunny", intent="tool", data={"temp": 21},
                    tool_name="get_weather"),
        ]))
        resp = await client.post("/chat", json={"message": "weather?"})
        body = resp.json()
        assert body["data"] == {"temp": 21}
        assert body["tool_name"] == "get_weather"


# ── /chat/stream (SSE) ────────────────────────────────────────────────────────
class TestChatStream:
    async def test_full_event_sequence(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([
            E.status("Processing your request..."),
            E.thinking("Let me check"),
            E.tool_execution("get_weather", {"city": "NYC"}),
            E.tool_result("get_weather", "21C sunny"),
            E.final("It's sunny", intent="chat"),
        ]))
        resp = await client.post("/chat/stream", json={"message": "weather?"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _sse_json(resp.text)
        types = [e["type"] for e in events]
        assert types == ["status", "thinking", "tool_execution", "tool_result",
                         "response", "done"]
        tool_exec = next(e for e in events if e["type"] == "tool_execution")
        assert tool_exec["tool_name"] == "get_weather"
        assert tool_exec["args"] == {"city": "NYC"}
        response_ev = next(e for e in events if e["type"] == "response")
        assert response_ev["content"] == "It's sunny"

    async def test_error_event_serialized(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop",
                            E.gen_from([E.status(), E.error("kaboom")]))
        resp = await client.post("/chat/stream", json={"message": "hi"})
        events = _sse_json(resp.text)
        assert {"type": "error", "message": "kaboom"} in events

    async def test_sub_agent_final_becomes_agent_step_result(self, client, monkeypatch):
        import core.routes.chat as chat
        # A 'final' carrying orch_step_id is a sub-agent step result, not a top-level response.
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([
            E.final("sub done", orch_step_id="s1", step_name="Sub"),
        ]))
        resp = await client.post("/chat/stream", json={"message": "hi"})
        events = _sse_json(resp.text)
        assert events[0]["type"] == "agent_step_result"
        assert events[0]["orch_step_id"] == "s1"
        # No top-level response/done for a sub-agent final.
        assert all(e["type"] != "response" for e in events)

    async def test_human_input_required_terminates_with_done(self, client, monkeypatch):
        import core.routes.chat as chat
        monkeypatch.setattr(chat, "run_react_loop", E.gen_from([
            E.orch_start(), E.human_input_required(prompt="Approve?"),
            E.final("should not appear"),  # after human_input the generator returns
        ]))
        resp = await client.post("/chat/stream", json={"message": "hi"})
        events = _sse_json(resp.text)
        types = [e["type"] for e in events]
        assert "human_input_required" in types
        assert types[-1] == "done"
        assert "response" not in types  # stream stopped at human input


# ── real engine end-to-end (fake LLM drives the actual ReAct loop) ────────────
class TestChatRealEngine:
    async def test_real_loop_plain_answer(self, client, monkeypatch, fake_llm, seed_agent):
        """Seed a real agent, prime the tool cache so MCP introspection is
        skipped, and let the REAL run_react_loop + run_agent_step run with the
        fake LLM returning a plain final answer."""
        agent = seed_agent(tools=[], skip_default_tools=True)
        # Prime the per-session tool cache so aggregate_all_tools doesn't touch
        # the placeholder session object.
        import core.tools as tools
        monkeypatch.setitem(tools._session_tools_cache, "_test", [])
        fake_llm.set_default("The answer is 42.")

        resp = await client.post("/chat", json={"message": "meaning of life?",
                                                 "agent_id": agent["id"]})
        assert resp.status_code == 200
        assert resp.json()["response"] == "The answer is 42."
        assert fake_llm.call_count >= 1
        # The fake LLM was routed the seeded agent's id.
        assert fake_llm.last_call.get("agent_id") == agent["id"]
