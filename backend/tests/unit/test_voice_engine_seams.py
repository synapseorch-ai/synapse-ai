"""
The two engine seams a phone call needs (VOX1a).

* `extra_tools`: a call's `transfer_call` / `end_call` are the surface's own
  tools, offered even to a visitor with nothing ticked, run by the caller's
  handler, and never handed to anything else.
* `VisitorScope.approvals="park"`: a caller's `confirm` tool asks a person
  (V16) instead of being refused. Every other visitor still gets the refusal
  (F280), which `test_visitor_scope.py` keeps pinning.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core import visitor
from core.tools import VirtualTool
from core.visitor import VisitorScope

def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def one_tool(monkeypatch):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools):
        ollama = [{"type": "function", "function": {"name": DANGER.name, "description": DANGER.description,
                                                    "parameters": DANGER.inputSchema}}]
        return [DANGER], {DANGER.name: DANGER.inputSchema}, ollama, str([DANGER.name])

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)


async def _run(fake_llm, script, agent, scope=None, extra_tools=None, executed=None):
    from core.react_engine import run_agent_step

    async def executor(name, args):
        if executed is not None:
            executed.append(name)
        return f"ran-{name}"

    fake_llm.script(script)
    with visitor.scope(scope):
        return [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="voice_chn_x_CA1", server_module=_server(),
            agent_override=agent, tool_executor=executor, max_turns=6, extra_tools=extra_tools)]


def _offered(fake_llm, i=0) -> set[str]:
    return {t["function"]["name"] for t in (fake_llm.calls[i].get("tools") or [])}


def _hang_up_tool(seen):
    async def handler(args):
        seen.append(args)
        return "The call ends after this answer."
    return (VirtualTool(name="end_call", description="End the call.",
                        inputSchema={"type": "object", "properties": {}}), handler)


class TestTheCallersOwnTools:
    async def test_offered_to_a_visitor_with_nothing_ticked_and_run_by_its_handler(self, fake_llm, one_tool):
        seen, executed = [], []
        events = await _run(fake_llm, [tool_call("end_call"), "Goodbye."], S.make_agent(tools=["all"]),
                            VisitorScope("channel", frozenset()), [_hang_up_tool(seen)], executed)
        assert "end_call" in _offered(fake_llm)
        assert "danger" not in _offered(fake_llm), "the visitor narrowing still holds"
        assert seen == [{}]
        assert executed == [], "the handler answered, not the generic executor"
        assert any(e["type"] == "tool_result" and e["tool_name"] == "end_call"
                   and "ends after this answer" in e["preview"] for e in events)
        assert [e for e in events if e["type"] == "final"][-1]["response"] == "Goodbye."

    async def test_a_handler_that_raises_is_an_observation_not_a_crash(self, fake_llm, one_tool):
        async def broken(args):
            raise RuntimeError("line is busy")
        tool = (VirtualTool(name="end_call", description="x", inputSchema={"type": "object"}), broken)
        events = await _run(fake_llm, [tool_call("end_call"), "ok"], S.make_agent(tools=["all"]),
                            extra_tools=[tool])
        assert any(e["type"] == "tool_result" and e["preview"] == "Error: line is busy" for e in events)

    async def test_never_shadows_a_tool_the_agent_holds(self, fake_llm, one_tool):
        seen, executed = [], []

        async def impostor(args):
            seen.append(args)
            return "impostor"
        tool = (VirtualTool(name="danger", description="x", inputSchema={"type": "object"}), impostor)
        await _run(fake_llm, [tool_call("danger"), "done"], S.make_agent(tools=["all"]),
                   extra_tools=[tool], executed=executed)
        assert seen == [] and executed == ["danger"]

    async def test_without_them_a_turn_offers_what_it_did(self, fake_llm, one_tool):
        await _run(fake_llm, ["done"], S.make_agent(tools=["all"]), VisitorScope("channel", frozenset()))
        assert _offered(fake_llm) <= {"update_tasks", "load_skill"}


class TestACallerIsAskedForNotRefused:
    async def test_park_asks_a_person(self, fake_llm, one_tool):
        executed = []
        agent = S.make_agent(tools=["all"], tool_policy={"destructive": "confirm"})
        events = await _run(fake_llm, [tool_call("danger"), "done"], agent,
                            VisitorScope("channel", frozenset({"danger"}), approvals="park"), executed=executed)
        assert executed == []
        assert events[-1]["type"] == "human_input_required"
        assert events[-1]["kind"] == "tool_approval" and events[-1]["tool_name"] == "danger"

    async def test_deny_is_still_the_default(self, fake_llm, one_tool):
        agent = S.make_agent(tools=["all"], tool_policy={"destructive": "confirm"})
        events = await _run(fake_llm, [tool_call("danger"), "done"], agent,
                            VisitorScope("channel", frozenset({"danger"})))
        assert not any(e["type"] == "human_input_required" for e in events)
        assert any("Blocked by policy" in (e.get("preview") or "") for e in events if e["type"] == "tool_result")


class TestTheScopeTravels:
    def test_park_survives_the_fleet_round_trip(self):
        scope = VisitorScope("channel", frozenset({"a"}), approvals="park")
        assert VisitorScope.from_dict(scope.to_dict()) == scope

    def test_a_deny_scope_dict_is_unchanged(self):
        """What the widget and channels send the fleet today, byte for byte."""
        assert VisitorScope("widget", frozenset({"b", "a"})).to_dict() == {"surface": "widget", "tools": ["a", "b"]}

    def test_an_unknown_approvals_value_fails_closed(self):
        assert VisitorScope.from_dict({"surface": "channel", "approvals": "allow"}).approvals == "deny"
