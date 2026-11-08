"""
`core.answer_filter` — what of a streaming model call a person may see (U72).

The engine strips reasoning and parses tool calls only once a call has ended;
a stream has to decide piece by piece. These pin that it never shows a tool
call's JSON or a model's private reasoning, whatever the pieces' boundaries,
and that what it shows of a plain answer is the answer.
"""
import json

import pytest

from core.answer_filter import AnswerFilter


def run(pieces, *, close=True):
    """Feed `pieces`; return (shown text, the resets, whether it stopped)."""
    f = AnswerFilter()
    shown, resets = "", []
    ops = []
    for p in pieces:
        ops += f.feed(p)
    if close and not f.stopped:
        ops += f.close()
    for op in ops:
        if op[0] == "text":
            shown += op[1]
        else:
            resets.append(op[1])
            shown = ""
    return shown, resets, f.stopped


def every_split(text):
    """`text` cut in two at every position, and in single characters."""
    yield [text]
    yield list(text)
    for i in range(1, len(text)):
        yield [text[:i], text[i:]]


def test_a_plain_answer_streams_whole():
    for pieces in every_split("Paris is the capital of France."):
        assert run(pieces) == ("Paris is the capital of France.", [], False)


def test_a_plain_answer_shows_before_close():
    f = AnswerFilter()
    ops = f.feed("Hello there, ") + f.feed("friend")
    assert "".join(o[1] for o in ops if o[0] == "text") == "Hello there, friend"


@pytest.mark.parametrize("text", [
    "[REASONING]the user wants a city[/REASONING]Paris.",
    "[reasoning]lower case[/reasoning]Paris.",
    "<think>hmm, a capital</think>Paris.",
    "<thinking>Nova thinks here</thinking>Paris.",
])
def test_reasoning_is_never_shown_at_any_split(text):
    for pieces in every_split(text):
        shown, resets, _ = run(pieces)
        assert shown == "Paris.", pieces
        assert resets == []


def test_an_orphan_close_takes_back_what_went_out():
    # A template that opened <think> for the model: all before the close was thinking.
    for pieces in every_split("the model mulls it over</think>Paris."):
        shown, _, _ = run(pieces)
        assert shown == "Paris.", pieces


def test_an_orphan_close_after_text_went_out_resets():
    f = AnswerFilter()
    first = f.feed("mulling over the question ")
    assert first == [("text", "mulling over the question ")]
    assert f.feed("</think>Paris.") == [("reset", "think_close"), ("text", "Paris.")]


@pytest.mark.parametrize("call", [
    json.dumps({"tool": "search", "arguments": {"q": "weather"}}),
    json.dumps({"arguments": {"q": "x"}, "tool": "search"}),
    "```json\n" + json.dumps({"tool": "search", "arguments": {}}) + "\n```",
    "<tool_call>" + json.dumps({"tool": "search", "arguments": {}}) + "</tool_call>",
])
def test_a_leading_tool_call_never_leaks_at_any_split(call):
    for pieces in every_split(call):
        shown, resets, stopped = run(pieces, close=False)
        assert shown == "" and resets == [], pieces
        assert stopped, pieces


def test_prose_before_a_tool_call_streams_then_the_call_stops_it():
    call = json.dumps({"tool": "search", "arguments": {"q": "weather"}})
    for pieces in every_split("Let me check that. " + call):
        shown, _, stopped = run(pieces, close=False)
        assert shown.strip() == "Let me check that.", pieces
        assert stopped


def test_nothing_after_a_tool_call_is_shown():
    f = AnswerFilter()
    f.feed(json.dumps({"tool": "a", "arguments": {}}))
    assert f.stopped
    assert f.feed(" and here is more text") == []
    assert f.close() == []


def test_non_tool_json_is_released_once_it_closes():
    text = 'Result: {"temperature": 21, "unit": "C"} as asked.'
    for pieces in every_split(text):
        assert run(pieces)[0] == text, pieces


def test_json_is_held_while_it_might_still_be_a_tool_call():
    f = AnswerFilter()
    assert f.feed('Here: {"to') == [("text", "Here: ")]
    assert f.feed('ol_hint": 1}') == [("text", '{"tool_hint": 1}')]


def test_a_brace_in_prose_or_code_streams():
    for text in ["Use {name} as the placeholder.", "if (x) { return y; }"]:
        f = AnswerFilter()
        ops = f.feed(text)
        assert "".join(o[1] for o in ops) == text


def test_a_code_fence_streams():
    text = "Try this:\n```python\nprint('hi')\n```\nDone."
    for pieces in every_split(text):
        assert run(pieces)[0] == text, pieces


def test_a_fenced_json_answer_is_released_at_its_closing_fence():
    text = "```json\n{\"name\": \"Ada\"}\n```"
    f = AnswerFilter()
    assert f.feed(text[:-3]) == []
    assert f.feed("```") == [("text", text)]


def test_close_releases_what_was_held():
    f = AnswerFilter()
    assert f.feed('The set {"a", "b') == [("text", "The set ")]
    assert f.close() == [("text", '{"a", "b')]


def test_restart_takes_back_a_retried_call():
    f = AnswerFilter()
    f.feed("Half an ans")
    assert f.restart() == [("reset", "retry")]
    assert f.feed("A whole answer.") == [("text", "A whole answer.")]
    assert f.shown == "A whole answer."


def test_restart_before_anything_showed_says_nothing():
    f = AnswerFilter()
    f.feed("[REASONING]still thinking")
    assert f.restart() == []


def test_leading_whitespace_is_not_shown():
    assert run(["\n\n  ", "Paris."])[0] == "Paris."
