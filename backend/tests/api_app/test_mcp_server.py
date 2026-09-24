"""Synapse's own MCP endpoint, driven by a real MCP client (D125).

The client is the SDK's streamable-HTTP client over the app's ASGI transport, so
every call crosses the edge's key check, the SDK's protocol handling and the
tool dispatch exactly as Claude Code's or Cursor's would.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
import pytest_asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from core.mcp_server.endpoint import MCP_PATH

URL = f"http://test{MCP_PATH}"

_ASK_THEN_THINK = [
    {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Which client?",
     "output_key": "answer", "next_step_id": "s"},
    {"id": "s", "name": "Think", "type": "llm",
     "prompt_template": "hi", "output_key": "out", "next_step_id": None},
]


@pytest_asyncio.fixture
async def mcp_app(test_app):
    """The app with its MCP endpoint running, as the lifespan would start it.

    The session manager holds an anyio task group, which must be entered and left
    on one task — so it runs in its own, as cloud's google-mcp tests do.
    """
    from core.mcp_server.endpoint import default_endpoint

    endpoint = default_endpoint()
    started, stop = asyncio.Event(), asyncio.Event()

    async def _serve():
        async with endpoint.serve():
            started.set()
            await stop.wait()

    task = asyncio.create_task(_serve())
    await asyncio.wait_for(started.wait(), 5)
    try:
        yield test_app
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def _http(app, headers=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers=headers or {}, timeout=30)


@contextlib.asynccontextmanager
async def mcp_session(app, api_key):
    async with _http(app, api_key["headers"]) as http, \
            streamable_http_client(URL, http_client=http) as (read, write, _), \
            ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _body(result) -> object:
    return json.loads(result.content[0].text)


_INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"}}}
_MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class TestTheKeyIsTheGate:
    @pytest.mark.parametrize("auth", [None, "Bearer sk-syn-" + "0" * 64, "Basic abc"])
    async def test_no_valid_key_is_refused_before_the_protocol(self, mcp_app, auth):
        headers = dict(_MCP_HEADERS)
        if auth:
            headers["authorization"] = auth
        async with _http(mcp_app) as http:
            resp = await http.post(MCP_PATH, json=_INITIALIZE, headers=headers)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    async def test_loopback_is_not_an_exemption(self, mcp_app):
        """The app's own routes let loopback in without the internal token; this one never does."""
        transport = httpx.ASGITransport(app=mcp_app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            resp = await http.post(MCP_PATH, json=_INITIALIZE, headers=_MCP_HEADERS)
        assert resp.status_code == 401

    async def test_a_revoked_key_is_refused(self, mcp_app, api_key):
        from core.api_keys import delete_api_key
        await delete_api_key(api_key["record"]["id"])
        async with _http(mcp_app, api_key["headers"]) as http:
            resp = await http.post(MCP_PATH, json=_INITIALIZE, headers=_MCP_HEADERS)
        assert resp.status_code == 401

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    async def test_there_is_no_stream_to_open_or_session_to_end(self, mcp_app, api_key, method):
        """Stateless: no server-sent stream and no session. The SDK held a GET open with
        no headers for ever, and Claude Code waited on it and never listed the tools."""
        async with _http(mcp_app, api_key["headers"]) as http:
            resp = await asyncio.wait_for(http.request(method, MCP_PATH, headers={
                "accept": "text/event-stream", "mcp-protocol-version": "2025-06-18"}), 5)
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"

    async def test_not_started_is_503_not_a_hang(self, test_app, api_key):
        async with _http(test_app, api_key["headers"]) as http:
            resp = await http.post(MCP_PATH, json=_INITIALIZE, headers=_MCP_HEADERS)
        assert resp.status_code == 503

    async def test_the_rest_of_the_app_is_untouched(self, mcp_app, api_key):
        async with _http(mcp_app, api_key["headers"]) as http:
            assert (await http.get("/api/v1/agents")).status_code == 200
            assert (await http.get("/api/v1/agents", headers={"authorization": ""})).status_code in (401, 403)


class TestTheToolList:
    async def test_builder_run_and_workflow_tools_and_no_deletes(self, mcp_app, api_key, seed_orchestration):
        await seed_orchestration(id="orch_brief", name="Client brief", description="Drafts a brief.")
        async with mcp_session(mcp_app, api_key) as s:
            tools = {t.name: t for t in (await s.list_tools()).tools}
        assert {"list_agents", "create_agent", "update_orchestration", "run_orchestration",
                "get_run", "answer_run", "cancel_run", "list_runs", "chat_with_agent"} <= set(tools)
        assert not {n for n in tools if n.startswith("delete_")}
        assert "finish_build" not in tools and "list_variables" not in tools
        assert tools["run_client_brief"].description.startswith("Drafts a brief.")
        assert tools["update_orchestration"].annotations.destructiveHint is True

    async def test_the_builders_own_orchestration_is_not_a_tool(self, mcp_app, api_key):
        from _fakes import seed
        await seed.seed_orchestrations([
            seed.make_orchestration(id="orch_mine", name="Mine"),
            seed.make_orchestration(id="orch_native_builder", name="AI Builder", system="native_builder"),
        ])
        async with mcp_session(mcp_app, api_key) as s:
            names = {t.name for t in (await s.list_tools()).tools}
        assert "run_mine" in names and "run_ai_builder" not in names


class TestBuilderTools:
    async def test_create_then_read_an_agent(self, mcp_app, api_key):
        async with mcp_session(mcp_app, api_key) as s:
            made = await s.call_tool("create_agent", {
                "name": "Researcher", "description": "Finds things", "type": "conversational",
                "tools": [], "system_prompt": "Find things.",
            })
            assert not made.isError, made.content[0].text
            agent_id = _body(made)["agent"]["id"]
            got = await s.call_tool("get_agent", {"agent_id": agent_id})
        assert _body(got)["name"] == "Researcher"

    async def test_a_delete_is_an_unknown_tool_and_deletes_nothing(self, mcp_app, api_key, seed_agent):
        from core.store.resources import get_agent
        agent = await seed_agent(id="agent_keep", name="Keep")
        async with mcp_session(mcp_app, api_key) as s:
            result = await s.call_tool("delete_agent", {"agent_id": agent["id"], "confirm": True})
        assert result.isError and "Unknown tool" in result.content[0].text
        assert await get_agent("agent_keep") is not None

    async def test_a_builder_tools_error_is_an_error_result(self, mcp_app, api_key):
        async with mcp_session(mcp_app, api_key) as s:
            result = await s.call_tool("get_agent", {"agent_id": "agent_nope"})
        assert result.isError


async def _paused_run(s, orch_id: str) -> dict:
    started = await s.call_tool("run_orchestration", {"orchestration_id": orch_id, "message": "go",
                                                      "wait_seconds": 10})
    assert not started.isError, started.content[0].text
    run = _body(started)
    assert run["status"] == "paused", run
    return run


class TestRuns:
    async def test_run_answer_complete_and_answered_once(self, mcp_app, api_key, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
        async with mcp_session(mcp_app, api_key) as s:
            run = await _paused_run(s, orch["id"])
            assert run["human_input_required"]["prompt"] == "Which client?"

            answered = await s.call_tool("answer_run", {"run_id": run["run_id"], "answer": "Acme",
                                                        "wait_seconds": 10})
            done = _body(answered)
            assert done["status"] == "completed", done
            assert done["shared_state"]["answer"] == "Acme"
            assert [st["step_id"] for st in done["steps"]] == ["ask", "s"]

            again = await s.call_tool("answer_run", {"run_id": run["run_id"], "answer": "Beta"})
            assert again.isError and "not waiting" in again.content[0].text

            listed = _body(await s.call_tool("list_runs", {"orchestration_id": orch["id"]}))
            assert [r["run_id"] for r in listed] == [run["run_id"]]

    async def test_a_workflow_tool_runs_its_workflow(self, mcp_app, api_key, seed_orchestration):
        await seed_orchestration(id="orch_hello", name="Say hello")
        async with mcp_session(mcp_app, api_key) as s:
            result = _body(await s.call_tool("run_say_hello", {"message": "go", "wait_seconds": 10}))
        assert result["status"] == "completed", result
        assert result["run_id"].startswith("run_orch_hello_")

    async def test_cancel_a_waiting_run(self, mcp_app, api_key, seed_orchestration):
        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
        async with mcp_session(mcp_app, api_key) as s:
            run = await _paused_run(s, orch["id"])
            stopped = _body(await s.call_tool("cancel_run", {"run_id": run["run_id"]}))
            assert stopped["status"] == "cancelled"
            read = _body(await s.call_tool("get_run", {"run_id": run["run_id"]}))
        assert read["status"] == "cancelled"

    async def test_a_run_that_outlasts_the_wait_carries_on(self, mcp_app, api_key, seed_orchestration, gated_llm):
        orch = await seed_orchestration(steps=[{"id": "s", "name": "Think", "type": "llm",
                                                "prompt_template": "hi", "output_key": "out",
                                                "next_step_id": None}], entry_step_id="s")
        async with mcp_session(mcp_app, api_key) as s:
            first = _body(await s.call_tool("run_orchestration", {
                "orchestration_id": orch["id"], "message": "go", "wait_seconds": 0}))
            assert first["status"] == "running" and "get_run" in first["note"]
            gated_llm.release.set()
            later = _body(await s.call_tool("get_run", {"run_id": first["run_id"], "wait_seconds": 10}))
        assert later["status"] == "completed", later

    async def test_the_builders_own_runs_are_not_served(self, mcp_app, api_key):
        from _fakes import seed
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.state import SharedState

        await seed.seed_orchestrations([seed.make_orchestration(id="orch_native_builder",
                                                                system="native_builder")])
        run_id = "run_orch_native_builder_1"
        await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                           status="paused", waiting_for_human=True)).checkpoint()
        async with mcp_session(mcp_app, api_key) as s:
            for tool, args in [("get_run", {"run_id": run_id}),
                               ("answer_run", {"run_id": run_id, "answer": "approve"}),
                               ("cancel_run", {"run_id": run_id})]:
                result = await s.call_tool(tool, args)
                assert result.isError and "not found" in result.content[0].text, tool
            assert _body(await s.call_tool("list_runs", {})) == []

    async def test_the_engines_own_keys_cannot_be_set(self, mcp_app, api_key, seed_orchestration):
        orch = await seed_orchestration()
        async with mcp_session(mcp_app, api_key) as s:
            result = await s.call_tool("run_orchestration", {
                "orchestration_id": orch["id"], "message": "go",
                "inputs": {"_model_override": "gpt-5", "client": "Acme"}})
            assert result.isError and "_model_override" in result.content[0].text
            assert _body(await s.call_tool("list_runs", {})) == []

    async def test_an_unknown_orchestration_is_refused(self, mcp_app, api_key):
        async with mcp_session(mcp_app, api_key) as s:
            result = await s.call_tool("run_orchestration", {"orchestration_id": "orch_nope", "message": "go"})
        assert result.isError and "not found" in result.content[0].text


class TestChat:
    async def test_one_turn_with_an_agent(self, mcp_app, api_key, seed_agent, fake_llm):
        await seed_agent(id="agent_chat", name="Chatty")
        fake_llm.script(["Hello from Chatty."])
        async with mcp_session(mcp_app, api_key) as s:
            reply = _body(await s.call_tool("chat_with_agent", {"message": "hi", "agent": "Chatty"}))
        assert reply["status"] == "completed", reply
        assert reply["response"] == "Hello from Chatty."
        assert reply["session_id"]

    async def test_an_unknown_agent_is_refused(self, mcp_app, api_key, seed_agent):
        await seed_agent(id="agent_chat", name="Chatty")
        async with mcp_session(mcp_app, api_key) as s:
            result = await s.call_tool("chat_with_agent", {"message": "hi", "agent": "Nobody"})
        assert result.isError and "not found" in result.content[0].text
