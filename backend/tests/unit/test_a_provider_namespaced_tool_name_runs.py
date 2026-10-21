"""A tool call named with a provider's namespace runs the tool it names (F779; SAI2-P1, Naveen 10-21: fold it in).

E2's measure (2026-10-21): Gemini 3 Flash called `default_api:lookup_sales`, the name its SDK gives a function, for
the offered `lookup_sales`. The engine took off OpenAI's `functions.` (F550) and nothing else, so the call was an
unknown tool: a wasted turn, or a failed step where turns are few. A known provider namespace is now dropped when —
and only when — what is left is exactly one tool the agent was offered. Never fuzzy: a name offered as written is
left as written, a bare name not offered is not invented, and a name two namespaces could both fit is left alone.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.react_engine import _offered_name

OFFERED = {"lookup_sales": {}, "send": {}}


class TestTheName:
    @pytest.mark.parametrize("written", ["default_api:lookup_sales", "default_api.lookup_sales",
                                         "functions.lookup_sales"])
    def test_a_known_namespace_comes_off_when_the_rest_is_offered(self, written):
        assert _offered_name(written, OFFERED) == "lookup_sales"

    def test_a_name_offered_as_written_stays(self):
        assert _offered_name("default_api:lookup_sales", {"default_api:lookup_sales": {}, "lookup_sales": {}}) == \
            "default_api:lookup_sales"

    @pytest.mark.parametrize("written", ["default_api:lookup", "default_api:", "my_api:lookup_sales",
                                         "default_api:default_api:lookup_sales", "lookup_sales2"])
    def test_nothing_is_guessed(self, written):
        assert _offered_name(written, OFFERED) == written

    def test_a_plain_name_is_unchanged(self):
        assert _offered_name("send", OFFERED) == "send"


async def test_flashs_call_runs_the_tool_in_a_turn(fake_llm):
    from core.react_engine import run_agent_step

    ran = []

    async def executor(name, args):
        ran.append(name)
        return json.dumps({"total": 1240})

    schema = {"type": "function", "function": {"name": "lookup_sales", "description": "Sales.", "parameters": {
        "type": "object", "properties": {"region": {"type": "string"}}}}}
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script(['{"tool": "default_api:lookup_sales", "arguments": {"region": "north"}}', "North sold 1240."])
    events = [e async for e in run_agent_step(
        message="North's sales?", agent_id=agent["id"], session_id="s_f779",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=4)]
    assert ran == ["lookup_sales"]
    assert [e for e in events if e.get("type") == "final"][-1]["response"] == "North sold 1240."


async def test_a_tool_steps_model_naming_it_so_runs_the_steps_tool(fake_llm, monkeypatch):
    """The same class on a Tool step's model path (F281 still holds: only the step's own tool)."""
    import core.tools as tools_mod
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine
    from core.tools import VirtualTool

    clock = VirtualTool("clock", "time", {})

    async def one(server_module, name, agent, custom_tools):
        return clock if name == "clock" else None

    monkeypatch.setattr(tools_mod, "resolve_one_tool", one)
    calls = []

    class _Session:
        async def call_tool(self, name, args, **_kw):
            calls.append(name)
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="09:00")], isError=False)

    server = types.SimpleNamespace(agent_sessions={"fake": _Session()}, memory_store=None,
                                   tool_router={"clock": ("fake", "clock")})
    orch = S.make_orchestration(id="orch_f779", entry_step_id="t", steps=[
        {"id": "t", "name": "Time", "type": "tool", "forced_tool": "clock", "prompt_template": "time?",
         "output_key": "now", "model": "gemini-x", "max_turns": 1, "next_step_id": None}])
    await S.seed_orchestrations([orch])
    fake_llm.script(['{"tool": "default_api:clock", "arguments": {}}'])
    events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), server).run("go", run_id="r_f779")]
    assert calls == ["clock"]
    assert [e for e in events if e.get("type") == "orchestration_complete"][-1]["status"] == "completed"
