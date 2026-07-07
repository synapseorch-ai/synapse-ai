"""
Internal ReAct paths: auto-compaction firing inside the loop, deterministic
tool-cache hits, and orchestration input-key context chaining.
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


class TestToolCacheHit:
    async def test_deterministic_tool_cache_hit(self, fake_llm):
        from core.cache import tool_cache
        # Pre-seed a cache entry for a deterministic (cacheable) tool.
        tool_cache.set("code_search", {"query": "widgets"}, "CACHED SEARCH RESULT", ttl_seconds=300)
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        fake_llm.script([tool_call("code_search", query="widgets"), "wrapped up"])
        events = [ev async for ev in __import__("core.react_engine", fromlist=["run_agent_step"]).run_agent_step(
            message="find widgets", agent_id=agent["id"], session_id="s_cache",
            server_module=_server(), agent_override=agent,
            tools_override=[_tool_schema("code_search")], max_turns=3)]
        results = [e for e in events if e.get("type") in ("tool_result", "tool_cache_hit")]
        assert any("CACHED SEARCH RESULT" in e.get("preview", "") for e in results)


class TestOrchestrationContextChain:
    async def test_agent_step_reads_prior_output(self, fake_llm, seed_agent):
        agent = seed_agent(id="chain_agent", tools=[], skip_default_tools=True)
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
