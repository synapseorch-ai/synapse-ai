"""A read repeated with only its letters' case changed is a repeat (F1495, C1; TRI2's class 2, pro
drafts/tri2/TRIAGE.md; Naveen's pick 11-10).

TRI2's prod run (run_01a1bf14…, 11-09): the planner, on Sonnet 4.6, read `read_workspace {topic: tool, name: github}`
three times and `{…, name: GitHub}` twice among nine reads. The repeat guard (F243, F1015, F1236's read window) keys a
read by its tool, its arguments and its result, and the result echoes the name asked ("No custom tool is called
'GitHub'."), so `github` and `GitHub` were two reads, and the guard asked for the answer only after the eleventh turn.
Now both are compared case-folded: a read that differs only in case reads nothing new, and the third asks for the
answer from what it has. Arguments or results that differ in more than case are never a repeat.
"""
import json
import types

from _fakes import seed as S


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _drive(fake_llm, script, executor, max_turns=10):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="find the GitHub tool", agent_id=agent["id"], session_id="s_f1495", server_module=_server(),
        agent_override=agent, tool_executor=executor, max_turns=max_turns,
        tools_override=[{"type": "function", "function": {
            "name": "read_workspace", "description": "Reads the workspace by topic",
            "parameters": {"type": "object", "properties": {"topic": {"type": "string"},
                                                            "name": {"type": "string"}}}}}])]


async def _no_tool_called(name, args):
    """`read_workspace`'s answer for a tool that is not here: it echoes the name asked."""
    return {"topic": "tool", "error": f"No custom tool is called {args.get('name')!r}."}


def _final(events):
    return next(e["response"] for e in reversed(events) if e.get("type") == "final")


def _read(name: str | None = None, topic: str = "tool") -> str:
    """One `read_workspace` call, as the engine parses it (the fakes' `tool_call` shape: its own first parameter is
    `name`, so a call with a `name` argument is written here)."""
    return json.dumps({"tool": "read_workspace", "arguments": {"topic": topic, **({"name": name} if name else {})}})


async def test_github_github_github_asks_for_the_answer(fake_llm):
    events = await _drive(fake_llm, [_read("github"), _read("github"), _read("GitHub"), "From what I have."],
                          _no_tool_called)
    assert fake_llm.call_count == 4
    assert "Give your final response now" in fake_llm.calls[3]["prompt_msg"], "the third read is a repeat"
    assert not fake_llm.calls[3].get("tools"), "the answer turn offers no tool"
    assert _final(events) == "From what I have."


async def test_prods_order_asks_by_the_third(fake_llm):
    """Not side by side: the read window counts a run of reads (F1236), whatever comes between."""
    script = [_read("github"), _read(topic="agents"), _read("GitHub"), _read("GITHUB"),
              "From what I have."]
    await _drive(fake_llm, script, _no_tool_called)
    assert "Give your final response now" in fake_llm.calls[4]["prompt_msg"]


async def test_another_name_is_never_a_repeat(fake_llm):
    script = [_read("github"), _read("gitlab"), _read("bitbucket"), _read("GitHub Actions"), "done"]
    events = await _drive(fake_llm, script, _no_tool_called)
    assert _final(events) == "done"
    assert not any("Give your final response now" in c.get("prompt_msg", "") for c in fake_llm.calls)
