"""A reply that was all reasoning is asked for again with less thinking, and its error names the right control (F863).

QA1c on prod (2026-10-22, rc3, S3 on Naveen's Anthropic key): a Claude Haiku 4.5 agent at the workspace defaults
(reasoning "medium", no response cap) closed five board cards in one go, and turn 7 came back as thinking only. The cap
was not what it hit — `plan_for` already sizes a medium Claude call at 6,144 + 4,096 = 10,240 and the whole run wrote
7,650 tokens — the model ended its turn after thinking. The engine's one retry (F305) sent the identical request with
a nudge, the second reply was thinking only too, and the run failed after the paid work. Its error then said "Raise Max
response length", the person did, and the raised cap is what made a Team step's projection balloon (F865).

Now the retry asks with the reasoning effort a step lower — one call, then back to the agent's own — on every
provider, because the engine asks; and "only reasoning" says to raise the cap only when the cap was what stopped it.
"""
import json
import types

import pytest

import core.llm_providers as llm
from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _turn(session_id, agent, **kwargs):
    from core.react_engine import run_agent_step
    return [e async for e in run_agent_step(
        message="Close the five cards.", agent_id=agent["id"], session_id=session_id,
        server_module=_server(), agent_override=agent, **{"tools_override": [], "max_turns": 15, **kwargs})]


def _provider(monkeypatch, *replies):
    """The model: each reply in turn, an exception raised; every call's effort kept."""
    import core.react_engine as re_mod
    efforts, queue = [], list(replies)

    async def model(*_a, **kw):
        efforts.append(kw.get("reasoning_effort"))
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return llm.LLMResult(reply)

    monkeypatch.setattr(re_mod, "llm_generate_response", model)
    return efforts


def _thought_only(stop="stop"):
    err = llm.EmptyAnswer("Error: Anthropic returned only reasoning.", reasoning="Now I'll close all 5 cards…")
    err.stop_reason = stop
    return err


class TestTheRetryThinksLess:
    @pytest.mark.parametrize("own, lower", [("medium", "low"), ("high", "medium"), ("low", "off"), ("off", "off")])
    async def test_a_provider_reply_of_only_reasoning_is_asked_again_a_step_lower(self, monkeypatch, own, lower):
        efforts = _provider(monkeypatch, _thought_only(), "Closed.")
        agent = S.make_agent(tools=[], skip_default_tools=True, reasoning_effort=own)
        events = await _turn(f"s_f863_{own}", agent)
        assert efforts == [own, lower]
        assert [e["response"] for e in events if e.get("type") == "final"] == ["Closed."]

    async def test_an_unset_effort_is_the_default_one_step_lower(self, monkeypatch):
        efforts = _provider(monkeypatch, _thought_only(), "Closed.")
        agent = S.make_agent(tools=[], skip_default_tools=True)
        await _turn("s_f863_unset", agent)
        assert efforts[1] == "low", "unset reads as the default, medium"

    async def test_only_the_retry_thinks_less_and_the_next_turn_is_the_agents_own(self, monkeypatch):
        efforts = _provider(monkeypatch, _thought_only(),
                            '{"tool": "close_card", "arguments": {"id": "KF-1"}}', "All closed.")

        async def executor(name, args):
            return json.dumps({"closed": args["id"]})

        schema = {"type": "function", "function": {"name": "close_card", "description": "Close.", "parameters": {
            "type": "object", "properties": {"id": {"type": "string"}}}}}
        agent = S.make_agent(tools=["all"], skip_default_tools=True, reasoning_effort="medium")
        await _turn("s_f863_next", agent, tools_override=[schema], tool_executor=executor)
        assert efforts == ["medium", "low", "medium"]

    async def test_the_retry_says_how_hard_it_asked(self, monkeypatch):
        _provider(monkeypatch, _thought_only(), "Closed.")
        agent = S.make_agent(tools=[], skip_default_tools=True, reasoning_effort="medium")
        events = await _turn("s_f863_said", agent)
        assert [(e["retry"], e.get("effort")) for e in events if e.get("type") == "reasoning_only"] == [(1, "low")]

    async def test_a_marker_only_reply_is_asked_again_a_step_lower(self, fake_llm):
        fake_llm.script(["[REASONING]Two and two.[/REASONING]", "4"])
        agent = S.make_agent(tools=[], skip_default_tools=True, reasoning_effort="high")
        await _turn("s_f863_marker", agent)
        assert [c.get("reasoning_effort") for c in fake_llm.calls] == ["high", "medium"]

    async def test_a_reply_cut_off_at_the_cap_keeps_the_truncation_path(self, monkeypatch):
        """F305's line holds: thinking cut off at max_tokens is a truncation, not this retry."""
        efforts = _provider(monkeypatch, _thought_only(stop="max_tokens"), "Closed.")
        agent = S.make_agent(tools=[], skip_default_tools=True, reasoning_effort="medium")
        events = await _turn("s_f863_cap", agent)
        assert not [e for e in events if e.get("type") == "reasoning_only"]
        assert efforts[1] == "medium"


class TestTheErrorNamesTheControlThatFits:
    def _anthropic(self, stop_reason):
        block = types.SimpleNamespace(type="thinking", thinking="thinking…")
        with pytest.raises(llm.EmptyAnswer) as caught:
            llm._extract_anthropic_response(types.SimpleNamespace(content=[block], stop_reason=stop_reason))
        return str(caught.value)

    def _gemini(self, finish):
        part = types.SimpleNamespace(function_call=None, text="thinking…", thought=True)
        cand = types.SimpleNamespace(finish_reason=types.SimpleNamespace(name=finish),
                                     content=types.SimpleNamespace(parts=[part]))
        with pytest.raises(llm.EmptyAnswer) as caught:
            llm._extract_gemini_response(types.SimpleNamespace(candidates=[cand]))
        return str(caught.value)

    @pytest.mark.parametrize("words", ["_anthropic", "_gemini"])
    def test_a_reply_the_model_ended_itself_says_lower_the_effort_not_raise_the_cap(self, words):
        said = getattr(self, words)("end_turn" if words == "_anthropic" else "STOP")
        assert "lower the reasoning effort" in said.lower()
        assert "Max response length" not in said

    @pytest.mark.parametrize("words", ["_anthropic", "_gemini"])
    def test_a_reply_cut_off_at_the_cap_still_says_raise_it(self, words):
        said = getattr(self, words)("max_tokens" if words == "_anthropic" else "MAX_TOKENS")
        assert "Max response length" in said
