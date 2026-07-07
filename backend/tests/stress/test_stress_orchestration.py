"""
Stress: many concurrent orchestration runs (LLM + print steps) through the REAL
engine under the 5-90s fake-LLM latency profile. Verifies concurrent runs don't
interfere and all complete.
"""
import types

import pytest

from _fakes import seed as S
from load_harness import run_load

pytestmark = pytest.mark.stress


def _server_module():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def test_concurrent_orchestration_runs(fake_llm, stress_params):
    fake_llm.set_default("draft")
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    def _orch(i: int) -> dict:
        return S.make_orchestration(
            id=f"orch_stress_{i}",
            entry_step_id="s_llm",
            steps=[
                {"id": "s_llm", "name": "Draft", "type": "llm",
                 "prompt_template": "About {state.user_input}", "output_key": "draft",
                 "model": "claude-test", "next_step_id": "s_print"},
                {"id": "s_print", "name": "Show", "type": "print",
                 "print_content": "R: {state.draft}", "output_key": "shown",
                 "next_step_id": None},
            ],
        )

    async def task(i: int):
        orch = Orchestration.model_validate(_orch(i))
        engine = OrchestrationEngine(orch, _server_module())
        completed = False
        async for ev in engine.run(f"topic {i}", run_id=f"run_stress_{i}"):
            if ev.get("type") == "orchestration_complete":
                completed = True
        assert completed, f"run {i} did not complete"

    metrics = await run_load(
        "orchestration", task,
        total=stress_params["total"], concurrency=stress_params["concurrency"],
    )
    assert metrics["failed"] == 0, metrics["sample_errors"]
    assert metrics["succeeded"] == stress_params["total"]
