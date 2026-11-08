"""A chat turn with a caller's system message, with tracing on (OAI1).

`_caller_instructions` was written between `run_react_loop` and its span
decorator, so the decorator wrapped it instead. With telemetry off that changes
nothing (the decorator hands back what the function returned), which is how
every OSS test passed; cloud runs with tracing on, and there the helper returned
a traced generator, so the OpenAI endpoint's every turn failed adding it to the
system prompt — and `run_react_loop` itself had lost its span. Measured on the
dev stack, 2026-10-10.
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import Streamed
from _fakes.telemetry import telemetry_on  # noqa: F401  (fixture)


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


def test_the_helper_is_a_plain_function_and_the_loop_keeps_its_span():
    import core.react_engine as re

    assert not hasattr(re._caller_instructions, "__wrapped__")
    assert re._caller_instructions("Be brief.").endswith("Be brief.")
    assert re._caller_instructions("  ") is None
    assert getattr(re.run_react_loop, "__wrapped__", None) is not None   # the span decorator is the loop's


async def test_a_turn_with_a_callers_system_message_runs_with_tracing_on(telemetry_on, fake_llm):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop

    agent = S.make_agent(id="agent_traced", name="Traced", tools=["all"], skip_default_tools=True)
    await S.seed_agents([agent])
    fake_llm.script([Streamed(["Fine."])])
    events = [e async for e in run_react_loop(
        ChatRequest(message="hi", session_id="oai_traced", agent_id=agent["id"]), _server(),
        stream_answer=True, history_override=[], caller_system="Answer briefly.", save_turn=False,
        log_source="openai")]

    assert [e for e in events if e["type"] == "final"][-1]["response"] == "Fine."
    assert "Answer briefly." in fake_llm.calls[-1]["sys_prompt"]
