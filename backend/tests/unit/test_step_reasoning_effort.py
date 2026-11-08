"""LLM, Tool and Evaluator steps think as hard as the workspace says (F77, D104).

An agent reads `reasoning_effort` off itself, then the workspace. The three step
kinds that call a model directly passed nothing, and `generate_response_ex`
hands `None` to the planner, which means "medium" — so a workspace set to off
still paid for thinking on every routing decision, and a switch-style model set
to high never thought in a step at all.
"""
import types

import httpx
import pytest

from _fakes import seed as S
from core.store.resources import save_tools


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


@pytest.fixture
def workspace_high(monkeypatch):
    import core.config as config
    monkeypatch.setattr(config, "load_settings", lambda: {"model": "mistral", "reasoning_effort": "high"})


async def _run(orch_dict):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    engine = OrchestrationEngine(Orchestration.model_validate(orch_dict), _server())
    return [e async for e in engine.run("hi", run_id=f"run_{orch_dict['id']}")]


class TestEveryStepKindInheritsTheWorkspace:
    async def test_llm_step(self, fake_llm, workspace_high):
        fake_llm.set_default("an answer")
        await _run(S.make_orchestration(
            id="orch_llm_effort", entry_step_id="l",
            steps=[{"id": "l", "name": "Write", "type": "llm", "prompt_template": "write",
                    "output_key": "o", "next_step_id": None}],
        ))
        assert fake_llm.calls and fake_llm.calls[0].get("reasoning_effort") == "high"

    async def test_evaluator_step(self, fake_llm, workspace_high):
        fake_llm.set_default('{"route": "done"}')
        await _run(S.make_orchestration(
            id="orch_eval_effort", entry_step_id="ev",
            steps=[{"id": "ev", "name": "Decide", "type": "evaluator",
                    "evaluator_prompt": "pick", "route_map": {"done": None},
                    "input_keys": [], "next_step_id": None}],
        ))
        assert fake_llm.calls and fake_llm.calls[0].get("reasoning_effort") == "high"

    async def test_tool_step(self, fake_llm, workspace_high, monkeypatch):
        async def fake_request(self, method, url, **kwargs):
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
        await save_tools([{
            "name": "ping", "description": "t", "tool_type": "http", "method": "POST",
            "url": "https://api.test/ping", "inputSchema": {"type": "object", "properties": {}},
        }])
        fake_llm.set_default('{"tool": "ping", "arguments": {}}')
        await _run(S.make_orchestration(
            id="orch_tool_effort", entry_step_id="t",
            steps=[{"id": "t", "name": "Ping", "type": "tool", "forced_tool": "ping",
                    "prompt_template": "call it", "output_key": "o", "max_turns": 1,
                    "next_step_id": None}],
        ))
        assert fake_llm.calls and fake_llm.calls[0].get("reasoning_effort") == "high"
