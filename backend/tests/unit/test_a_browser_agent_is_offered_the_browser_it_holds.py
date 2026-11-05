"""A Browser agent is offered the browser it holds, however its tool list was saved (F1405; TRI1's read of BLD1's
take-over clip on v2.0; Naveen 11-06: "in v2.0", and the plan restated: one rule for the list and the guard).

A Browser agent made with its own list and no browser names in it (`"tools": []`, type browser, co_drive) holds the
browser at run time: `gate.held_by` adds the browser's tools to the run's guard (`react_engine._type_tools`). But what
the model is shown comes from `tools.effective_allowed_tools`, which added only the type's defaults, and there is no
browser entry among them, so the browser's tools were never declared. The agent tried `grep`, `read_file` and
`list_files` on a web page and made an answer up. The list it is offered now follows the guard's rule: a Browser agent
is offered the browser's tools, on an API model (its declared tools) and on a `cli.*` model (`tools_json`) alike.
Another type with an empty list is offered no browser; `"all"` reaches the browser as before (its server is not opt-in);
an agent's own tools stay.
"""
from __future__ import annotations

import types

import pytest

from core.browser import client as browser_client
from core.browser.gate import ALLOWED
from core.tool_router import ToolRouter
from core.tools import aggregate_all_tools, effective_allowed_tools, resolve_one_tool

BROWSER = sorted(ALLOWED)


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f"{name}."
        self.inputSchema = {"type": "object", "properties": {}}


class _Session:
    def __init__(self, *names):
        self._tools = [_Tool(n) for n in names]

    async def list_tools(self):
        return type("Result", (), {"tools": self._tools})()


def _module():
    built = {browser_client.SERVER_NAME: _Session(*BROWSER), "file_reader": _Session("read_file", "grep", "list_files")}
    router = ToolRouter()
    for name, session in built.items():
        for tool in session._tools:
            router.register(name, tool.name, session_key=name, alias=True)
    return types.SimpleNamespace(agent_sessions=built, tool_router=router, memory_store=None, _session_tools={})


def _agent(tools, kind="browser", **more) -> dict:
    return {"id": "portal_agent", "name": "Portal agent", "type": kind, "tools": tools,
            "browser": {"mode": "co_drive", "sites": ["portal.example.com"]}, **more}


async def _offered(agent: dict) -> tuple[set[str], set[str], str]:
    declared, _schemas, ollama_tools, tools_json = await aggregate_all_tools(_module(), agent, [])
    return ({t.name for t in declared}, {t["function"]["name"] for t in ollama_tools}, tools_json)


class TestABrowserAgent:
    async def test_with_an_empty_list_is_offered_every_browser_tool_on_either_kind_of_model(self):
        declared, api, tools_json = await _offered(_agent([]))
        assert set(BROWSER) <= declared, sorted(set(BROWSER) - declared)
        assert set(BROWSER) <= api, sorted(set(BROWSER) - api)
        # A cli.* model reads the tools as text in its prompt.
        assert all(f"'tool': '{name}'" in tools_json for name in BROWSER)

    async def test_its_own_tools_stay(self):
        declared, _api, _json = await _offered(_agent(["read_file"]))
        assert "read_file" in declared and set(BROWSER) <= declared

    async def test_one_that_opts_out_of_the_type_defaults_still_holds_the_browser(self):
        """`skip_default_tools` leaves out the defaults; the browser is what a Browser agent is, as the guard says."""
        declared, _api, _json = await _offered(_agent([], skip_default_tools=True))
        assert set(BROWSER) <= declared

    async def test_a_tool_step_finds_the_browsers_tool_for_it(self):
        assert await resolve_one_tool(_module(), "browser_navigate", _agent([]), []) is not None

    @pytest.mark.parametrize("tools", [[], ["read_file"], ["all"]])
    @pytest.mark.parametrize("kind", ["browser", "conversational"])
    def test_the_list_it_is_offered_holds_everything_its_guard_allows(self, tools, kind):
        """One rule: the run's guard (`react_engine._type_tools`) allows nothing the offered list leaves out."""
        from core.react_engine import _type_tools

        agent = _agent(tools, kind)
        assert set(_type_tools(agent)) <= set(effective_allowed_tools(agent))


class TestLeftAsItWas:
    async def test_another_type_with_an_empty_list_is_offered_no_browser(self):
        declared, api, tools_json = await _offered(_agent([], "conversational"))
        assert not set(BROWSER) & declared and not set(BROWSER) & api
        assert not any(f"'tool': '{name}'" in tools_json for name in BROWSER)
        assert await resolve_one_tool(_module(), "browser_navigate", _agent([], "conversational"), []) is None

    @pytest.mark.parametrize("kind", ["browser", "conversational"])
    async def test_all_reaches_the_browser_as_before(self, kind):
        declared, _api, _json = await _offered(_agent(["all"], kind))
        assert set(BROWSER) <= declared and {"read_file", "grep", "list_files"} <= declared
        assert effective_allowed_tools(_agent(["all"], kind))[0] == "all"
        assert not set(BROWSER) & set(effective_allowed_tools(_agent(["all"], kind)))

    async def test_another_types_own_list_is_its_own(self):
        declared, _api, _json = await _offered(_agent(["read_file", "browser_snapshot"], "conversational"))
        assert {"read_file", "browser_snapshot"} <= declared and "browser_click" not in declared


#: An API model, shown its tools as declarations, and a cli.* model, shown them in its prompt.
MODELS = [None, "cli.claude.claude-haiku-4-5"]


class TestOnAVisitorsTurn:
    """The browser a Browser agent now holds by its type is still held to a visitor's list (F280): a widget's or a
    channel's visitor is offered, may run and is served only what the owner ticked (`react_engine`'s `_admits`, and
    `mcp_needs.bounded` on the fleet). Before F1405 such an agent's browser was never offered, so no reader held this."""

    @staticmethod
    async def _turn(fake_llm, script, ticked, executed, model):
        from _fakes import seed as S
        from core import visitor
        from core.react_engine import run_agent_step
        from core.visitor import VisitorScope

        async def executor(name, args):
            executed.append(name)
            return f"ran-{name}"

        agent = S.make_agent(type="browser", tools=[], model=model,
                             browser={"mode": "co_drive", "sites": ["portal.example.com"]})
        fake_llm.script(script)
        with visitor.scope(VisitorScope("widget", frozenset(ticked))):
            return [ev async for ev in run_agent_step(
                message="go", agent_id=agent["id"], session_id="s_visitor", server_module=_module(),
                agent_override=agent, tool_executor=executor, max_turns=4)]

    @staticmethod
    def _seen(fake_llm, model) -> set[str]:
        """The browser tools the model was shown: an API model's declared tools, or a cli.* model's `tools_json`,
        which is written into its prompt."""
        call = fake_llm.calls[0]
        if model is None:
            return set(BROWSER) & {t["function"]["name"] for t in (call.get("tools") or [])}
        prompt = str(call.get("sys_prompt") or "")
        return {name for name in BROWSER if f"'tool': '{name}'" in prompt}

    @pytest.mark.parametrize("model", MODELS)
    async def test_nothing_ticked_offers_no_browser_and_a_call_to_it_does_not_run(self, fake_llm, model):
        from _fakes.fake_llm import tool_call

        executed: list[str] = []
        await self._turn(fake_llm, [tool_call("browser_navigate", url="https://portal.example.com"), "done"], (),
                         executed, model)
        assert self._seen(fake_llm, model) == set()
        assert executed == []

    @pytest.mark.parametrize("model", MODELS)
    async def test_one_ticked_offers_that_one_alone(self, fake_llm, model):
        executed: list[str] = []
        await self._turn(fake_llm, ["done"], ("browser_snapshot",), executed, model)
        assert self._seen(fake_llm, model) == {"browser_snapshot"}

    async def test_the_fleet_brings_no_browser_session_when_nothing_is_ticked(self):
        from core.scale import mcp_needs
        from core.visitor import VisitorScope

        agent = _agent([])
        member = await mcp_needs.for_agent(agent, None)
        assert member is mcp_needs.EVERY or set(BROWSER) <= set(member)
        nothing = await mcp_needs.for_agent(agent, VisitorScope("widget", frozenset()))
        assert nothing is not mcp_needs.EVERY and not set(BROWSER) & set(nothing), nothing
        one = await mcp_needs.for_agent(agent, VisitorScope("widget", frozenset({"browser_snapshot"})))
        assert one is not mcp_needs.EVERY and set(BROWSER) & set(one) == {"browser_snapshot"}, one
