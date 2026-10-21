"""An agent is taught the task list only when it is given one (F803; SAI2-P1).

The stable system prompt told every agent to "call `update_tasks` FIRST" for a multi-step job, including the agents the
engine never offers it to: one with `planning` off, one with `skip_default_tools`, one run on a caller's own tool list
(`tools_override`). A model that follows its prompt then calls a tool it does not have — gpt-oss-120b on cloud dev did
in 3 of 12 trials of E2's probe — and spends a turn on "does not exist" (or, before F802, lost the turn). The section
now goes with the tool: where `update_tasks` is offered it reads as before, word for word; where it is not, it is not
there.
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

from core.tools import TASK_LIST_SECTION, build_system_prompt

LOOKUP = {"type": "function", "function": {"name": "lookup", "description": "lookup.", "parameters": {
    "type": "object", "properties": {"q": {"type": "string"}}}}}


def _prompt(**kw):
    return build_system_prompt("You write reports.", "[]", "s_f803", lambda _sid: {}, None, **kw)


class TestThePrompt:
    def test_it_teaches_the_task_list_by_default(self):
        assert TASK_LIST_SECTION in _prompt()
        assert TASK_LIST_SECTION.startswith("**TASK LIST (`update_tasks`):**")

    def test_without_the_tool_it_is_not_mentioned(self):
        prompt = _prompt(task_list=False)
        assert "update_tasks" not in prompt
        assert "TASK LIST" not in prompt

    def test_only_the_section_differs(self):
        assert _prompt().replace(TASK_LIST_SECTION, "") == _prompt(task_list=False)


async def _system(fake_llm, agent, **kw):
    from core.react_engine import run_agent_step

    fake_llm.script(["Done."])
    [ev async for ev in run_agent_step(
        message="Write the weekly report.", agent_id=agent["id"], session_id="s_f803",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, max_turns=2, **kw)]
    return fake_llm.calls[0]["sys_prompt"]


class TestATurn:
    async def test_an_agent_that_plans_is_taught_it(self, fake_llm):
        assert TASK_LIST_SECTION in await _system(fake_llm, S.make_agent(tools=[]))

    async def test_planning_off_is_not(self, fake_llm):
        assert "update_tasks" not in await _system(fake_llm, S.make_agent(tools=[], planning=False))

    async def test_skip_default_tools_is_not(self, fake_llm):
        assert "update_tasks" not in await _system(fake_llm, S.make_agent(tools=[], skip_default_tools=True))

    async def test_a_callers_own_tools_are_not(self, fake_llm):
        async def executor(name, args):
            return "ok"

        agent = S.make_agent(tools=["all"])
        assert "update_tasks" not in await _system(fake_llm, agent, tools_override=[LOOKUP],
                                                   tool_executor=executor)

    async def test_a_planning_agent_still_keeps_its_list(self, fake_llm):
        """The tool itself is untouched: a call to it writes the plan as before."""
        from core.react_engine import run_agent_step

        fake_llm.script([tool_call("update_tasks", tasks=[{"content": "write", "status": "completed"}]), "Done."])
        agent = S.make_agent(tools=[])
        events = [ev async for ev in run_agent_step(
            message="Write the weekly report.", agent_id=agent["id"], session_id="s_f803b",
            server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
            agent_override=agent, max_turns=3)]
        assert any(e.get("type") == "tasks_updated" for e in events)
