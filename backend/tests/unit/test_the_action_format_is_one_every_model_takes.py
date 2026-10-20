"""The agent prompt's action format asks for nothing the Claude 5 family refuses (F757).

CLIM1's Phase 1, 2026-10-17: every Synapse agent on Sonnet 5.5 failed. Through the
Anthropic API, Sonnet 5.5, Opus 5.5 and Fable 5.1 answered the engine's agent prompt
with `stop_reason: "refusal"` and no text (Haiku 4.5 answered); an engine Sonnet 5.5
agent ended "The model answered with nothing usable"; Claude Code said "Sonnet 5.5's
safeguards flagged this message". Measured by removing parts of the prompt, the
trigger was "### REASONING & ACTION FORMAT": a request to write text inside a marker
block before every tool call. Renaming the marker (`[WHY]`) or rewording the section
around it was still refused; one plain sentence saying why, with no marker, passed on
all three. The section was in `core/tools.py` since 3590e7b3 (2026-05-21).

Naveen: a neutral action format any model takes, no model-name case. The thinking
panel keeps working: a sentence before the call arrives as the model's thought
(`llm_thought`), and a model that still writes `[REASONING]` markers has them stripped
and shown as before.
"""
import types

import pytest

from core.tools import _reasoning_protocol


MARKER_WORDS = ("[REASONING]", "[/REASONING]", "private scratch", "scratch space", "REQUIRED before every")


class TestTheFormatAsksForNoMarkerBlock:
    def test_the_section_names_no_marker(self):
        section = _reasoning_protocol(False)
        for words in MARKER_WORDS:
            assert words.lower() not in section.lower(), words

    def test_it_asks_for_a_plain_sentence_saying_why(self):
        section = _reasoning_protocol(False)
        assert "### ACTION FORMAT" in section
        assert "saying why" in section

    def test_it_still_teaches_the_call_and_the_answer(self):
        section = _reasoning_protocol(False)
        assert '{"tool": "<tool_name>", "arguments": {"<key>": "<value>"}}' in section
        assert "final answer" in section.lower()

    def test_a_native_tool_interface_is_told_where_the_sentence_goes(self):
        assert "native function-calling" in _reasoning_protocol(False)


class TestTheWholePromptAsksForNone:
    """What a step's agent actually sends, not only the section: its example turn
    taught the marker block too."""

    @pytest.mark.parametrize("model", ["gpt-4.1-mini", "claude-sonnet-5-5"])
    async def test_an_agents_system_prompt_asks_for_no_marker_block(self, fake_llm, model):
        from _fakes import seed as S
        from core.react_engine import run_agent_step

        agent = S.make_agent(tools=[], model=model)
        fake_llm.script(["done"])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        [ev async for ev in run_agent_step(
            message="hi", agent_id=agent["id"], session_id="s_f757", server_module=server,
            agent_override=agent, max_turns=2,
        )]
        system = fake_llm.calls[0]["sys_prompt"]
        assert "### ACTION FORMAT" in system
        # A native-reasoning model is told not to write markers; neither is asked to.
        for words in ("[REASONING]...[/REASONING]", "\n[REASONING]\n", "private scratch", "scratch space",
                      "REQUIRED before every", "Emit a `[REASONING]`"):
            assert words.lower() not in system.lower(), words


class TestAMarkerAModelStillWritesIsStillShownApart:
    @pytest.mark.parametrize("text,reasoning,action", [
        ("[REASONING]look it up[/REASONING]\n{\"tool\": \"get_order\", \"arguments\": {}}",
         ["look it up"], "{\"tool\": \"get_order\", \"arguments\": {}}"),
    ])
    def test_it_is_stripped_and_extracted(self, text, reasoning, action):
        from core.react_engine import extract_reasoning, strip_reasoning
        assert extract_reasoning(text) == reasoning
        assert strip_reasoning(text) == action
