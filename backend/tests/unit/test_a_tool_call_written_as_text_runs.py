"""A tool call a model writes as text, in OpenAI's own parallel-call shape, runs; it is never the answer (F537).

TEAM0 measured 1 in 12 single-agent runs on gpt-4.1-mini (`MEMORY/drafts/team0/measure/results/
research-single-1-mini-2.json`): the model wrote its parallel calls as text, in the pseudo-format OpenAI uses inside
its own tool runner (`{"tool_uses": [{"recipient_name": "functions.<tool>", "parameters": {…}}]}`), twice. The engine
reads a call as `{"tool": …}`, so it found none, the parse retry's pattern (`"tool"\\s*:`) did not match
`"tool_uses":`, and the reply was taken as the agent's answer: one model call, no tool ran, quality 0/6. While
streaming, the answer filter released it to the person as text.

Naveen's call (BF6): recognise the shape and run the calls it names. `functions.` is OpenAI's namespace, not part of
the name. A block written twice runs its calls once. A malformed one gets the parse retry, as `{"tool": …}` does.
"""
import json
import types

import pytest

from _fakes import seed as S
from core.answer_filter import AnswerFilter
from core.react_engine import looks_like_a_tool_call, parse_all_tool_calls, parse_tool_call

BLOCK = json.dumps({"tool_uses": [
    {"recipient_name": "functions.lookup_product", "parameters": {"name": "hubspot"}},
    {"recipient_name": "functions.lookup_product", "parameters": {"name": "pipedrive"}},
    {"recipient_name": "functions.lookup_product", "parameters": {"name": "zoho"}},
]}, separators=(",", ":"))
#: TEAM0's reply, as the model wrote it.
TEAM0 = f"{BLOCK}\n\n{BLOCK}"

THREE = [{"tool": "lookup_product", "arguments": {"name": n}} for n in ("hubspot", "pipedrive", "zoho")]


def test_teams_reply_is_three_calls_run_once():
    assert parse_all_tool_calls(TEAM0) == THREE


def test_the_first_call_is_found_too():
    call, _ = parse_tool_call(TEAM0)
    assert call == THREE[0]


def test_the_multi_tool_use_wrapper_is_its_calls():
    wrapped = json.dumps({"recipient_name": "multi_tool_use.parallel", "parameters": json.loads(BLOCK)})
    assert parse_all_tool_calls(wrapped) == THREE


def test_one_recipient_is_one_call():
    one = json.dumps({"recipient_name": "functions.lookup_product", "parameters": {"name": "zoho"}})
    assert parse_all_tool_calls(one) == [THREE[2]]


def test_a_call_written_twice_in_the_engines_own_shape_still_runs_twice():
    # Unchanged: only the pseudo-format, which the model doubled, is run once.
    call = json.dumps({"tool": "lookup_product", "arguments": {"name": "zoho"}})
    assert parse_all_tool_calls(f"{call}\n{call}") == [THREE[2], THREE[2]]


def test_json_that_is_not_a_call_is_still_not_one():
    for text in ('{"tool_uses": []}', '{"tool_uses": "none"}', '{"recipient_name": 7}',
                 '{"answer": {"recipient_name": "Ana"}}', '{"recipient_name": "", "parameters": {}}'):
        assert parse_all_tool_calls(text) == [], text


def test_a_malformed_one_gets_the_parse_retry():
    assert looks_like_a_tool_call(BLOCK[:-12])
    assert looks_like_a_tool_call('{"recipient_name": "functions.lookup_product", "parameters": {"name": "zoho",}}')


def _shown(pieces):
    f = AnswerFilter()
    ops = []
    for p in pieces:
        ops += f.feed(p)
    if not f.stopped:
        ops += f.close()
    shown = ""
    for op in ops:
        shown = shown + op[1] if op[0] == "text" else ""
    return shown


@pytest.mark.parametrize("cut", [None, 1, 7, 15, 40, len(BLOCK) - 3])
def test_the_stream_never_shows_it(cut):
    pieces = [TEAM0] if cut is None else [TEAM0[:cut], TEAM0[cut:]]
    assert _shown(pieces) == ""
    assert _shown(list(TEAM0)) == ""


def test_prose_before_it_streams_and_it_does_not():
    assert _shown(["Let me look these up. ", BLOCK]) == "Let me look these up. "


async def test_the_turn_runs_the_calls_and_answers_after(fake_llm):
    from core.react_engine import run_agent_step

    ran = []

    async def executor(name, args):
        ran.append((name, args))
        return f"{args['name']}: $15 a seat"

    schema = {"type": "function", "function": {"name": "lookup_product", "description": "Look a product up.",
                                               "parameters": {"type": "object",
                                                              "properties": {"name": {"type": "string"}}}}}
    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script([TEAM0, "HubSpot, Pipedrive and Zoho are each $15 a seat."])
    events = [ev async for ev in run_agent_step(
        message="Compare hubspot, pipedrive and zoho.", agent_id=agent["id"], session_id="s_f537",
        server_module=types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}),
        agent_override=agent, tools_override=[schema], tool_executor=executor, max_turns=4)]
    assert ran == [("lookup_product", {"name": n}) for n in ("hubspot", "pipedrive", "zoho")]
    final = [e for e in events if e.get("type") == "final"]
    assert final and final[-1]["response"] == "HubSpot, Pipedrive and Zoho are each $15 a seat."
    assert "tool_uses" not in json.dumps([e for e in events if e.get("type") in ("final", "answer_delta")])
