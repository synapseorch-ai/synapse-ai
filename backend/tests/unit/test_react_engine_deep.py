"""
Deep coverage of core.react_engine.run_agent_step: the tool-execution loop,
reasoning projection, blocked/unavailable tools, the max-turns guard, LLM error
handling, and delegate-agent context injection — all driven by the fake LLM.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _tool_schema(name="my_tool"):
    return {"type": "function", "function": {
        "name": name, "description": "test tool",
        "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}}}}


async def _drive(agent, fake_llm, script, *, tools_override=None, tool_executor=None,
                 max_turns=5, message="go"):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    events = []
    async for ev in run_agent_step(
        message=message, agent_id=agent.get("id"), session_id="s1",
        server_module=_server(), agent_override=agent,
        tools_override=tools_override, tool_executor=tool_executor, max_turns=max_turns,
    ):
        events.append(ev)
    return events


def _types(events):
    return [e.get("type") for e in events]


class TestToolLoop:
    async def test_tool_executed_then_final(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        calls = []

        async def executor(name, args):
            calls.append((name, args))
            return "TOOL-OUTPUT-42"

        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1), "The final answer."],
            tools_override=[_tool_schema()], tool_executor=executor,
        )
        assert calls == [("my_tool", {"x": 1})]
        results = [e for e in events if e.get("type") == "tool_result"]
        assert results and "TOOL-OUTPUT-42" in results[-1]["preview"]
        finals = [e for e in events if e.get("type") == "final"]
        assert finals and finals[-1]["response"] == "The final answer."

    async def test_unavailable_tool_is_blocked(self, fake_llm):
        # Agent allows no tools -> a tool call is blocked, then it wraps up.
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1), "wrapped up"],
            tools_override=[_tool_schema()], tool_executor=None,
        )
        previews = [e.get("preview", "") for e in events if e.get("type") == "tool_result"]
        assert any("not available" in p.lower() or "blocked" in p.lower() for p in previews)


class TestReasoningAndThought:
    async def test_reasoning_block_projected(self, fake_llm):
        agent = S.make_agent(tools=[], skip_default_tools=True)
        events = await _drive(
            agent, fake_llm,
            ["[REASONING]thinking hard[/REASONING]\nHere is the answer."],
            tools_override=[],
        )
        reasoning = [e for e in events if e.get("type") == "llm_reasoning"]
        assert reasoning and "thinking hard" in reasoning[0]["reasoning"]
        finals = [e for e in events if e.get("type") == "final"]
        assert "Here is the answer." in finals[-1]["response"]


class TestErrorAndGuards:
    async def test_llm_exception_yields_error_event(self, fake_llm, monkeypatch):
        agent = S.make_agent(tools=[], skip_default_tools=True)

        async def _boom(*a, **k):
            raise RuntimeError("model unreachable")

        import core.react_engine as re
        monkeypatch.setattr(re, "llm_generate_response", _boom)
        from core.react_engine import run_agent_step
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[], max_turns=3)]
        assert any(e.get("type") == "error" for e in events)

    async def test_max_turns_guard(self, fake_llm):
        # Keep calling a tool forever -> the max_turns guard must stop the loop.
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "again"

        # Script more tool calls than max_turns allows.
        events = await _drive(
            agent, fake_llm,
            [tool_call("my_tool", x=1)] * 6,
            tools_override=[_tool_schema()], tool_executor=executor, max_turns=2,
        )
        # Loop terminated (a final/summary is produced rather than looping forever).
        assert any(e.get("type") in ("final", "error") for e in events)
        assert fake_llm.call_count <= 4  # bounded by the guard, not the script length


class TestToolLoopVariants:
    async def test_multiple_tool_calls_in_one_turn(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        seen = []

        async def executor(name, args):
            seen.append(name)
            return f"out-{name}"

        # Two tool-call JSON objects in a single LLM response, then a final.
        two_calls = tool_call("tool_a", x=1) + "\n" + tool_call("tool_b", y=2)
        events = await _drive(
            agent, fake_llm, [two_calls, "all done"],
            tools_override=[_tool_schema("tool_a"), _tool_schema("tool_b")],
            tool_executor=executor,
        )
        assert seen == ["tool_a", "tool_b"]
        assert any(e.get("type") == "final" for e in events)

    async def test_post_tool_hook_emits_extra_events(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "ok"

        async def hook(name, raw_output):
            yield {"type": "custom_hook_event", "tool": name}

        from core.react_engine import run_agent_step
        fake_llm.script([tool_call("my_tool", x=1), "done"])
        events = [ev async for ev in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[_tool_schema()],
            tool_executor=executor, post_tool_hook=hook, max_turns=3)]
        assert any(e.get("type") == "custom_hook_event" for e in events)

    async def test_history_override_and_images(self, fake_llm):
        agent = S.make_agent(tools=[], skip_default_tools=True)
        from core.react_engine import run_agent_step
        fake_llm.script(["final with context"])
        events = [ev async for ev in run_agent_step(
            message="describe", agent_id=agent["id"], session_id="s1", server_module=_server(),
            agent_override=agent, tools_override=[],
            history_override=[{"role": "user", "content": "earlier"},
                              {"role": "assistant", "content": "reply"}],
            images=["data:image/png;base64,iVBORw0KGgo="], max_turns=2)]
        assert any(e.get("type") == "final" for e in events)
        # The fake LLM received the overridden history + image on the first turn.
        assert fake_llm.last_call.get("history_messages")


class TestDelegateContext:
    async def test_delegate_agent_builds_roster(self, fake_llm, seed_agent):
        target = seed_agent(id="worker", name="Worker", description="does work")
        delegate = S.make_agent(id="lead", name="Lead", type="delegate",
                                 delegate_agent_ids=[target["id"]], tools=["all"])
        S.seed_agents([target, delegate])
        # No tools_override -> the real delegate-context injection path runs.
        from core.react_engine import run_agent_step
        fake_llm.script(["All delegated, done."])
        events = [ev async for ev in run_agent_step(
            message="handle this", agent_id="lead", session_id="s1",
            server_module=_server(), max_turns=2)]
        assert any(e.get("type") == "final" for e in events)
