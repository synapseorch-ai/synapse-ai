"""A call named with OpenAI's namespace runs, and a reply of a tool's arguments alone is not an answer (F550, F551).

SUB1a's client-grade live check (D279): gpt-4.1-mini on the app's chat route never got its helper to run. It wrote
the engine's own shape with OpenAI's namespace, `{"tool": "functions.spawn_subtask", …}`, and the guard refused a
tool the agent does not have (F550; F537 took `functions.` off only inside OpenAI's pseudo-format). Its next reply was
its calls' arguments alone, `{"order":"HP-2041"}` twice, which became the agent's answer (F551).

Now the namespace comes off at dispatch, where the offered tools are known — only when the bare name is offered and
the dotted one is not (BF6's review: a custom tool may be named with a dot). And a chat reply that is only JSON
objects, each shaped like exactly one offered tool's arguments, is asked again once and never shown; a structured
answer (one asked for in JSON, or a workflow step's) is an answer as before.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.tools import VirtualTool

ORDER = VirtualTool("lookup_order", "looks up an order", {"type": "object", "properties": {"order": {"type": "string"}},
                                                          "required": ["order"]}, None)


class _Session:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        self.calls.append((name, dict(args)))
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"{args.get('order')} shipped day 12")],
                                     isError=False)


@pytest.fixture
def server(monkeypatch):
    import core.react_engine as re_mod

    async def aggregated(server_module, agent, custom_tools, **_):
        held = [ORDER] if "lookup_order" in (agent.get("tools") or []) or "all" in (agent.get("tools") or []) else []
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    session = _Session()
    return types.SimpleNamespace(agent_sessions={"fake": session}, memory_store=None,
                                 tool_router={"lookup_order": ("fake", "lookup_order")}, session=session)


async def _turn(server, fake_llm, script, *, source="chat", **agent):
    from core.react_engine import run_agent_step

    a = S.make_agent(**{"tools": ["lookup_order"], "skip_default_tools": True, **agent})
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(message="check HP-2041", agent_id=a["id"], session_id="s_f550",
                                              server_module=server, agent_override=a, max_turns=5, source=source)]


def _final(events):
    return [e for e in events if e.get("type") == "final"][0]["response"]


class TestANamespacedCallRuns:
    async def test_functions_dot_a_tool_it_has_runs_that_tool(self, server, fake_llm):
        events = await _turn(server, fake_llm, [json.dumps({"tool": "functions.lookup_order",
                                                            "arguments": {"order": "HP-2041"}}), "HP-2041 is fine."])
        assert server.session.calls == [("lookup_order", {"order": "HP-2041"})]
        assert _final(events) == "HP-2041 is fine."
        assert not [e for e in events if e.get("refused")]

    def test_a_dotted_name_that_is_itself_offered_is_left_alone(self):
        from core.react_engine import _offered_name

        assert _offered_name("functions.lookup_order", {"functions.lookup_order": {}, "lookup_order": {}}) \
            == "functions.lookup_order"
        assert _offered_name("functions.nothing", {"lookup_order": {}}) == "functions.nothing"
        assert _offered_name("lookup_order", {"lookup_order": {}}) == "lookup_order"


ARGS_ONLY = '{"order":"HP-2041"}\n\n{"order":"HP-2042"}'


class TestArgumentsAloneAreNotAnAnswer:
    async def test_they_are_asked_for_again_and_never_shown(self, server, fake_llm):
        events = await _turn(server, fake_llm, [ARGS_ONLY, "Both shipped inside 30 days."])
        assert _final(events) == "Both shipped inside 30 days."
        assert any(e.get("type") == "tool_call_unparsed" for e in events)

    async def test_twice_is_a_turn_with_no_answer_not_one_shown(self, server, fake_llm):
        from core.llm_providers import LLMError

        with pytest.raises(LLMError):
            await _turn(server, fake_llm, [ARGS_ONLY, ARGS_ONLY])

    async def test_a_json_answer_asked_for_in_chat_still_completes(self, server, fake_llm):
        """BF6's review: an agent asked to reply in JSON answers in JSON; it is shaped like no tool's arguments."""
        answer = '{"late": ["HP-2044"], "on_time": 11}'
        events = await _turn(server, fake_llm, [answer])
        assert _final(events) == answer

    async def test_a_workflow_steps_json_answer_completes(self, server, fake_llm):
        """A step's structured output is never second-guessed, even shaped like a tool's arguments."""
        events = await _turn(server, fake_llm, ['{"order": "HP-2041"}'], source="orchestration")
        assert _final(events) == '{"order": "HP-2041"}'

    async def test_an_agent_with_no_tools_answers_json_as_it_likes(self, server, fake_llm):
        events = await _turn(server, fake_llm, ['{"order": "HP-2041"}'], tools=[])
        assert _final(events) == '{"order": "HP-2041"}'

    def test_bare_objects_are_read_only_when_that_is_all_there_is(self):
        from core.tool_call_shapes import bare_objects

        assert bare_objects(ARGS_ONLY) == [{"order": "HP-2041"}, {"order": "HP-2042"}]
        assert bare_objects('Here: {"order": "HP-2041"}') == []
        assert bare_objects('{"tool": "lookup_order", "arguments": {}}') == []
        assert bare_objects("[1, 2]") == [] and bare_objects("{}") == []
