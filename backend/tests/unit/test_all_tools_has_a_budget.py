"""What `"all"` adds to a turn has a budget (F483).

An agent made with no `tools` key holds `["all"]`, and `"all"` reaches every tool of every server the workspace
connected. Nothing bounded that, and a server can grow without anyone in the workspace doing anything: acme's Vercel
server went from the 37 tools its discovery stored to 236 live, and an `["all"]` turn there sent 343 tools, 169,391
tokens a call (measured, `MEMORY/drafts/bf6/F483.md`). On nova-micro (128K) Bedrock refused every call ("Input Tokens
Exceeded"), which the run read as "refused as invalid"; on a model that can read it, every call paid for it, unseen.

So a turn's tools that come only through `"all"` from connected servers may add at most `ALL_TOOLS_TOKEN_BUDGET`
tokens. Over it, whole servers are left out, largest first, and the turn says which, once, where the run log and
chat's activity line show it. A tool the agent named is never left out, and nor is a native or builder tool. A caller
that asks what exists (the builder's listing, F489's guard) passes no budget and sees everything, as before.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.tool_router import ToolRouter
from core.tools import ALL_TOOLS_TOKEN_BUDGET, aggregate_all_tools, declaration_tokens


class _Tool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema if schema is not None else {"type": "object", "properties": {}}


class _Session:
    def __init__(self, *tools):
        self._tools = list(tools)

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


def _big_schema(i: int) -> dict:
    # About 2,500 characters, the size of a Vercel tool with its option descriptions.
    return {"type": "object", "properties": {
        f"option_{j}": {"type": "string", "description": f"Option {j} of tool {i}: " + "x" * 120}
        for j in range(16)}, "required": ["option_0"]}


def _server_module():
    sessions = {
        "ext_mcp_vercel": _Session(*[_Tool(f"tool_{i}", f"Vercel tool {i}.", _big_schema(i)) for i in range(236)]),
        "ext_mcp_deepwiki": _Session(*[_Tool(n, f"DeepWiki {n}.") for n in ("ask_wiki_question", "read_wiki_contents",
                                                                           "read_wiki_structure")]),
        "time": _Session(_Tool("get_current_time", "The time now.")),
    }
    router = ToolRouter()
    for session_name, session in sessions.items():
        external = session_name.startswith("ext_mcp_")
        namespace = session_name[len("ext_mcp_"):] if external else session_name
        for tool in session._tools:
            router.register(namespace, tool.name, session_key=session_name, alias=not external)
    return types.SimpleNamespace(agent_sessions=sessions, tool_router=router, memory_store=None, _session_tools={})


def _vercel_tokens() -> int:
    return sum(declaration_tokens(f"vercel__tool_{i}", f"Vercel tool {i}.", _big_schema(i)) for i in range(236))


def test_the_test_server_is_past_the_budget():
    assert _vercel_tokens() > ALL_TOOLS_TOKEN_BUDGET


def test_declaration_tokens_reads_a_tool_as_the_model_does():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    assert declaration_tokens("x__y", "Find.", schema) == (len("x__y") + len("Find.") + len(json.dumps(schema))) // 4


async def test_a_server_past_the_budget_is_left_out_whole():
    left_out: list[dict] = []
    tools, schema_map, ollama_tools, tools_json = await aggregate_all_tools(
        _server_module(), {"tools": ["all"]}, [], budget=ALL_TOOLS_TOKEN_BUDGET, left_out=left_out)
    names = {t.name for t in tools}
    assert not any(n.startswith("vercel__") for n in names)
    assert {"deepwiki__ask_wiki_question", "deepwiki__read_wiki_contents", "get_current_time"} <= names
    # The four shapes agree: nothing left out reaches the model by another door.
    assert not any(n.startswith("vercel__") for n in schema_map)
    assert not any(t["function"]["name"].startswith("vercel__") for t in ollama_tools)
    assert "vercel__" not in tools_json
    assert left_out == [{"server": "vercel", "tools": 236, "tokens": _vercel_tokens()}]


async def test_a_named_tool_is_never_left_out():
    left_out: list[dict] = []
    tools, *_ = await aggregate_all_tools(
        _server_module(), {"tools": ["all", "vercel__tool_7"]}, [], budget=ALL_TOOLS_TOKEN_BUDGET, left_out=left_out)
    vercel = [t.name for t in tools if t.name.startswith("vercel__")]
    assert vercel == ["vercel__tool_7"]
    assert left_out[0]["server"] == "vercel" and left_out[0]["tools"] == 235


async def test_an_agent_naming_a_whole_server_gets_all_of_it():
    named = [f"vercel__tool_{i}" for i in range(236)]
    left_out: list[dict] = []
    tools, *_ = await aggregate_all_tools(
        _server_module(), {"tools": named}, [], budget=ALL_TOOLS_TOKEN_BUDGET, left_out=left_out)
    assert sum(t.name.startswith("vercel__") for t in tools) == 236
    assert left_out == []


async def test_under_the_budget_nothing_is_left_out():
    module = _server_module()
    del module.agent_sessions["ext_mcp_vercel"]
    left_out: list[dict] = []
    tools, *_ = await aggregate_all_tools(module, {"tools": ["all"]}, [], budget=ALL_TOOLS_TOKEN_BUDGET,
                                          left_out=left_out)
    assert "deepwiki__read_wiki_structure" in {t.name for t in tools}
    assert left_out == []


async def test_without_a_budget_every_tool_is_declared():
    # What the builder's listing and F489's guard ask: what exists, not what one turn sends.
    tools, *_ = await aggregate_all_tools(_server_module(), {"tools": ["all"]}, [])
    assert sum(t.name.startswith("vercel__") for t in tools) == 236


async def test_the_largest_server_goes_first():
    module = _server_module()
    # A second server past the budget on its own, but smaller than Vercel's.
    module.agent_sessions["ext_mcp_figma"] = _Session(
        *[_Tool(f"f_{i}", "Figma.", _big_schema(i)) for i in range(70)])
    for i in range(70):
        module.tool_router.register("figma", f"f_{i}", session_key="ext_mcp_figma", alias=False)
    left_out: list[dict] = []
    tools, *_ = await aggregate_all_tools(module, {"tools": ["all"]}, [], budget=ALL_TOOLS_TOKEN_BUDGET,
                                          left_out=left_out)
    assert [x["server"] for x in left_out] == ["vercel", "figma"]
    assert "deepwiki__ask_wiki_question" in {t.name for t in tools}


async def _turn(fake_llm, *, visitor_scope=None):
    from core import visitor
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"])
    fake_llm.script(["OK"])

    async def go():
        return [ev async for ev in run_agent_step(
            message="Say OK.", agent_id=agent["id"], session_id="s_f483", server_module=_server_module(),
            agent_override=agent, max_turns=2)]

    if visitor_scope is None:
        return await go()
    with visitor.scope(visitor_scope):
        return await go()


async def test_the_turn_leaves_the_server_out_and_says_so(fake_llm):
    events = await _turn(fake_llm)
    offered = [t["function"]["name"] for t in fake_llm.calls[0]["tools"]]
    assert offered and not any(n.startswith("vercel__") for n in offered)
    assert "deepwiki__ask_wiki_question" in offered
    notes = [e["message"] for e in events if e.get("type") == "status" and "vercel" in (e.get("message") or "")]
    assert len(notes) == 1, notes
    assert "236 tools" in notes[0] and "Left out" in notes[0]


async def test_a_visitor_is_not_told(fake_llm):
    from core import visitor

    events = await _turn(fake_llm, visitor_scope=visitor.VisitorScope("widget", frozenset()))
    assert not [e for e in events if e.get("type") == "status" and "Left out" in (e.get("message") or "")]


async def test_a_run_step_leaves_it_out_too():
    # The step's own path: tools aggregated once per run (`resolve_for_run`) carry what was left out.
    from core.orchestration.resolved import resolve_for_run

    agent = S.make_agent(tools=["all"])
    engine = types.SimpleNamespace(server_module=_server_module(), _custom_tools=[],
                                   _resolved={})
    from core.orchestration.resolved import ResolvedAgent
    engine._resolved[agent["id"]] = ResolvedAgent(agent=agent, custom_tools=[])
    entry = await resolve_for_run(engine, agent["id"])
    assert not any(t.name.startswith("vercel__") for t in entry.all_tools)
    assert entry.left_out == [{"server": "vercel", "tools": 236, "tokens": _vercel_tokens()}]


