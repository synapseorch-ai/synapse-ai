"""A read repeated anywhere in a step is noticed, unless something between could have changed it (F1236; Naveen 10-29).

The repeat guard (F243) counted only the same call as the one just before it, so reads interleaved — A, B, A, B —
were never repeats: Naveen's prod run read the workspace 7 times in one step. Naveen's rule: "reads count, waits
reset". A call that only reads (a trusted read-only declaration, or one of the builder's readers) with the same
arguments and the same result as one since the last call that could have changed things is a repeat, however many
reads sit between; the same thresholds and answer turn follow. A wait, a write, or any call nothing marks as
read-only starts that window again, so polling — `ls`, `sleep`, `ls` — runs on as before; and a call in a row with
the same result still counts as it did. A read whose result changed is never a repeat. A resumed step goes on
counting.
"""
from __future__ import annotations

import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call

_READ = {"type": "function", "function": {"name": "read_workspace", "description": "r", "parameters": {
    "type": "object", "properties": {"topic": {"type": "string"}}}}}
_BASH = {"type": "function", "function": {"name": "run_bash", "description": "b", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _same(name, args):
    return f"{args.get('topic')}: 3 rows" if name == "read_workspace" else "ok"


async def _drive(fake_llm, script, executor=_same, *, resume_state=None, progress=False, max_turns=12):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="what is here?", agent_id=agent["id"], session_id="s_f1236", server_module=_server(),
        agent_override=agent, tool_executor=executor, max_turns=max_turns, tools_override=[_READ, _BASH],
        resume_state=resume_state, progress_events=progress)]


def _stopped(events):
    return [e for e in events if e.get("type") == "repeat_stopped"]


def _fired(events) -> bool:
    """The guard acted: F1015's answer turn began (a model that then answers in words ends there), or it stopped."""
    return bool(_stopped(events)) or any(
        e.get("type") == "status" and e.get("message") == "Answering from what it already has" for e in events)


def _final(events):
    return next(e["response"] for e in reversed(events) if e.get("type") == "final")


AGENTS, FLOWS = tool_call("read_workspace", topic="agents"), tool_call("read_workspace", topic="workflows")


async def test_naveen_s_prod_shape_stops_at_the_third_read(fake_llm):
    """Seven interleaved workspace reads in one step: the third identical one ends the reading."""
    events = await _drive(fake_llm, [AGENTS, FLOWS, AGENTS, FLOWS, AGENTS, FLOWS, AGENTS, "never reached"])
    [stopped] = _stopped(events) or [None]
    assert stopped and stopped["tool_name"] == "read_workspace" and stopped["count"] == 3
    assert any("nothing between that could have changed it" in c.get("prompt_msg", "") for c in fake_llm.calls), \
        "the second interleaved repeat is named first"
    assert fake_llm.call_count <= 6, "it never reaches the seventh read"


async def test_polling_with_a_wait_between_runs_on(fake_llm):
    """run_bash is marked nothing: `sleep` (and `ls` itself) could change things, so the window starts again."""
    ls, wait = tool_call("run_bash", command="ls"), tool_call("run_bash", command="sleep 10")
    events = await _drive(fake_llm, [ls, wait, ls, wait, ls, wait, ls, "done"])
    assert _final(events) == "done" and not _fired(events)


async def test_a_call_that_could_change_things_between_reads_starts_again(fake_llm):
    write = tool_call("run_bash", command="echo x >> notes.txt")
    events = await _drive(fake_llm, [AGENTS, FLOWS, AGENTS, write, AGENTS, FLOWS, "done"])
    assert _final(events) == "done" and not _fired(events)


async def test_a_read_whose_result_changed_is_not_a_repeat(fake_llm):
    seen = {"n": 0}

    async def polling(name, args):
        seen["n"] += 1
        return f"run_1: step {seen['n']} of 9"
    events = await _drive(fake_llm, [AGENTS, FLOWS] * 3 + ["done"], polling)
    assert _final(events) == "done" and not _fired(events)


async def test_in_a_row_still_counts_as_before(fake_llm):
    events = await _drive(fake_llm, [AGENTS, AGENTS, AGENTS, "From what I have: 3 agents."])
    assert _fired(events) and _final(events) == "From what I have: 3 agents."


async def test_a_resumed_step_goes_on_counting(fake_llm):
    first = await _drive(fake_llm, [AGENTS, FLOWS, AGENTS, "paused here"], progress=True)
    snapshot = [{k: v for k, v in e.items() if k != "type"} for e in first if e.get("type") == "_step_progress"][-1]
    assert snapshot["repeat_window"], "the step's snapshot keeps the reads it saw"
    fake_llm.reset()
    events = await _drive(fake_llm, [AGENTS, "From what I have."], resume_state=snapshot)
    assert _fired(events), "the third read after the resume is the guard's"
