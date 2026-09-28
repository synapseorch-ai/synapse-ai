"""A follow-up turn sees what its own tools found (F242).

Each turn was saved with the tools it used, and the next turn was sent the
user's words and the answer only. Turn 2 found and fixed
`inventory/inventory/pricing.py`; turn 4 ("continue where you left off") and
turn 5 explored again from scratch, read a path that does not exist and never
recovered — about $0.8 of one blind turn on 2.5 Pro.

Now the recent turns carry a budgeted digest of their calls — tool, arguments
and the start of the result for the last few, names only before that, under a
hard cap — and `session_history` opens any earlier turn in full on demand.
The digest rides chat's history only: a workflow's `state.history` (F260) and a
delegate's empty history (F286) are unchanged.
"""
import json
import types

from _fakes import seed as S
from _fakes.fake_llm import tool_call


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


def _schema(name="list_dir"):
    return {"type": "function", "function": {
        "name": name, "description": "list a folder",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}


LISTING = "inventory/inventory/store.py\ninventory/inventory/pricing.py\ninventory/tests/test_store.py"


async def _turn(agent, fake_llm, script, message, *, session_id="s_f242", executor=None,
                tools_override=None, **kw):
    from core.react_engine import run_agent_step

    async def _default(name, args):
        return LISTING

    fake_llm.script(script)
    start = fake_llm.call_count
    events = [ev async for ev in run_agent_step(
        message=message, agent_id=agent["id"], session_id=session_id, server_module=_server(),
        agent_override=agent,
        tools_override=[_schema()] if tools_override is None else tools_override,
        tool_executor=executor or _default, max_turns=6, **kw)]
    return events, fake_llm.calls[start:]


def _tool_names(call):
    return [t.get("function", {}).get("name") for t in (call.get("tools") or [])]


class TestTheNextTurnSeesTheCalls:
    async def test_the_tool_its_arguments_and_what_it_returned(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, [tool_call("list_dir", path="work/inventory"), "Found the package."],
                    "where is the pricing code?")
        _, calls = await _turn(agent, fake_llm, ["It is in inventory/inventory/pricing.py."],
                               "continue where you left off")

        history = calls[0]["history_messages"]
        assert [m["role"] for m in history] == ["user", "assistant"]
        answer = history[1]["content"]
        assert answer.startswith("Found the package."), "the answer itself comes first, unchanged"
        assert "list_dir" in answer and "work/inventory" in answer, "the call and its arguments"
        assert "inventory/inventory/pricing.py" in answer, "what the call found"
        for message in history:
            assert set(message) == {"role", "content"} and isinstance(message["content"], str), (
                "providers get plain role/content text — Gemini breaks on anything else")

    async def test_a_turn_with_no_tools_is_sent_as_it_was(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, ["Hello."], "hi")
        _, calls = await _turn(agent, fake_llm, ["Still here."], "you there?")
        assert calls[0]["history_messages"] == [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello."}]

    async def test_a_blocked_call_is_kept_with_what_the_model_was_told(self, fake_llm):
        agent = S.make_agent(tools=["list_dir"], skip_default_tools=True)
        await _turn(agent, fake_llm, [tool_call("rm_rf", path="/"), "Could not."], "clean up",
                    tools_override=[_schema(), _schema("rm_rf")])
        _, calls = await _turn(agent, fake_llm, ["ok"], "why not?")
        answer = calls[0]["history_messages"][1]["content"]
        assert "rm_rf" in answer and "not available" in answer

    async def test_older_turns_keep_names_and_the_whole_digest_is_capped(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        for i in range(6):
            async def big(name, args, i=i):
                return f"RESULT-{i} " + "x" * 5000
            await _turn(agent, fake_llm, [tool_call("list_dir", path=f"dir{i}"), f"answer {i}"],
                        f"question {i}", executor=big)
        _, calls = await _turn(agent, fake_llm, ["ok"], "and now?")

        history = calls[0]["history_messages"]
        assistants = [m["content"] for m in history if m["role"] == "assistant"]
        assert "RESULT-5" in assistants[-1] and "dir5" in assistants[-1], "the newest turn in detail"
        assert "RESULT-0" not in assistants[0] and "list_dir" in assistants[0], (
            "an older turn keeps the tool's name, not its output")
        digest_chars = sum(len(a) - len(f"answer {i}") for i, a in enumerate(assistants))
        assert digest_chars <= 6500, digest_chars


class TestSessionHistory:
    async def test_it_lists_the_turns_and_opens_one(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await S.seed_agents([agent])

        async def secret(name, args):
            return "md5 b665e6c2 " + "y" * 3000 + " TAIL-OF-OUTPUT"

        await _turn(agent, fake_llm, [tool_call("list_dir", path="a"), "done"], "first", executor=secret)
        await _turn(agent, fake_llm, ["second answer"], "second")
        _, calls = await _turn(agent, fake_llm, [
            tool_call("session_history"), tool_call("session_history", turn=1), "It was b665e6c2."],
            "what did the first turn find?")

        assert "session_history" in _tool_names(calls[0]), "offered once there is an earlier turn"
        listing, opened = calls[1]["prompt_msg"], calls[2]["prompt_msg"]
        assert "Turn 1" in listing and "Turn 2" in listing and "first" in listing
        assert "TAIL-OF-OUTPUT" in opened, "a turn opens with its calls' results in full"
        assert '"a"' in opened or "path" in opened

    async def test_it_is_not_offered_to_a_new_conversation(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        _, calls = await _turn(agent, fake_llm, ["hi"], "first message", session_id="s_new")
        assert "session_history" not in _tool_names(calls[0])

    async def test_a_delegate_is_neither_given_the_digest_nor_the_tool(self, fake_llm):
        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, [tool_call("list_dir", path="a"), "done"], "first")
        _, calls = await _turn(agent, fake_llm, ["ok"], "second", history_override=[], persist_turn=False)
        assert calls[0]["history_messages"] == []
        assert "session_history" not in _tool_names(calls[0])


class TestTheWorkflowsHistoryIsUnchanged:
    async def test_state_history_stays_role_and_content(self, fake_llm):
        """F260's `state.history` and its CONVERSATION SO FAR read this."""
        from core.session import _save_conversation_turn, get_recent_history_messages

        await _save_conversation_turn("s_plain", "a1", "q", "a", tools=["t: out"],
                                      calls=[{"tool": "t", "args": {"k": 1}, "result": "out"}])
        assert await get_recent_history_messages("s_plain", agent_id="a1") == [
            {"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


class TestTheRecordIsKept:
    async def test_the_saved_turn_holds_each_call(self, fake_llm):
        from core.session import _get_conversation_history

        agent = S.make_agent(tools=["all"], skip_default_tools=True)
        await _turn(agent, fake_llm, [tool_call("list_dir", path="work"), "done"], "look",
                    session_id="s_rec")
        (turn,) = await _get_conversation_history("s_rec", agent["id"])
        (call,) = turn["calls"]
        assert call["tool"] == "list_dir" and call["args"] == {"path": "work"}
        assert "inventory/inventory/pricing.py" in call["result"]
        assert turn["tools"], "the older `tools` field is still written"
        json.dumps(turn)  # stored as JSON
