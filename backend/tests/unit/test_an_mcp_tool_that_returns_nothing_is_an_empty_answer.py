"""An MCP tool's result is read whole and never breaks the reading: nothing is an empty answer (F1344; CAP3's cap-03,
FF17; eb 11-04).

An MCP server sends a tool's list as one content part per item, and an empty list as no content at all. The agent
path read `result.content[0].text` unguarded: a tool that returned [] gave the agent "list index out of range", and
in cap-03 both rounds' agents read that as "the queue is empty"; a list of three gave it the first item only, and a
part with no text (an image) broke the call. The Tool step path guarded the empty case (steps.py) but read only the
first part too. Both now read a result one way (`tool_result.mcp_text`): no content is its structured content or "",
one part its text, several the list the tool returned (each part JSON) or their texts one per line, and a part with
no text is named.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.tools import VirtualTool

QUEUE = VirtualTool("list_queue", "lists the orders waiting", {"type": "object", "properties": {}}, None)
ITEMS = ['{"order": "A1"}', '{"order": "B2"}', '{"order": "C3"}']


def _text(text: str):
    return types.SimpleNamespace(type="text", text=text)


#: A part with no text, as an image's is.
IMAGE = types.SimpleNamespace(type="image", data="aGk=", mimeType="image/png")


class _Session:
    def __init__(self, content: list):
        self.content = content

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        return types.SimpleNamespace(content=list(self.content), isError=False)


def _server(monkeypatch, content: list):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        ollama = [{"type": "function", "function": {"name": QUEUE.name, "description": QUEUE.description,
                                                    "parameters": QUEUE.inputSchema}}]
        return [QUEUE], {QUEUE.name: QUEUE.inputSchema}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    return types.SimpleNamespace(agent_sessions={"fake": _Session(content)}, memory_store=None,
                                 tool_router={"list_queue": ("fake", "list_queue")})


async def _agent_saw(monkeypatch, fake_llm, content: list) -> str:
    """What the agent was handed after calling the tool: the model's next input, whole."""
    from core.react_engine import run_agent_step

    server = _server(monkeypatch, content)
    a = S.make_agent(tools=["list_queue"], skip_default_tools=True)
    fake_llm.script([tool_call("list_queue"), "Done."])
    events = [ev async for ev in run_agent_step(message="What is waiting in the queue?", agent_id=a["id"],
                                                session_id="s_f1344", server_module=server, agent_override=a,
                                                max_turns=4)]
    assert [e for e in events if e.get("type") == "final"][0]["response"] == "Done."
    assert len(fake_llm.calls) >= 2, fake_llm.calls
    return json.dumps(fake_llm.calls[1:], default=str, ensure_ascii=False)


async def _step_read(monkeypatch, content: list) -> str:
    from core.orchestration.steps import ToolStepExecutor

    engine = types.SimpleNamespace(server_module=_server(monkeypatch, content))
    return await ToolStepExecutor()._execute_tool("list_queue", {}, engine, run_id="run_f1344")


class TestTheAgent:
    async def test_a_tool_that_returned_nothing_is_an_empty_answer_never_an_index_error(self, monkeypatch, fake_llm,
                                                                                       client):
        saw = await _agent_saw(monkeypatch, fake_llm, [])
        assert "list index out of range" not in saw and "IndexError" not in saw, saw

    async def test_a_list_of_three_reaches_it_whole(self, monkeypatch, fake_llm, client):
        saw = await _agent_saw(monkeypatch, fake_llm, [_text(t) for t in ITEMS])
        assert all(order in saw for order in ("A1", "B2", "C3")), saw

    async def test_a_part_with_no_text_is_named_and_the_rest_read(self, monkeypatch, fake_llm, client):
        saw = await _agent_saw(monkeypatch, fake_llm, [IMAGE, _text("2 orders are waiting")])
        assert "2 orders are waiting" in saw and "image/png" in saw and "has no attribute" not in saw, saw


class TestAToolStep:
    async def test_a_tool_that_returned_nothing_writes_an_empty_answer(self, monkeypatch, client):
        assert await _step_read(monkeypatch, []) == ""

    async def test_a_list_of_three_is_the_list(self, monkeypatch, client):
        assert json.loads(await _step_read(monkeypatch, [_text(t) for t in ITEMS])) == [json.loads(t) for t in ITEMS]

    async def test_parts_that_are_not_json_are_one_per_line(self, monkeypatch, client):
        assert await _step_read(monkeypatch, [_text("first"), _text("second")]) == "first\nsecond"

    async def test_a_part_with_no_text_is_named(self, monkeypatch, client):
        assert await _step_read(monkeypatch, [IMAGE, _text("2 orders are waiting")]) == (
            "[image (image/png) not shown]\n2 orders are waiting")

    @pytest.mark.parametrize("one", ['{"order": "A1"}', "plain words"])
    async def test_one_part_is_its_text_as_before(self, monkeypatch, client, one):
        assert await _step_read(monkeypatch, [_text(one)]) == one
