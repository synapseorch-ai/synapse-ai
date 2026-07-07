"""
Stress: many concurrent agent chats through the REAL ReAct loop while the fake
LLM injects random 5-90s latency on ~30% of calls. Asserts the system stays
correct under load (no errors, no dropped runs) and records latency/throughput.

Marked ``stress`` — excluded from the deploy gate; run in the nightly job or
locally with `-m stress`.
"""
import types

import pytest

from load_harness import run_load

pytestmark = pytest.mark.stress


def _server_module():
    return types.SimpleNamespace(agent_sessions={"_s": object()}, memory_store=None, tool_router={})


async def test_concurrent_agent_chats(fake_llm, stress_params):
    """Drive run_agent_step directly (self-contained: agent_override + no tools)
    so each task is a full ReAct turn hitting the delayed fake LLM."""
    fake_llm.set_default("Stress response.")
    from core.react_engine import run_agent_step

    agent = {"id": "stress_agent", "name": "Stress", "type": "conversational",
             "tools": [], "system_prompt": "You are fast.", "skip_default_tools": True}
    server = _server_module()

    async def task(i: int):
        final = None
        async for ev in run_agent_step(
            message=f"request {i}",
            agent_id="stress_agent",
            session_id=f"sess_{i}",
            server_module=server,
            agent_override=agent,
            tools_override=[],
            max_turns=1,
        ):
            if ev.get("type") == "final":
                final = ev
        assert final is not None, f"task {i} produced no final event"

    metrics = await run_load(
        "agent_chat", task,
        total=stress_params["total"], concurrency=stress_params["concurrency"],
    )
    assert metrics["failed"] == 0, metrics["sample_errors"]
    assert metrics["succeeded"] == stress_params["total"]
    # Every task made at least one (delayed) LLM call.
    assert fake_llm.call_count >= stress_params["total"]
