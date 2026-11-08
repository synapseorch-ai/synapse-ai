"""An answer never carries the history's notes on what tools ran (F559, VOX1f, D273).

BOARD1a's live check (Nova Pro, a schedule's second firing): the conversation held the first firing's answer with
its digest appended (F242: "[Tool calls you made for this answer, kept by the system for you — not part of your
reply. …]" and a line per call), and the model ended its own answer with a copy of the block, so the saved answer,
what a channel or a schedule posts from it, and the next turn's history all carried the engine's notes. The engine
knows the headings' exact words: a copied block is cut, from its heading to the end, in the stream (nobody sees or
hears it) and in the answer that is saved, sent and posted.
"""
import types

import pytest

from _fakes import seed as S
from core.answer_filter import AnswerFilter

ANSWER = ("I have added one new bug to the shared board 'Bugs'. Here's the summary:\n\n"
          "- Added bug BUG-104: \"Password reset email is sent twice\"\n\nA total of 1 bug was added to the board.")
COPIED = ("\n\n[Tool calls you made for this answer, kept by the system for you — not part of your reply. Read any "
          "earlier turn in full with session_history.]\n- fetch_bug_list → [{\"id\": \"BUG-101\"}]\n"
          "- board_add {\"title\": \"Login times out\", \"key\": \"BUG-101\"} → Already on the shared board 'Bugs'")


def _stream(pieces):
    f = AnswerFilter()
    shown = ""
    ops = []
    for p in pieces:
        ops += f.feed(p)
    ops += f.close()
    for op in ops:
        shown = shown + op[1] if op[0] == "text" else ""
    return shown


@pytest.mark.parametrize("heading", [
    "[Tool calls you made for this answer, kept by the system for you — not part of your reply.]",
    "[Tools you used for this answer: shop_search_products]",
    "[This turn failed after these tool calls ran — they did run, kept by the system for you — not part of your reply.]",
    "[This turn was stopped after these tool calls ran: board_add]",
    "[This turn was cut off before it answered, after these tool calls ran — they did run, kept by the system for you.]",
])
def test_a_copied_heading_is_cut_to_the_end(heading):
    from core.answer_filter import strip_copied_digest
    assert strip_copied_digest(f"{ANSWER}\n\n{heading}\n- board_add → ok") == ANSWER


def test_an_answer_without_one_is_untouched():
    from core.answer_filter import strip_copied_digest
    text = "See [1] and [Note: prices exclude tax]. Tools you used: none."
    assert strip_copied_digest(text) == text


@pytest.mark.parametrize("size", [1, 3, 7, 40])
def test_the_stream_never_shows_it_at_any_split(size):
    text = ANSWER + COPIED
    pieces = [text[i:i + size] for i in range(0, len(text), size)]
    shown = _stream(pieces)
    assert "kept by the system" not in shown and "fetch_bug_list" not in shown
    assert shown.strip() == ANSWER


def test_a_bracket_that_is_no_heading_streams():
    text = "Two results [1] and [Tools listed below]: a, b."
    assert _stream(list(text)) == text


async def test_the_saved_and_final_answer_never_carry_it(fake_llm):
    from core.react_engine import run_agent_step
    from core.store import sessions as store

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script([ANSWER + COPIED])
    events = [ev async for ev in run_agent_step(
        message="Add today's bugs to the board.", agent_id=agent["id"], session_id="s_f559",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, max_turns=2)]
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[-1]["response"] == ANSWER
    saved = (await store.get("s_f559", agent["id"]))["messages"]
    assert saved[-1]["content"] == ANSWER


def test_the_headings_are_the_digests_own():
    """The cut knows the words `_digests` writes: a heading reworded there is still cut here."""
    from core.answer_filter import DIGEST_HEADINGS
    from core.chat_turns import FAILED_ANSWER, STOPPED_ANSWER
    from core.session import _digests

    call = {"tool": "board_add", "args": {"key": "BUG-1"}, "result": "ok"}
    turns = [{"assistant": a, "calls": [call]} for a in ("Done.", FAILED_ANSWER, STOPPED_ANSWER, "")]
    turns = [*({"assistant": "Older.", "calls": [call]} for _ in range(4)), *turns]
    for note in _digests(turns).values():
        assert note.startswith(DIGEST_HEADINGS), note[:80]
