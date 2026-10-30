"""
An agent's answer streams while the model writes it (U72).

Chat sent the answer whole when the loop ended: a long answer was a minute of
"Analyzing your request…". With `stream_answer`, the engine yields
`answer_delta` as the model writes and `answer_reset` when what it showed turns
out not to be the answer, and `final` still says what the answer is. This is
the in-process hook every streaming caller reads: chat, the OpenAI-compatible
endpoint and a voice call.
"""
import json
import types

from _fakes import seed as S
from _fakes.fake_llm import Streamed, tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


def _tool_schema(name="lookup"):
    return {"type": "function", "function": {
        "name": name, "description": "test tool",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}


async def _step(fake_llm, script, *, stream=True, tools=False, max_turns=5):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    agent = S.make_agent(tools=["all"], skip_default_tools=True)

    async def executor(name, args):
        return "sunny, 21C"

    kw = dict(tools_override=[_tool_schema()], tool_executor=executor) if tools else {}
    return [e async for e in run_agent_step(
        message="go", agent_id=agent["id"], session_id="s_stream", server_module=_server(),
        agent_override=agent, max_turns=max_turns, stream_answer=stream, **kw)]


def _streamed(events):
    """What a reader that honours resets ends up showing."""
    shown = ""
    for e in events:
        if e["type"] == "answer_delta":
            shown += e["text"]
        elif e["type"] == "answer_reset":
            shown = ""
    return shown


def _final(events):
    return [e for e in events if e["type"] == "final"][-1]


async def test_a_plain_answer_streams_and_final_says_so(fake_llm):
    events = await _step(fake_llm, [Streamed(["Paris is ", "the capital ", "of France."])])

    deltas = [e for e in events if e["type"] == "answer_delta"]
    assert len(deltas) >= 1 and all(e["turn"] == 1 for e in deltas)
    assert _streamed(events) == "Paris is the capital of France."
    final = _final(events)
    assert final["response"] == "Paris is the capital of France." and final["streamed"] is True
    # The deltas come before the answer of record.
    assert events.index(deltas[-1]) < events.index(final)


async def test_without_the_flag_nothing_streams(fake_llm):
    events = await _step(fake_llm, [Streamed(["Paris."])], stream=False)
    assert not [e for e in events if e["type"].startswith("answer_")]
    assert "streamed" not in _final(events)


async def test_reasoning_never_streams(fake_llm):
    events = await _step(fake_llm, [Streamed(["[REASON", "ING]they want a city[/REAS", "ONING]Paris."])])
    assert _streamed(events) == "Paris."
    assert "they want" not in "".join(e.get("text", "") for e in events if e["type"] == "answer_delta")
    assert _final(events)["streamed"] is True


async def test_narration_before_a_tool_call_is_taken_back(fake_llm):
    call = tool_call("lookup", q="weather")
    events = await _step(fake_llm, [
        Streamed(["Let me check ", "that. ", call]),
        Streamed(["It is ", "sunny."]),
    ], tools=True)

    resets = [e for e in events if e["type"] == "answer_reset"]
    assert [r["reason"] for r in resets] == ["tool_call"] and resets[0]["turn"] == 1
    # The reset precedes the tool running; the call's JSON never streamed.
    assert events.index(resets[0]) < next(i for i, e in enumerate(events) if e["type"] == "tool_execution")
    assert '"tool"' not in "".join(e.get("text", "") for e in events if e["type"] == "answer_delta")
    assert _streamed(events) == "It is sunny."
    assert _final(events)["streamed"] is True


async def test_a_leading_tool_call_streams_nothing(fake_llm):
    events = await _step(fake_llm, [Streamed([tool_call("lookup", q="x")]), Streamed(["Done."])], tools=True)
    assert not [e for e in events if e["type"] == "answer_reset"]
    assert [e["turn"] for e in events if e["type"] == "answer_delta"] == [2]


async def test_a_native_tool_call_takes_back_what_streamed(fake_llm):
    # A provider streamed prose, then returned the call as JSON, the prose moved aside.
    events = await _step(fake_llm, [
        Streamed(["I'll look that up."], returns=tool_call("lookup", q="x")),
        Streamed(["Found it."]),
    ], tools=True)
    assert [e["reason"] for e in events if e["type"] == "answer_reset"] == ["tool_call"]
    assert _streamed(events) == "Found it."


async def test_a_retried_call_restarts_the_stream(fake_llm):
    events = await _step(fake_llm, [Streamed(["Half an ", "Whole answer."], restart_after=1,
                                             returns="Whole answer.")])
    assert [e["reason"] for e in events if e["type"] == "answer_reset"] == ["retry"]
    assert _streamed(events) == "Whole answer."
    assert _final(events)["streamed"] is True


def _cut_then(monkeypatch, *texts_and_stops):
    import core.react_engine as re_mod
    from core.llm_providers import LLMResult

    replies = iter([LLMResult(text, stop_reason=stop) for text, stop in texts_and_stops])

    async def model(*a, **k):
        from core import model_wait
        result = next(replies)
        model_wait.emit_delta(result.text)
        return result

    monkeypatch.setattr(re_mod, "llm_generate_response", model)


async def test_a_reply_cut_at_the_limit_keeps_its_start_on_the_screen(fake_llm, monkeypatch):
    """F1273: the start of an answer cut at the output limit is kept, so it is never taken back — the rest streams
    after it, what was shown is the answer word for word, and its start appears once (it used to vanish, then
    reappear whole as a `replaced`)."""
    _cut_then(monkeypatch, ("The first half, ", "max_tokens"), ("and the rest.", "end_turn"))
    events = await _step(fake_llm, [])
    final = [e for e in events if e["type"] == "final"][-1]
    assert [e for e in events if e["type"] == "answer_reset"] == []
    assert _streamed(events) == final["response"] == "The first half, and the rest."
    assert _streamed(events).count("The first half") == 1
    assert final.get("streamed") is True


async def test_a_cut_tool_call_is_never_shown_as_the_answer(fake_llm, monkeypatch):
    """A cut reply that was a tool call is still dropped: the model sends the whole call again, and only the
    answer that follows is shown."""
    _cut_then(monkeypatch, ('{"tool": "lookup", "arguments": {"q": "x', "max_tokens"), ("The answer.", "end_turn"))
    events = await _step(fake_llm, [])
    final = [e for e in events if e["type"] == "final"][-1]
    assert final["response"] == "The answer."
    assert _streamed(events) == "The answer." and '"tool"' not in _streamed(events)


async def test_a_model_failure_after_text_takes_it_back(fake_llm, monkeypatch):
    import pytest

    import core.react_engine as re_mod
    from core.llm_providers import LLMError

    async def model(*a, **k):
        from core import model_wait
        model_wait.emit_delta("Partly written")
        raise LLMError("content filter")

    monkeypatch.setattr(re_mod, "llm_generate_response", model)
    from core.react_engine import run_agent_step
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    events = []
    with pytest.raises(LLMError):
        async for e in run_agent_step(message="go", agent_id=agent["id"], session_id="s_err",
                                      server_module=_server(), agent_override=agent, stream_answer=True):
            events.append(e)
    assert [e["type"] for e in events if e["type"].startswith("answer_")] == ["answer_delta", "answer_reset"]
    assert events[-1]["reason"] == "error"


async def test_an_answer_the_loop_replaces_is_not_streamed(fake_llm):
    # Every turn is a tool call: the loop runs out and answers in its own words.
    call = tool_call("lookup", q="x")
    events = await _step(fake_llm, [Streamed(["Checking. ", call])] * 3, tools=True, max_turns=2)
    final = _final(events)
    assert final["streamed"] is False and "ran out of turns" in final["response"]
    assert _streamed(events) == ""


async def test_a_json_answer_is_released_whole(fake_llm):
    answer = json.dumps({"city": "Paris", "country": "France"})
    events = await _step(fake_llm, [Streamed([answer[:10], answer[10:]])])
    assert _streamed(events) == answer
    assert _final(events)["streamed"] is True


async def test_a_workflow_agents_final_says_it_did_not_stream(fake_llm):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop

    orch = S.make_orchestration(id="orch_u72", entry_step_id="answer", steps=[
        {"id": "answer", "name": "Answer", "type": "llm", "prompt_template": "Reply to: {state.user_input}",
         "model": "gpt-4o", "output_key": "reply", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    agent = S.make_orchestrator_agent("orch_u72", id="agent_orch_u72", name="Flow")
    await S.seed_agents([agent])
    fake_llm.script([Streamed(["From ", "the workflow."])])

    events = [e async for e in run_react_loop(
        ChatRequest(message="hi", session_id="s_u72w", agent_id=agent["id"]), _server(), stream_answer=True)]
    assert not [e for e in events if e["type"].startswith("answer_")]
    top = [e for e in events if e["type"] == "final" and not e.get("orch_step_id")]
    assert top and top[-1]["streamed"] is False


async def test_the_chat_entry_point_streams_a_plain_agent(fake_llm):
    from core.models import ChatRequest
    from core.react_engine import run_react_loop

    agent = S.make_agent(id="agent_u72", name="Plain", tools=["all"], skip_default_tools=True)
    await S.seed_agents([agent])
    fake_llm.script([Streamed(["Hello ", "there."])])
    events = [e async for e in run_react_loop(
        ChatRequest(message="hi", session_id="s_u72", agent_id=agent["id"]), _server(), stream_answer=True)]
    assert _streamed(events) == "Hello there."
    assert _final(events)["streamed"] is True
