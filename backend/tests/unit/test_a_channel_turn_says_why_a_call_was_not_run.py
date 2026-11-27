"""A call that needs approval, in a conversation where nobody can approve it, is not run and is said (F1748).

A messaging channel's turn (WhatsApp, Telegram, Slack…) runs where no approval card can be answered. A call its
agent's policy holds for a person used to park there: the turn ended, the channel read no answer and sent nothing,
and nothing kept the call, so the person on the channel got silence. Now the call is not run, is recorded as refused,
and the agent is told it needs approval from someone on the team, which nobody can give in this conversation, in words
its reply can pass on to a customer as well as to an owner on their own channel.

Near misses read as before: the same call parks where a person can answer (a chat), an approved call runs, and a
denied one is blocked.
"""
from __future__ import annotations

import types

import httpx
import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call

SAID = "needs approval from someone on the team"


@pytest.fixture
def sent(monkeypatch):
    import core.http_tool as http_tool

    record = types.SimpleNamespace(calls=[])

    async def send(client, tool, args, variables, **kw):
        record.calls.append((tool["name"], args))
        return httpx.Response(200, text='{"ok": true}', request=httpx.Request("POST", tool["url"]))

    monkeypatch.setattr(http_tool, "send", send)
    return record


@pytest.fixture
async def desk():
    from core.routes.tools import _upsert_custom_tools

    await _upsert_custom_tools([{"name": "wipe_orders", "description": "Clears a day's orders", "tool_type": "http",
                                 "method": "POST", "url": "https://api.harborpine.example/wipe",
                                 "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}}}}])


def _agent(decision="confirm"):
    return S.make_agent(name="Order desk", tools=["wipe_orders"], skip_default_tools=True,
                        tool_policy={"tool:wipe_orders": decision})


async def _turn(fake_llm, script, *, agent=None, **kw):
    from core.react_engine import run_agent_step

    fake_llm.script(script)
    agent = agent or _agent()
    server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
    return [ev async for ev in run_agent_step(message="Clear Tuesday's orders.", agent_id=agent["id"],
                                              session_id="s_f1748", server_module=server, agent_override=agent,
                                              max_turns=4, **kw)]


def _asked(events):
    return [e for e in events if e.get("type") == "human_input_required"]


def _finals(events):
    return [e.get("response") for e in events if e.get("type") == "final"]


class TestWhereNobodyCanApprove:
    async def test_the_call_is_not_run_and_the_agent_says_why(self, fake_llm, sent, desk):
        from core.chat_approval import nobody_to_ask

        with nobody_to_ask():
            events = await _turn(fake_llm, [tool_call("wipe_orders", day="Tuesday"),
                                            "That needs a team member's approval, so I haven't cleared them."])
        assert sent.calls == [] and _asked(events) == []
        told = fake_llm.calls[-1].get("prompt_msg") or ""
        assert f"Tool 'wipe_orders' {SAID}" in told and "nobody can give it in this conversation" in told
        refused = [e for e in events if e.get("type") == "tool_result" and e.get("refused")]
        assert refused and refused[0]["reason"] == "it needs a team member's approval, which nobody can give here"
        assert _finals(events) == ["That needs a team member's approval, so I haven't cleared them."]

    async def test_a_channel_gets_the_agents_reply_not_silence(self, fake_llm, sent, desk):
        from core.messaging.manager import MessagingManager

        agent = _agent()
        await S.seed_agents([agent])
        fake_llm.script([tool_call("wipe_orders", day="Tuesday"), "It needs a team member's approval."])
        server = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        reply = await MessagingManager(server).run_agent("Clear Tuesday's orders.", agent["id"], "chan_f1748")
        assert reply == "It needs a team member's approval." and sent.calls == []


class TestNearMisses:
    async def test_where_a_person_can_answer_it_still_parks(self, fake_llm, sent, desk):
        events = await _turn(fake_llm, [tool_call("wipe_orders", day="Tuesday")], progress_events=True)
        assert _asked(events) and sent.calls == []

    async def test_an_approved_call_runs(self, fake_llm, sent, desk):
        from core.chat_approval import nobody_to_ask
        from core.react_engine import _approval_id

        args = {"day": "Tuesday"}
        key = _approval_id("wipe_orders", args)
        state = {"approved_calls": [key], "pending_call": {"tool": "wipe_orders", "arguments": args,
                                                           "approved": True, "approval_id": key}}
        with nobody_to_ask():
            await _turn(fake_llm, ["Cleared."], resume_state=state)
        assert sent.calls == [("wipe_orders", args)]

    async def test_a_denied_call_is_blocked_as_before(self, fake_llm, sent, desk):
        from core.chat_approval import nobody_to_ask

        with nobody_to_ask():
            events = await _turn(fake_llm, [tool_call("wipe_orders", day="Tuesday"), "Blocked."],
                                 agent=_agent("deny"))
        told = fake_llm.calls[-1].get("prompt_msg") or ""
        assert sent.calls == [] and "blocked by this agent's tool policy" in told and SAID not in told
        assert _asked(events) == []
