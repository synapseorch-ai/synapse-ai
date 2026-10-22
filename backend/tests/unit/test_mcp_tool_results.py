"""
How an MCP tool result becomes the text an agent sees.

A CallToolResult carries a list of content blocks. FastMCP turns a tool that
returns a list into one TextContent block per item, a tool that returns
nothing (None, an empty list) into no blocks at all, and an Image into an
ImageContent block. These tests call real tools over an in-memory MCP session
and check that the ReAct loop and the orchestration TOOL step pass on all of
the result, and that an empty or non-text result is not reported as a broken
server.
"""
import types

import pytest
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP, Image
from mcp.shared.memory import create_connected_server_and_client_session

from _fakes import seed as S
from _fakes.fake_llm import tool_call

# FastMCP's own settings model warns about a forward reference when the first
# server is built (mcp 1.29, upstream). It says nothing about this code.
pytestmark = pytest.mark.filterwarnings("ignore:Field 'lifespan' has an incomplete definition")


def _demo_server() -> FastMCP:
    mcp = FastMCP("demo")

    @mcp.tool()
    def list_items() -> list[str]:
        """Three items, sent as three text blocks."""
        return ["alpha", "beta", "gamma"]

    @mcp.tool()
    def no_matches() -> list[str]:
        """A search that found nothing, sent as no blocks."""
        return []

    @mcp.tool()
    def snapshot() -> Image:
        """A picture, sent as one image block."""
        return Image(data=b"\x89PNG\r\n", format="png")

    return mcp


class _FakeManager:
    def __init__(self):
        self.statuses = []

    def _set_status(self, name, status):
        self.statuses.append((name, status))


def _schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "demo tool",
        "parameters": {"type": "object", "properties": {}}}}


async def _run_mcp_tool(fake_llm, session, tool):
    """Drive run_agent_step through its MCP branch with a real session."""
    from core.react_engine import run_agent_step

    routed = f"demo__{tool}"
    manager = _FakeManager()
    server_module = types.SimpleNamespace(
        agent_sessions={"ext_mcp_demo": session},
        tool_router={routed: ("ext_mcp_demo", tool)},
        memory_store=None,
        mcp_manager=manager,
    )
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script([tool_call(routed), "done"])
    events = [ev async for ev in run_agent_step(
        message="go", agent_id=agent["id"], session_id="s1",
        server_module=server_module, agent_override=agent,
        tools_override=[_schema(routed)], max_turns=3,
    )]
    previews = [e["preview"] for e in events
                if e.get("type") == "tool_result" and e.get("tool_name") == routed]
    return previews, manager.statuses


class TestReactLoop:
    async def test_every_text_block_reaches_the_agent(self, fake_llm):
        async with create_connected_server_and_client_session(_demo_server()) as session:
            previews, statuses = await _run_mcp_tool(fake_llm, session, "list_items")
        assert len(previews) == 1
        for item in ("alpha", "beta", "gamma"):
            assert item in previews[0]
        assert statuses == []

    async def test_empty_result_is_not_an_error(self, fake_llm):
        async with create_connected_server_and_client_session(_demo_server()) as session:
            previews, statuses = await _run_mcp_tool(fake_llm, session, "no_matches")
        # A tool that found nothing must not mark its server disconnected.
        assert statuses == []
        assert len(previews) == 1
        assert not previews[0].startswith("Error")

    async def test_image_result_is_not_an_error(self, fake_llm):
        async with create_connected_server_and_client_session(_demo_server()) as session:
            previews, statuses = await _run_mcp_tool(fake_llm, session, "snapshot")
        assert statuses == []
        assert len(previews) == 1
        assert not previews[0].startswith("Error")
        assert "image/png" in previews[0]


class TestOrchestrationToolStep:
    async def test_every_text_block_is_returned(self):
        from core.orchestration.steps import ToolStepExecutor

        async with create_connected_server_and_client_session(_demo_server()) as session:
            engine = types.SimpleNamespace(server_module=types.SimpleNamespace(
                agent_sessions={"ext_mcp_demo": session},
                tool_router={"demo__list_items": ("ext_mcp_demo", "list_items")},
            ))
            out = await ToolStepExecutor()._execute_tool("demo__list_items", {}, engine)
        for item in ("alpha", "beta", "gamma"):
            assert item in out


class TestResultText:
    def test_single_text_block_is_unchanged(self):
        from core.mcp_client import tool_result_text
        result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text='{"ok": true}')])
        assert tool_result_text(result) == '{"ok": true}'

    def test_structured_content_used_when_there_are_no_blocks(self):
        from core.mcp_client import tool_result_text
        result = mcp_types.CallToolResult(content=[], structuredContent={"count": 0})
        assert tool_result_text(result) == '{"count": 0}'

    def test_embedded_text_resource_keeps_its_text(self):
        from core.mcp_client import tool_result_text
        result = mcp_types.CallToolResult(content=[
            mcp_types.TextContent(type="text", text="header"),
            mcp_types.EmbeddedResource(type="resource", resource=mcp_types.TextResourceContents(
                uri="file:///notes.txt", mimeType="text/plain", text="body text")),
        ])
        assert tool_result_text(result) == "header\nbody text"
