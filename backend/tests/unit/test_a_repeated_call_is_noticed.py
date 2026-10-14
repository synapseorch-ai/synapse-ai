"""The same call, with the same arguments and the same result, is noticed (F243).

One turn made eleven consecutive identical `list_directory` calls, each with an
identical result, plus two 35k-character responses cut at the output cap, for
seven minutes, until Stop. Only `max_turns` (40) would have ended it.

A repeat is the same tool, the same arguments and the same result as the call
just before it. The second in a row tells the model it already has that answer;
the third ends the turn and says why. A tool run again with different
arguments — `run_bash` with another command — or the same command giving a
different result is never a repeat (Naveen, 2026-09-28).
"""
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _drive(fake_llm, script, executor, max_turns=10):
    from core.react_engine import run_agent_step

    agent = S.make_agent(tools=["all"], skip_default_tools=True)
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="look around", agent_id=agent["id"], session_id="s_f243", server_module=_server(),
        agent_override=agent, tool_executor=executor, max_turns=max_turns,
        tools_override=[{"type": "function", "function": {
            "name": "run_bash", "description": "b",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}}}}])]


async def _same(name, args):
    return "inventory/\ntests/"


def _final(events):
    return next(e["response"] for e in reversed(events) if e.get("type") == "final")


class TestTheSameCallThreeTimes:
    async def test_the_second_is_told_and_the_third_ends_the_turn(self, fake_llm):
        ls = tool_call("run_bash", command="ls work/inventory")
        events = await _drive(fake_llm, [ls, ls, ls, ls, ls, "never reached"], _same)

        assert fake_llm.call_count == 3, "no model call after the third identical result"
        assert "already have that answer" in fake_llm.calls[2]["prompt_msg"], "the second repeat was named"
        stopped = [e for e in events if e.get("type") == "repeat_stopped"]
        assert stopped and stopped[0]["tool_name"] == "run_bash" and stopped[0]["count"] == 3
        answer = _final(events)
        assert "run_bash" in answer and "same arguments" in answer and "3 times" in answer

    async def test_three_in_one_response_end_it_too(self, fake_llm):
        ls = {"tool": "run_bash", "arguments": {"command": "ls"}}
        import json
        batch = "\n".join(json.dumps(ls) for _ in range(4))
        events = await _drive(fake_llm, [batch, "never reached"], _same)
        assert fake_llm.call_count == 1
        assert [e for e in events if e.get("type") == "repeat_stopped"]


class TestNotARepeat:
    async def test_the_same_tool_with_other_arguments_runs_on(self, fake_llm):
        script = [tool_call("run_bash", command=f"cat file{i}.py") for i in range(5)] + ["done"]
        events = await _drive(fake_llm, script, _same)
        assert _final(events) == "done"
        assert not any("already have that answer" in c.get("prompt_msg", "") for c in fake_llm.calls)

    async def test_the_same_command_with_a_new_result_runs_on(self, fake_llm):
        seen = {"n": 0}

        async def clock(name, args):
            seen["n"] += 1
            return f"10:02:{seen['n']:02d}"

        date = tool_call("run_bash", command="date")
        events = await _drive(fake_llm, [date] * 5 + ["done"], clock)
        assert _final(events) == "done"
        assert not any("already have that answer" in c.get("prompt_msg", "") for c in fake_llm.calls)

    async def test_anything_in_between_starts_the_count_again(self, fake_llm):
        ls = tool_call("run_bash", command="ls")
        wait = tool_call("run_bash", command="sleep 10")
        events = await _drive(fake_llm, [ls, ls, wait, ls, ls, "done"], _same)
        assert _final(events) == "done"
        assert not [e for e in events if e.get("type") == "repeat_stopped"]


def _round(*commands):
    """One reply that makes several calls at once (a parallel round)."""
    import json
    return "\n".join(json.dumps({"tool": "run_bash", "arguments": {"command": c}}) for c in commands)


async def _per_command(name, args):
    return f"output of {args.get('command')}"


class TestARepeatedRound:
    """F476 (VOX1e, D263): gpt-4.1-mini with planning on made the same three searches (A, B, C) three replies in a
    row before answering, ~10 s of a phone caller's silence. The call guard compares a call with the one just
    before it, and in A,B,C,A,B,C,A,B,C no two neighbours are alike, so it never saw it. A round is one reply's
    calls, all of them: made again with the same arguments and the same results, the second is told and the third
    ends the turn, as a single call is."""

    async def test_the_second_round_is_told_and_the_third_ends_the_turn(self, fake_llm):
        abc = _round("search phone case", "search charger", "search watch")
        events = await _drive(fake_llm, [abc, abc, abc, abc, "never reached"], _per_command)

        assert fake_llm.call_count == 3, "no model call after the third identical round"
        told = fake_llm.calls[2]["prompt_msg"]
        assert "same 3 calls" in told and "already have those answers" in told, "the second round was named"
        assert "already have those answers" not in fake_llm.calls[1]["prompt_msg"]
        stopped = [e for e in events if e.get("type") == "repeat_stopped"]
        assert stopped and stopped[0]["count"] == 3 and stopped[0]["round"] == 3
        answer = _final(events)
        assert answer.startswith("I stopped here because I was repeating myself")
        assert "same 3 calls" in answer and "3 times" in answer

    async def test_the_note_comes_after_the_whole_round(self, fake_llm):
        """Not between its calls: the model reads it before its next reply, after every result."""
        abc = _round("search phone case", "search charger", "search watch")
        await _drive(fake_llm, [abc, abc, "done"], _per_command)
        told = fake_llm.calls[2]["prompt_msg"]
        assert told.rindex("already have those answers") > told.rindex("output of search watch")

    async def test_a_round_with_a_new_result_runs_on(self, fake_llm):
        seen = {"n": 0}

        async def ticking(name, args):
            seen["n"] += 1
            return f"{args.get('command')} at {seen['n']}"

        ab = _round("date", "uptime")
        events = await _drive(fake_llm, [ab] * 4 + ["done"], ticking)
        assert _final(events) == "done"
        assert not [e for e in events if e.get("type") == "repeat_stopped"]

    async def test_another_round_in_between_starts_the_count_again(self, fake_llm):
        ab, cd = _round("search a", "search b"), _round("search c", "search d")
        events = await _drive(fake_llm, [ab, ab, cd, ab, ab, "done"], _per_command)
        assert _final(events) == "done"
        assert not [e for e in events if e.get("type") == "repeat_stopped"]

    async def test_a_round_in_another_order_is_another_round(self, fake_llm):
        """The same calls asked in another order are read as a new plan, not a loop: only the same round, call
        for call, is a repeat."""
        events = await _drive(fake_llm, [_round("search a", "search b"), _round("search b", "search a"),
                                         _round("search a", "search b"), "done"], _per_command)
        assert _final(events) == "done"
