"""A tool call that was not run says so, everywhere a person or a caller reads it (U68).

With the call denied by policy, one model still answered "done", and nothing
contradicted it: the chat showed a success tick beside the refused tool, the
saved turn's call record carried no flag, and v1's response had no refusal
information at all.

Now each refusal point marks its `tool_result` (`refused`, `reason`), the call
record the turn keeps carries the same, chat's stream forwards it, and v1's
answer lists `refused_calls`. The model's own words are left as they are.
"""
import types
import unittest.mock as _m

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

DANGER = {"type": "function", "function": {"name": "danger", "description": "deletes rows",
                                           "parameters": {"type": "object", "properties": {}}}}
AGENT = "agent_u68"


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


@pytest.fixture
async def denied():
    agent = S.make_agent(id=AGENT, name="Cleaner", tools=["all"], skip_default_tools=True,
                         tool_policy={"undeclared": "deny"})
    await S.seed_agents([agent])
    return agent


@pytest.fixture
def ran():
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    calls: list[str] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == AGENT:
            async def executor(name, args):
                calls.append(name)
                return "deleted"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield calls


class TestTheEngineMarksIt:
    async def test_a_policy_refusal(self, denied, ran, fake_llm):
        from core.react_engine import run_agent_step
        fake_llm.script([tool_call("danger"), "Done — the rows are deleted."])
        events = [e async for e in run_agent_step(message="clean up", agent_id=AGENT, session_id="s_u68",
                                                  server_module=_server())]
        [result] = [e for e in events if e.get("type") == "tool_result"]
        assert result["refused"] is True and "policy" in result["reason"]
        assert ran == []

        from core.session import _get_conversation_history
        [turn] = await _get_conversation_history("s_u68", AGENT)
        [call] = turn["calls"]
        assert call["tool"] == "danger" and call["refused"] is True and "policy" in call["reason"]
        assert turn["assistant"] == "Done — the rows are deleted.", "the model's words are left alone"

    async def test_a_tool_the_agent_does_not_hold(self, fake_llm):
        from core.react_engine import run_agent_step
        agent = S.make_agent(id="agent_u68_b", tools=["list_dir"], skip_default_tools=True)
        fake_llm.script([tool_call("rm_rf", path="/"), "Could not."])
        events = [e async for e in run_agent_step(
            message="clean", agent_id=agent["id"], session_id="s_u68b", server_module=_server(),
            agent_override=agent, tools_override=[DANGER, {**DANGER, "function": {**DANGER["function"], "name": "rm_rf"}}],
            tool_executor=lambda n, a: None)]
        [result] = [e for e in events if e.get("type") == "tool_result"]
        assert result["refused"] is True

    async def test_a_call_that_ran_is_not_marked(self, fake_llm):
        from core.react_engine import run_agent_step
        agent = S.make_agent(id="agent_u68_c", tools=["all"], skip_default_tools=True)

        async def executor(name, args):
            return "ok"

        fake_llm.script([tool_call("danger"), "Done."])
        events = [e async for e in run_agent_step(
            message="go", agent_id=agent["id"], session_id="s_u68c", server_module=_server(),
            agent_override=agent, tools_override=[DANGER], tool_executor=executor)]
        [result] = [e for e in events if e.get("type") == "tool_result"]
        assert "refused" not in result


class TestTheCallersAreTold:
    async def test_chat_streams_the_mark(self, client, denied, ran, fake_llm):
        fake_llm.script([tool_call("danger"), "Done."])
        resp = await client.post("/chat/stream", json={"message": "clean up", "agent_id": AGENT, "session_id": "s_u68d"})
        assert '"refused": true' in resp.text

    async def test_v1_lists_refused_calls(self, client, api_key, denied, ran, fake_llm):
        fake_llm.script([tool_call("danger"), "Done — deleted."])
        resp = await client.post("/api/v1/chat", json={"message": "clean up", "agent": AGENT, "session_id": "v1_u68"},
                                 headers=api_key["headers"])
        body = resp.json()
        assert body["response"] == "Done — deleted."
        [refused] = body["refused_calls"]
        assert refused["tool_name"] == "danger" and refused["category"] == "undeclared"
        assert "policy" in refused["reason"]

    async def test_v1_says_none_when_none_were(self, client, api_key, fake_llm):
        await S.seed_agents([S.make_agent(id="agent_u68_e", tools=[], skip_default_tools=True)])
        fake_llm.script(["Hello."])
        resp = await client.post("/api/v1/chat", json={"message": "hi", "agent": "agent_u68_e"},
                                 headers=api_key["headers"])
        assert resp.json()["refused_calls"] == []


# ── a person's refusal (F306) ────────────────────────────────────────────────
#
# After Refuse on an approval card, the resumed turn only told the model: no
# `tool_result`, no call on the saved turn — so only the card said "Refused",
# U68's "not run" list under the answer left it out, and v1 said nothing.

ASKS = "agent_f306"


@pytest.fixture
async def asks():
    agent = S.make_agent(id=ASKS, name="Careful Cleaner", tools=["all"], skip_default_tools=True,
                         tool_policy={"undeclared": "confirm"})
    await S.seed_agents([agent])
    return agent


@pytest.fixture
def ran_asks():
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    calls: list[str] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == ASKS:
            async def executor(name, args):
                calls.append(name)
                return "deleted"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield calls


def _refused_resume(reason=""):
    from core.chat_approval import resume_state_for
    from core.react_engine import _approval_id
    return resume_state_for({
        "tool_name": "danger", "tool_args": {"table": "users"}, "category": "destructive",
        "approval_id": _approval_id("danger", {"table": "users"}),
        "progress": {"turn": 1, "context_text": "User Request: clean up\n", "calls": []},
    }, approve=False, reason=reason)


class TestAPersonsRefusal:
    async def test_the_engine_records_it(self, fake_llm):
        from core.react_engine import run_agent_step
        from core.session import _get_conversation_history
        agent = S.make_agent(id="agent_f306_a", tools=["all"], skip_default_tools=True)
        fake_llm.script(["I left the table alone."])
        events = [e async for e in run_agent_step(
            message="clean up", agent_id=agent["id"], session_id="s_f306a", server_module=_server(),
            agent_override=agent, tools_override=[DANGER], tool_executor=lambda n, a: None,
            resume_state=_refused_resume("not on production"))]

        execs = [e for e in events if e.get("type") == "tool_execution"]
        [result] = [e for e in events if e.get("type") == "tool_result"]
        assert [(e["tool_name"], e["args"]) for e in execs] == [("danger", {"table": "users"})]
        assert result["refused"] is True
        assert result["reason"] == "the person refused it: not on production"
        assert result["category"] == "destructive"
        assert fake_llm.call_count == 1, "the model is still asked, once, to carry on without it"

        [turn] = await _get_conversation_history("s_f306a", agent["id"])
        [call] = turn["calls"]
        assert call["tool"] == "danger" and call["refused"] is True
        assert call["reason"] == "the person refused it: not on production"

    async def test_a_refusal_with_no_reason_says_who(self, fake_llm):
        from core.react_engine import run_agent_step
        agent = S.make_agent(id="agent_f306_b", tools=["all"], skip_default_tools=True)
        fake_llm.script(["Fine."])
        events = [e async for e in run_agent_step(
            message="clean up", agent_id=agent["id"], session_id="s_f306b", server_module=_server(),
            agent_override=agent, tools_override=[DANGER], tool_executor=lambda n, a: None,
            resume_state=_refused_resume())]
        [result] = [e for e in events if e.get("type") == "tool_result"]
        assert result["reason"] == "the person refused it"

    async def test_chat_streams_it_and_the_saved_turn_keeps_it(self, client, asks, ran_asks, fake_llm):
        from core.session import _get_conversation_history
        fake_llm.script([tool_call("danger"), "I left the table alone."])
        parked = await client.post("/chat/stream", json={"message": "clean up", "agent_id": ASKS,
                                                          "session_id": "s_f306c"})
        assert '"human_input_required"' in parked.text or '"tool_approval"' in parked.text
        answered = await client.post("/chat/s_f306c/approve", json={"approve": False, "reason": "not today",
                                                                     "agent_id": ASKS})
        assert answered.status_code == 200
        assert '"refused": true' in answered.text
        assert ran_asks == [], "a refused call never runs"
        turns = await _get_conversation_history("s_f306c", ASKS)
        refused = [c for t in turns for c in t.get("calls", []) if c.get("refused")]
        assert [(c["tool"], c["reason"]) for c in refused] == [("danger", "the person refused it: not today")]

    async def test_v1_streams_it(self, client, api_key, asks, ran_asks, fake_llm):
        fake_llm.script([tool_call("danger"), "Left it."])
        parked = await client.post("/api/v1/chat", json={"message": "clean up", "agent": ASKS,
                                                          "session_id": "v1_f306"}, headers=api_key["headers"])
        assert parked.json()["status"] == "awaiting_approval"
        answered = await client.post("/api/v1/chat/v1_f306/approve", json={"approve": False},
                                     headers=api_key["headers"])
        assert '"refused": true' in answered.text
        assert ran_asks == []

    async def test_a_call_moved_past_is_marked_not_run(self):
        """F247's record of a card nobody answered: not run, and why (U68's flags)."""
        from core.chat_approval import MOVED_ON, moved_past_turn
        [_question, answer] = moved_past_turn(
            {"tool_name": "danger", "tool_args": {}, "approval_id": "x", "message": "clean up",
             "progress": {}}, why=MOVED_ON)
        call = answer["calls"][-1]
        assert call["tool"] == "danger" and call["refused"] is True and call["reason"] == MOVED_ON
