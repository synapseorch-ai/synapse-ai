"""A thought never carries a tool call's JSON, a schema blob or the history's digest (F907, POL1, D317).

Naveen, 2026-10-23: "under chat sometimes the thoughts are not great.. it has tool call jsons too some model i think
adds jsons too in thoughts". The engine sends each model reply as an `llm_thought` before it parses the reply, and a
provider's native calls reach that reply as `{"tool": …, "arguments": …}` lines (`_openai_compat_extract`); gpt-oss,
Nova and Flash also write calls and schema-shaped JSON into their own text and reasoning. The chat drew all of it.
QA1c's F559 line is the same seam: the answer had a copied digest cut, its thought kept it, so the person read the
engine's notes in the thinking panel.

The class fix is where the thought is made: a call reads as the words "→ Called name", any other JSON object (or a
list of them) is dropped, a fence or `<tool_call>` holding one goes with it, and a copied digest is cut. The engine
still parses the raw text — only what a person reads changes. A thought with nothing left is not sent.
"""
import json
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.answer_filter import readable_thought

CALL = '{"tool": "lookup", "arguments": {"q": "hubspot"}}'
DIGEST = ("\n\n[Tool calls you made for this answer, kept by the system for you — not part of your reply. Read any "
          "earlier turn in full with session_history.]\n- spawn_subtask {\"task\": \"elevation\"} → 7.0 m")


class TestReadableThought:
    def test_a_call_alone_reads_as_words(self):
        assert readable_thought(CALL) == "→ Called lookup"

    def test_several_calls_on_their_own_lines(self):
        two = CALL + "\n" + '{"tool": "time", "arguments": {}}'
        assert readable_thought(two) == "→ Called lookup\n→ Called time"

    def test_prose_around_a_call_is_kept(self):
        text = f"I should check the CRM first.\n{CALL}\nThen I'll write the summary."
        assert readable_thought(text) == "I should check the CRM first.\n→ Called lookup\nThen I'll write the summary."

    @pytest.mark.parametrize("wrapped", [
        f"```json\n{CALL}\n```",
        f"```\n{CALL}\n```",
        f"<tool_call>{CALL}</tool_call>",
        f"<tool_call>\n{CALL}\n</tool_call>",
    ])
    def test_a_fenced_or_tagged_call(self, wrapped):
        assert readable_thought(f"Checking.\n{wrapped}") == "Checking.\n→ Called lookup"

    def test_openais_pseudo_format(self):
        uses = {"tool_uses": [{"recipient_name": "functions.lookup", "parameters": {"q": "x"}},
                              {"recipient_name": "functions.time", "parameters": {}}]}
        assert readable_thought(json.dumps(uses)) == "→ Called lookup\n→ Called time"

    def test_a_schema_or_data_blob_is_dropped(self):
        schema = '{"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}'
        listing = '[{"name": "time", "description": "The time"}, {"name": "vault_read", "description": "Read"}]'
        assert readable_thought(f"The tool takes:\n{schema}\nand there are:\n{listing}") == \
            "The tool takes:\nand there are:"

    def test_a_copied_digest_is_cut(self):
        assert readable_thought("Rotterdam's elevation is 7.0 meters." + DIGEST) == \
            "Rotterdam's elevation is 7.0 meters."

    @pytest.mark.parametrize("text", [
        "Use {state.summary} in the reply and cite [1].",
        "The set {1, 2} and the list [1, 2] are fine.  \n\n\n  Spacing stays as written.",
        "An empty object {} means nothing was found.",
    ])
    def test_prose_without_json_is_untouched(self, text):
        assert readable_thought(text) == text

    def test_nothing_left_is_empty(self):
        assert readable_thought('{"type": "object"}') == ""
        assert readable_thought("") == ""


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


LOOKUP = {"type": "function", "function": {"name": "lookup", "description": "look",
                                           "parameters": {"type": "object", "properties": {}}}}


async def _run(fake_llm, session_id, script):
    from core.react_engine import run_agent_step
    agent = S.make_agent(tools=["all"], skip_default_tools=True)

    async def executor(name, args):
        return "found it"

    fake_llm.script(script)
    events = [e async for e in run_agent_step(
        message="look it up", agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent, tools_override=[LOOKUP], tool_executor=executor)]
    return agent, events


class TestTheEngine:
    async def test_a_turns_thoughts_say_the_call_in_words(self, fake_llm):
        from core.session import _get_conversation_history
        agent, events = await _run(fake_llm, "s_f907a", ["Let me look." + "\n" + tool_call("lookup"), "It is found."])
        thoughts = [e["thought"] for e in events if e.get("type") == "llm_thought"]
        assert thoughts == ["Let me look.\n→ Called lookup", "It is found."]
        [turn] = await _get_conversation_history("s_f907a", agent["id"])
        assert turn["thoughts"] == thoughts
        assert [e["response"] for e in events if e.get("type") == "final"] == ["It is found."]

    async def test_native_reasoning_holding_a_call(self, fake_llm):
        from core.llm_providers import LLMResult
        _agent, events = await _run(fake_llm, "s_f907b", [
            LLMResult(tool_call("lookup"), reasoning=f"I need the CRM. {CALL}"),
            LLMResult("It is found.", reasoning='{"type": "object"}'),
        ])
        reasoning = [e["reasoning"] for e in events if e.get("type") == "llm_reasoning"]
        assert reasoning == ["I need the CRM.\n→ Called lookup"], "the second turn's was only a blob: not sent"

    async def test_a_copied_digest_never_reaches_the_thoughts(self, fake_llm):
        """QA1c's F559 line: the answer was cut, its thought was not, so the thought was the digest block."""
        from core.session import _get_conversation_history
        agent, events = await _run(fake_llm, "s_f907c", ["Rotterdam's elevation is 7.0 meters." + DIGEST])
        [turn] = await _get_conversation_history("s_f907c", agent["id"])
        assert turn["assistant"] == "Rotterdam's elevation is 7.0 meters."
        assert turn["thoughts"] == [turn["assistant"]], "the last thought echoes the answer, so the chat drops it"
        assert not any("kept by the system" in e.get("thought", "") for e in events)

    async def test_a_saved_list_from_elsewhere_is_made_readable_too(self):
        """The fleet's job and a stopped turn save from the events (`thinking_from_events`) and a resumed turn
        carries an older snapshot's raw thoughts: every saved list passes through the same reader."""
        from core.session import panel_text
        assert panel_text([CALL, "Done.", '{"type": "object"}']) == ["→ Called lookup", "Done."]
