"""An answer cut at the output limit keeps its start (F1273; TRI1, found building D339 P4).

The loop asks a model whose reply was cut at the output limit to "continue from exactly where you stopped", and kept
only that continuation as the answer: a long answer — a report, a page of findings — lost everything before the
cut, and nothing said so. Now the pieces are one answer, in order. A cut reply that was a tool call is not part of
the answer: the model is asked to send the whole call again.
"""
from __future__ import annotations

import types

from _fakes import seed as S


def _model(pieces, monkeypatch):
    """A model that answers with `pieces` in turn: (text, stop_reason)."""
    import core.llm_providers as llm
    import core.react_engine as re_mod

    replies = list(pieces)

    async def model(*_a, **_kw):
        text, stop = replies.pop(0)
        return llm.LLMResult(text, 10, 10, 0, 0, stop)

    monkeypatch.setattr(re_mod, "llm_generate_response", model)


async def _answer(max_turns=6) -> str:
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=[], skip_default_tools=True)
    events = [ev async for ev in run_agent_step(
        message="Write the report.", agent_id=agent["id"], session_id="s_f1273",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[], max_turns=max_turns)]
    finals = [e for e in events if e.get("type") == "final"]
    assert finals, "the turn ended with a final event"
    return finals[-1]["response"]


async def test_a_cut_answer_is_one_answer_with_its_rest(monkeypatch):
    _model([("The first half of the report, ", "max_tokens"), ("and its second half.", "end_turn")], monkeypatch)
    assert await _answer() == "The first half of the report, and its second half."


async def test_an_answer_cut_more_than_twice_keeps_every_piece(monkeypatch):
    _model([("A. ", "max_tokens"), ("B. ", "max_tokens"), ("C.", "max_tokens")], monkeypatch)
    assert await _answer() == "A. B. C."


async def test_a_cut_tool_call_is_not_part_of_the_answer(monkeypatch):
    _model([('{"tool": "lookup", "arguments": {"q": "a', "max_tokens"), ("The answer.", "end_turn")], monkeypatch)
    assert await _answer() == "The answer."


async def test_an_answer_that_was_never_cut_is_as_it_was(monkeypatch):
    _model([("Short and whole.", "end_turn")], monkeypatch)
    assert await _answer() == "Short and whole."
