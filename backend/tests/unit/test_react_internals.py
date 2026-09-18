"""
Internal ReAct paths: auto-compaction firing inside the loop, a tool result
never served from a cache, and orchestration input-key context chaining.
"""
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _tool_schema(name):
    return {"type": "function", "function": {
        "name": name, "description": "t",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}


class TestCompaction:
    async def test_compaction_fires_in_loop(self, fake_llm, monkeypatch):
        import core.react_engine as re
        monkeypatch.setattr(re, "load_settings", lambda: {
            "model": "mistral", "auto_compact_enabled": True, "auto_compact_threshold": 50})
        fake_llm.set_default("final answer")
        agent = S.make_agent(tools=[], skip_default_tools=True)
        big_history = [{"role": "user", "content": "x" * 400},
                       {"role": "assistant", "content": "y" * 400}]
        events = [ev async for ev in re.run_agent_step(
            message="continue", agent_id=agent["id"], session_id="s_compact",
            server_module=_server(), agent_override=agent, tools_override=[],
            history_override=big_history, max_turns=1)]
        assert any(e.get("type") == "final" for e in events)


class _FormSession:
    """An MCP session serving `collect_data`, counting calls."""

    def __init__(self):
        self.calls = 0

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, read_timeout_seconds=None):
        import json
        self.calls += 1
        form = {"title": "Details", "fields": [{"name": "email", "type": "text"}]}
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=json.dumps(form))])


class TestNoToolResultIsServedTwice:
    async def test_the_same_form_asked_for_twice_is_a_form_both_times(self, fake_llm):
        """F67: the tool cache served `collect_data` — the one tool its hand list
        and the real tool names agreed on — and a hit skipped the step that
        marks the reply as a form, so the second person asked got text instead.
        The cache is gone; every call reaches the tool."""
        from core.react_engine import run_agent_step
        session = _FormSession()
        server = types.SimpleNamespace(
            agent_sessions={"forms": session}, memory_store=None,
            tool_router={"collect_data": ("forms", "collect_data")},
        )
        agent = S.make_agent(tools=["all"], skip_default_tools=True)

        finals = []
        for conversation in ("s_form_1", "s_form_2"):
            fake_llm.script([tool_call("collect_data", title="Details"), "Please fill this in."])
            events = [ev async for ev in run_agent_step(
                message="ask for their email", agent_id=agent["id"], session_id=conversation,
                server_module=server, agent_override=agent,
                tools_override=[_tool_schema("collect_data")], max_turns=3)]
            finals.append(next(e for e in reversed(events) if e.get("type") == "final"))

        assert [f.get("intent") for f in finals] == ["collect_data", "collect_data"]
        assert session.calls == 2
        assert not any(e.get("type") == "tool_cache_hit" for e in events)


class TestTheCacheFieldsAreGone:
    def test_a_step_saved_with_the_old_toggle_still_loads(self):
        """Nothing has read them since F67, and batch 3 removed the step
        panel's toggle, their last writer — so the fields went too. A stored
        orchestration that still carries them loads (unknown keys are
        ignored), and its next save drops them."""
        from core.models_orchestration import StepConfig
        step = StepConfig.model_validate({
            "id": "s", "name": "S", "type": "agent",
            "cache_tools_enabled": False, "cache_tool_ttl_seconds": 60,
        })
        dumped = step.model_dump()
        assert "cache_tools_enabled" not in dumped
        assert "cache_tool_ttl_seconds" not in dumped


class TestOrchestrationContextChain:
    async def test_agent_step_reads_prior_output(self, fake_llm, seed_agent):
        agent = await seed_agent(id="chain_agent", tools=[], skip_default_tools=True)
        fake_llm.set_default("used the context")
        orch = S.make_orchestration(
            entry_step_id="p1",
            steps=[
                {"id": "p1", "name": "Produce", "type": "print",
                 "print_content": "DATA-123", "output_key": "produced", "next_step_id": "a1"},
                {"id": "a1", "name": "Consume", "type": "agent", "agent_id": agent["id"],
                 "prompt_template": "process {state.produced}", "input_keys": ["produced"],
                 "output_key": "consumed", "next_step_id": None},
            ],
        )
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine
        events = [ev async for ev in OrchestrationEngine(
            Orchestration.model_validate(orch), _server()).run("go", run_id="run_chain")]
        assert "orchestration_complete" in [e.get("type") for e in events]
        assert fake_llm.call_count >= 1
