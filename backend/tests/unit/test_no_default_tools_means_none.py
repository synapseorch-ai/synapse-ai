"""An agent with "Default tools" off cannot run a default tool by calling it by name (F462).

`skip_default_tools` (F414's "Default tools" switch) keeps the type's default
tools — read_file, list_files, search_files… — out of what the agent is offered.
But the dispatch guard admitted a call by name to anything in `always_allowed`,
which added the type's defaults whatever the switch said. Measured by C9: an
agent with `tools=["write_note"]` and the switch off, scripted to call
`list_files`, got the call run (no refusal) — a tightly scoped agent could read
the workspace's files. `_spawn_grantable` inherited the same set.

Now with the switch off the type's defaults are not in `always_allowed`: such a
call is refused like any tool the agent does not have. What the agent is given
another way — a selected skill's loader, the memory tools, its knowledge
bases' tools — stays admitted. With the switch on, nothing changes.
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

WRITE_NOTE = {"type": "function", "function": {
    "name": "write_note", "description": "Write a note.",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _turn(fake_llm, agent, script, tools_override=(WRITE_NOTE,)):
    from core.react_engine import run_agent_step
    ran: list[str] = []

    async def executor(name, args):
        ran.append(name)
        return f"{name} ran"

    fake_llm.script(script)
    events = [ev async for ev in run_agent_step(
        message="Note that the sky is blue.", agent_id=agent["id"], session_id=f"s_f462_{agent['id']}",
        server_module=_server(), agent_override=agent, tool_executor=executor,
        tools_override=list(tools_override) if tools_override is not None else None, max_turns=4)]
    results = {e.get("tool_name"): e for e in events if e.get("type") == "tool_result"}
    return ran, results


class TestDefaultToolsOff:
    async def test_a_default_tool_called_by_name_is_refused(self, fake_llm):
        agent = S.make_agent(tools=["write_note"], skip_default_tools=True)
        ran, results = await _turn(fake_llm, agent, [tool_call("list_files", path="."), "Done."])

        assert "list_files" not in ran, "the call never reached a tool"
        assert results["list_files"].get("refused") is True

    async def test_its_own_tool_still_runs(self, fake_llm):
        agent = S.make_agent(tools=["write_note"], skip_default_tools=True)
        ran, _ = await _turn(fake_llm, agent, [tool_call("write_note", text="blue"), "Done."])
        assert ran == ["write_note"]

    async def test_the_memory_tools_it_was_given_stay_admitted(self, fake_llm):
        """Memory is the agent's by its switch, not its tool list (the engine offers it
        only with no `tools_override`)."""
        agent = S.make_agent(tools=[], skip_default_tools=True, memory=True)
        _, results = await _turn(fake_llm, agent, [tool_call("read_memory"), "Done."], tools_override=None)
        assert "read_memory" in results
        assert "Tool not available for this agent" not in str(results["read_memory"].get("preview"))


class TestDefaultToolsOn:
    async def test_a_default_tool_called_by_name_runs_as_before(self, fake_llm):
        agent = S.make_agent(tools=["write_note"])
        ran, results = await _turn(fake_llm, agent, [tool_call("list_files", path="."), "Done."])
        assert not results["list_files"].get("refused")
        assert "list_files" in ran
