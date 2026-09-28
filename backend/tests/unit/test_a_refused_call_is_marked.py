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
