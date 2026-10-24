"""An agent offered no tool at all answers in words, never with an engine sentence (SAI2-P4, F983; SAI2's review 7).

Synapse AI's talk step (`step_answer_talk`) holds no tool: small talk needs nothing read. A prompt-mode model (prod's
Nova Pro) can still write a tool call. The engine refuses it, as it refuses any call to a tool the agent does not
hold; but twice in a row ended the step "ran out of turns", and "hi" showed an error (F1015's class).

So when an agent is offered no tool at all — no tools and no finish tools — the first call it makes starts F1015's
answer turn: tools withheld, "no tool is offered for this reply; give your final response now". A reply in words is
the answer. A reply that is still only a call ends the turn on a fixed sentence in words, as an answer: never
`no_answer`, never "ran out of turns". An agent that holds any tool keeps today's behaviour exactly.
"""
import json
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

from core.react_engine import NO_TOOLS_SENTENCE

READ = tool_call("read_workspace", topic="today")


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _chat(fake_llm, script, *, tools=(), max_turns=3, tools_override=None):
    from core.react_engine import run_agent_step

    async def executor(name, args):
        return json.dumps({"ok": True})

    agent = S.make_agent(tools=list(tools), skip_default_tools=True)
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="hi", agent_id=agent["id"], session_id="s_no_tools", server_module=_server(), agent_override=agent,
        tool_executor=executor, max_turns=max_turns, tools_override=tools_override)]


def _final(events):
    return next(e for e in reversed(events) if e.get("type") == "final")


class TestAnAgentWithNoTools:
    async def test_an_invented_call_then_words_is_the_answer(self, fake_llm):
        events = await _chat(fake_llm, [READ, "Hi! What shall we work on?"])
        final = _final(events)
        assert final["response"] == "Hi! What shall we work on?" and "no_answer" not in final
        asked = fake_llm.calls[1]
        assert "no tool is offered for this reply" in asked["prompt_msg"]
        assert not asked.get("tools")
        assert [e for e in events if e.get("type") == "tool_result" and e.get("refused")], "the call was refused"

    async def test_two_invented_calls_end_in_words_not_an_engine_sentence(self, fake_llm):
        events = await _chat(fake_llm, [READ, READ, "never reached"])
        final = _final(events)
        assert final["response"] == NO_TOOLS_SENTENCE and "no_answer" not in final
        assert "ran out of turns" not in final["response"] and "repeating" not in final["response"]
        assert fake_llm.call_count == 2, "one answer turn, then the sentence"
        assert not [e for e in events if e.get("type") == "repeat_stopped"]

    async def test_on_its_last_turn_a_call_still_ends_in_words(self, fake_llm):
        final = _final(await _chat(fake_llm, [READ, "never reached"], max_turns=1))
        assert final["response"] == NO_TOOLS_SENTENCE and "no_answer" not in final


class TestAnAgentWithATool:
    async def test_an_unknown_call_is_refused_as_today_with_no_answer_turn(self, fake_llm):
        look = {"type": "function", "function": {"name": "look", "description": "Look.",
                                                 "parameters": {"type": "object", "properties": {}}}}
        events = await _chat(fake_llm, [READ, "Here it is."], tools=["look"], tools_override=[look])
        assert _final(events)["response"] == "Here it is."
        assert "no tool is offered for this reply" not in fake_llm.calls[1]["prompt_msg"]
        assert [e for e in events if e.get("type") == "tool_result" and e.get("refused")]
