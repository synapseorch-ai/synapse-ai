"""
An agent and its tools are resolved once per run, not once per step.

Every counter here is a spy on the seam the engine already crosses: the
resource provider (`resolve_agent`, `resolve_custom_tools`), the tool
aggregation, and the tenant variables document.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server(**over):
    base = dict(agent_sessions={}, memory_store=None, tool_router={})
    base.update(over)
    return types.SimpleNamespace(**base)


class _Counters:
    """Count the resolution calls without changing what they return."""

    def __init__(self, monkeypatch):
        import core.orchestration.resolved as resolved_mod
        import core.react_engine as re_mod
        import core.scale.context as ctx_mod
        import core.variables as vars_mod
        self.n = {"resolve_agent": 0, "aggregate_all_tools": 0, "variables.load": 0,
                  "resolve_custom_tools": 0, "resolve_orchestration": 0}

        def counting(name, real):
            async def wrapper(*a, **kw):
                self.n[name] += 1
                return await real(*a, **kw)
            return wrapper

        monkeypatch.setattr(ctx_mod, "resolve_agent", counting("resolve_agent", ctx_mod.resolve_agent))
        monkeypatch.setattr(ctx_mod, "resolve_custom_tools",
                            counting("resolve_custom_tools", ctx_mod.resolve_custom_tools))
        monkeypatch.setattr(ctx_mod, "resolve_orchestration",
                            counting("resolve_orchestration", ctx_mod.resolve_orchestration))
        monkeypatch.setattr(vars_mod, "load", counting("variables.load", vars_mod.load))
        # Bound at import in both modules that call it.
        counted_agg = counting("aggregate_all_tools", re_mod.aggregate_all_tools)
        monkeypatch.setattr(re_mod, "aggregate_all_tools", counted_agg)
        import core.tools as tools_mod
        monkeypatch.setattr(tools_mod, "aggregate_all_tools", counted_agg)


async def _run(orch_dict, server=None, run_id="run_reuse"):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), server or _server())
    events = [ev async for ev in engine.run("go", run_id=run_id)]
    return events, engine


def _chain(agent_ids: list[str]):
    """One agent step per id, chained."""
    steps = []
    for i, aid in enumerate(agent_ids):
        steps.append({"id": f"s{i}", "name": f"Step {i}", "type": "agent", "agent_id": aid,
                      "prompt_template": "work", "output_key": f"o{i}", "max_turns": 4,
                      "next_step_id": f"s{i + 1}" if i + 1 < len(agent_ids) else None})
    return S.make_orchestration(id="orch_reuse", entry_step_id="s0", steps=steps)


class TestOnePerRun:
    async def test_three_steps_on_one_agent_resolve_once(self, fake_llm, monkeypatch):
        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.set_default("done")
        c = _Counters(monkeypatch)

        events, engine = await _run(_chain(["a1", "a1", "a1"]))

        assert any(e.get("type") == "orchestration_complete" for e in events)
        assert c.n["resolve_agent"] == 1
        assert c.n["aggregate_all_tools"] == 1
        assert c.n["resolve_custom_tools"] == 1
        # The engine loads the tenant variables once for the run and hands
        # them down; run_agent_step no longer reads them per step.
        assert c.n["variables.load"] == 1
        assert list(engine._resolved) == ["a1"]

    async def test_two_agents_two_entries(self, fake_llm, monkeypatch):
        await S.seed_agents([S.make_agent(id="a1"), S.make_agent(id="a2")])
        fake_llm.set_default("done")
        c = _Counters(monkeypatch)

        _, engine = await _run(_chain(["a1", "a2", "a1", "a2"]))

        assert sorted(engine._resolved) == ["a1", "a2"]
        assert c.n["resolve_agent"] == 2
        assert c.n["aggregate_all_tools"] == 2

    async def test_the_agents_tool_list_is_not_grown_by_the_defaults(self, fake_llm):
        """`aggregate_all_tools` appended the type defaults onto the agent's
        own `tools` list, in place, every time it ran."""
        await S.seed_agents([S.make_agent(id="a1", tools=["my_tool"])])
        fake_llm.set_default("done")

        _, engine = await _run(_chain(["a1", "a1", "a1"]))

        assert engine._resolved["a1"].agent["tools"] == ["my_tool"]

    async def test_each_step_starts_from_the_aggregated_set(self, fake_llm):
        """run_agent_step appends the task tool to what it is given; the second
        step must not see two of them."""
        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.set_default("done")

        await _run(_chain(["a1", "a1"]))

        for call in fake_llm.calls:
            names = [t["function"]["name"] for t in (call.get("tools") or [])]
            assert names.count("update_tasks") == 1, names


class TestTheCacheIsNotToolsOverride:
    """`tools_override` disables the task tool, skills and memory. The
    per-run cache must leave every one of those guards exactly as it was."""

    async def test_task_tool_is_still_declared(self, fake_llm):
        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.set_default("done")
        await _run(_chain(["a1"]))
        names = [t["function"]["name"] for t in fake_llm.calls[0]["tools"]]
        assert "update_tasks" in names

    async def test_memory_tools_are_still_declared(self, fake_llm):
        from core.agent_memory import MEMORY_TOOL_NAMES
        await S.seed_agents([S.make_agent(id="a1", tools=["all"], memory=True)])
        fake_llm.set_default("done")
        await _run(_chain(["a1"]))
        names = {t["function"]["name"] for t in fake_llm.calls[0]["tools"]}
        assert names & set(MEMORY_TOOL_NAMES)

    async def test_an_unknown_agent_still_fails_the_step(self, fake_llm):
        fake_llm.set_default("done")
        events, _ = await _run(_chain(["nope"]))
        assert any(e.get("type") in ("step_error", "orchestration_error") for e in events)
        assert not any(e.get("type") == "step_complete" for e in events)


class TestDispatchDoesNotReResolve:
    async def test_custom_tools_are_listed_once_however_many_calls(self, fake_llm, monkeypatch):
        """A call to a tool the router does not know consulted the custom-tool
        list — read from the store — on every such call."""
        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.script([tool_call("ghost", n=1), tool_call("ghost", n=2), tool_call("ghost", n=3), "done"])
        c = _Counters(monkeypatch)
        await _run(_chain(["a1"]))
        assert fake_llm.call_count == 4
        assert c.n["resolve_custom_tools"] == 1


class TestResolveOneTool:
    def _server_with_mcp(self):
        from core.tools import VirtualTool
        return _server(
            agent_sessions={"srv": object()},
            tool_router={"srv__my_tool": ("srv", "my_tool")},
            _session_tools={"srv": [VirtualTool("my_tool", "does things", {"type": "object"})]},
        )

    async def test_finds_an_mcp_tool_without_aggregating(self, monkeypatch):
        from core.tools import resolve_one_tool
        import core.tools as tools_mod

        async def boom(*a, **k):
            raise AssertionError("aggregate_all_tools must not run")
        monkeypatch.setattr(tools_mod, "aggregate_all_tools", boom)

        tool = await resolve_one_tool(self._server_with_mcp(), "my_tool", {"tools": ["all"]}, [])
        assert tool is not None and tool.name == "my_tool" and tool.description == "does things"

    async def test_respects_the_agents_allowlist(self):
        from core.tools import resolve_one_tool
        agent = {"tools": ["other"], "skip_default_tools": True}
        assert await resolve_one_tool(self._server_with_mcp(), "my_tool", agent, []) is None

    async def test_finds_a_custom_tool(self):
        from core.tools import resolve_one_tool
        custom = [{"name": "webhook", "description": "posts", "inputSchema": {"type": "object"}}]
        tool = await resolve_one_tool(_server(), "webhook", {"tools": ["all"]}, custom)
        assert tool is not None and tool.name == "webhook"

    async def test_finds_a_builder_tool(self):
        from core.tools import resolve_one_tool
        from core.builder_tools import BUILDER_TOOL_SCHEMAS
        name = BUILDER_TOOL_SCHEMAS[0]["function"]["name"]
        tool = await resolve_one_tool(_server(), name, {"tools": ["all"]}, [])
        assert tool is not None and tool.name == name

    async def test_unknown_is_none(self):
        from core.tools import resolve_one_tool
        assert await resolve_one_tool(_server(), "nothing", {"tools": ["all"]}, []) is None

    async def test_the_tool_step_uses_it(self, fake_llm, monkeypatch):
        import core.tools as tools_mod

        async def boom(*a, **k):
            raise AssertionError("the tool step aggregated every tool to find one")
        monkeypatch.setattr(tools_mod, "aggregate_all_tools", boom)

        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.set_default(tool_call("my_tool", x=1))
        orch = S.make_orchestration(
            id="orch_toolstep", entry_step_id="t",
            steps=[{"id": "t", "name": "Call", "type": "tool", "forced_tool": "my_tool",
                    "agent_id": "a1", "prompt_template": "call it", "output_key": "o",
                    "max_turns": 1, "next_step_id": None}],
        )
        events, _ = await _run(orch, server=self._server_with_mcp())
        # The declaration was found; what happens on execution is the
        # session's business, not this test's.
        assert any("preparing to call 'my_tool'" in str(e.get("message", "")) for e in events)

    async def test_a_slow_tool_step_model_call_keeps_the_watcher_informed(self, fake_llm, monkeypatch):
        import core.model_wait as mw
        monkeypatch.setattr(mw, "HEARTBEAT_SECONDS", 0.05)
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MIN", "0.25")
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_MAX", "0.25")
        monkeypatch.setenv("SYNAPSE_FAKE_LLM_DELAY_PROB", "1")
        await S.seed_agents([S.make_agent(id="a1", tools=["all"])])
        fake_llm.set_default(tool_call("my_tool", x=1))
        orch = S.make_orchestration(
            id="orch_toolslow", entry_step_id="t",
            steps=[{"id": "t", "name": "Call", "type": "tool", "forced_tool": "my_tool",
                    "agent_id": "a1", "prompt_template": "call it", "output_key": "o",
                    "max_turns": 1, "next_step_id": None}],
        )
        events, _ = await _run(orch, server=self._server_with_mcp())
        beats = [e for e in events if e.get("type") == "thinking"
                 and "waiting on the model" in str(e.get("message", ""))]
        assert len(beats) >= 2, [e.get("type") for e in events]

class TestNestedOrchestration:
    async def test_the_sub_orchestration_is_resolved_by_id(self, fake_llm, monkeypatch):
        """It used to load every orchestration in the tenant and scan."""
        import core.routes.orchestrations as orch_routes

        async def boom():
            raise AssertionError("load_orchestrations must not run for a nested call")
        monkeypatch.setattr(orch_routes, "load_orchestrations", boom)

        sub = S.make_orchestration(id="orch_sub", entry_step_id="p", steps=[
            {"id": "p", "name": "P", "type": "print", "print_content": "sub done",
             "output_key": "sub_out", "next_step_id": None}])
        parent = S.make_orchestration(id="orch_parent", entry_step_id="s", steps=[
            {"id": "s", "name": "Delegate", "type": "agent", "agent_id": "orch_agent",
             "prompt_template": "go", "output_key": "result", "next_step_id": None}])
        await S.seed_orchestrations([sub, parent])
        await S.seed_agents([S.make_orchestrator_agent("orch_sub", id="orch_agent")])
        c = _Counters(monkeypatch)

        events, _ = await _run(parent, run_id="run_parent")

        assert any(e.get("type") == "orchestration_complete" for e in events)
        assert c.n["resolve_orchestration"] >= 1
        final = next(e for e in reversed(events) if e.get("type") == "orchestration_complete")
        assert final["final_state"]["result"] == "sub done"
