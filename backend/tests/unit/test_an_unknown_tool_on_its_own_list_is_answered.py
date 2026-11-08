"""A call to a tool the turn does not have is answered, whatever shape the turn's tools are in (F802; SAI2-P1).

A caller may hand `run_agent_step` its own tools as OpenAI-style declarations (`tools_override`) with an executor that
answers its own names and falls through (None) for the rest. A model that calls a name it was never given — gpt-oss on
cloud dev called the task list it had been taught but not offered, 3 of 12 trials — reached the "does not exist"
answer, which listed the turn's tools as `t.name` and so raised `AttributeError` on a declaration: the turn died
instead of being told. It is told now, by name, and goes on.
"""
import json
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _declared(name):
    return {"type": "function", "function": {"name": name, "description": f"{name}.", "parameters": {
        "type": "object", "properties": {"q": {"type": "string"}}}}}


LOOKUP, SUBMIT = _declared("lookup"), _declared("submit_plan")


async def _executor(name, args):
    if name in ("lookup", "submit_plan"):
        return json.dumps({"ok": name})
    return None  # not ours: the engine's routing answers it


async def _turn(finish):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    return [ev async for ev in run_agent_step(
        message="Plan the week.", agent_id=agent["id"], session_id="s_f802",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[LOOKUP, SUBMIT], tool_executor=_executor, max_turns=4,
        source="orchestration_step", finish_tools=finish)]


async def test_the_model_is_told_the_tool_does_not_exist_and_what_it_has(fake_llm):
    fake_llm.script([tool_call("update_tasks", tasks=[]), "Done."])
    events = await _turn([])
    assert {"type": "tool_result", "tool_name": "update_tasks", "preview": "Error: tool not found"} in events
    told = fake_llm.calls[1]["prompt_msg"]
    assert "Tool 'update_tasks' does not exist" in told
    assert "Available tools: lookup, submit_plan" in told


async def test_a_finishing_step_goes_on_to_its_finish_call(fake_llm):
    fake_llm.script([tool_call("update_tasks", tasks=[]), tool_call("submit_plan", q="w")])
    final = [e for e in await _turn(["submit_plan"]) if e.get("type") == "final"][-1]
    assert final["finish"] == {"tool": "submit_plan", "result": {"ok": "submit_plan"}}
