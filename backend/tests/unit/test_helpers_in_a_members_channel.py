"""A member's own messaging channel may send a helper to the background (S4, SUB1b, D281).

SUB1a gave background helpers to the app's chat only, and a channel ran them as a
wait (Naveen at SUB1a's plan: "app chat now, channels later"). A channel whose
senders are workspace members now has what the app's chat has: its turn has an
inbox, and a result that arrives after it answered comes back as one follow-up
turn of the agent, whose answer is sent to the same chat. A visitor's channel never
does (S6): it runs a helper while its turn waits.
"""
from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

from _fakes.routed_llm import RoutedLLM, install
from core.store import sessions as store
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))
AUTOMATED = "Automated input, not from the person"


class _Session:
    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"{name} says ok")], isError=False)


@pytest.fixture
def tools(monkeypatch):
    import core.react_engine as re_mod
    import core.server as server

    async def aggregated(server_module, agent, custom_tools, **_):
        held = [LOOKUP] if "lookup" in (agent.get("tools") or []) else []
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    monkeypatch.setattr(server, "agent_sessions", {"fake": _Session()}, raising=False)
    monkeypatch.setattr(server, "tool_router", {"lookup": ("fake", "lookup")}, raising=False)


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


@pytest.fixture
async def agent(seed_agent):
    return await seed_agent(name="Stockist help", tools=["spawn_subtask", "lookup"], skip_default_tools=True)


@pytest.fixture
def sent(monkeypatch):
    """What reached a chat, by (channel, chat)."""
    from core.messaging import manager as manager_mod

    got: list[tuple[str, str, str]] = []

    async def send_to_chat(channel_id, chat_id, text):
        got.append((channel_id, chat_id, text))

    monkeypatch.setattr(manager_mod, "send_to_chat", send_to_chat)
    return got


def spawn(task, tools, **args):
    return json.dumps({"tool": "spawn_subtask", "arguments": {"task": task, "tools": tools, **args}})


BACKGROUND = spawn("CHECK A", ["lookup"], name="30-day check", background=True)


async def eventually(check, timeout: float = 10.0, what: str = "it"):
    deadline = time.monotonic() + timeout
    while True:
        got = check()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} never happened")
        await asyncio.sleep(0.02)


def _offered_background(call) -> bool:
    for tool in call.get("tools") or []:
        if tool["function"]["name"] == "spawn_subtask":
            return "background" in tool["function"]["parameters"]["properties"]
    raise AssertionError("spawn_subtask was not offered")


def _manager():
    import core.server as server
    from core.messaging.manager import MessagingManager
    return MessagingManager(server_module=server)


class TestAMembersChannel:
    async def test_its_helpers_result_comes_back_once_to_the_same_chat(self, agent, tools, llm, sent):
        finish = asyncio.Event()

        async def helper_works(_kw):
            await finish.wait()
            return "3 of the 12 orders are past the 30-day rule."

        llm.on("PLAN IT", [BACKGROUND, "I've sent a helper; I'll come back with the late ones."])
        llm.on("CHECK A", [helper_works])
        llm.on(AUTOMATED, ["Three orders are late: HP-2044, HP-2049, HP-2051."], raw=True)

        # Bounded: run as a wait, the helper would hold the turn until `finish`, which only comes after it.
        reply = await asyncio.wait_for(
            _manager().run_agent("PLAN IT", agent["id"], "telegram_ch1_42", channel_id="ch1", chat_id="42"), 20)

        assert reply.startswith("I've sent a helper")
        assert _offered_background(llm.calls_for("PLAN IT")[0])
        assert not sent, "nothing more is said until the helper ends"
        finish.set()
        await eventually(lambda: sent, what="the follow-up in the chat")
        await asyncio.sleep(0.2)
        assert sent == [("ch1", "42", "Three orders are late: HP-2044, HP-2049, HP-2051.")]
        messages = (await store.get("telegram_ch1_42", agent["id"]))["messages"]
        question, answer = messages[-2:]
        assert question["automated"]["kind"] == "helper_result" and "3 of the 12" in question["content"]
        assert answer["content"].startswith("Three orders are late")
        assert llm.calls_for(AUTOMATED)[0]["prompt_msg"].startswith(AUTOMATED)

    async def test_a_follow_up_offers_no_background(self, agent, tools, llm, sent):
        finish = asyncio.Event()

        async def helper_works(_kw):
            await finish.wait()
            return "done"

        llm.on("PLAN IT", [BACKGROUND, "Sent."]).on("CHECK A", [helper_works])
        llm.on(AUTOMATED, ["It is done."], raw=True)
        await asyncio.wait_for(
            _manager().run_agent("PLAN IT", agent["id"], "telegram_ch1_43", channel_id="ch1", chat_id="43"), 20)
        finish.set()
        await eventually(lambda: sent, what="the follow-up")
        assert not _offered_background(llm.calls_for(AUTOMATED)[0])


class TestNoBackgroundElsewhere:
    async def test_a_visitors_channel_runs_it_as_a_wait(self, agent, tools, llm, sent):
        from core import visitor
        llm.on("PLAN IT", [BACKGROUND, "All checked."]).on("CHECK A", ["A is fine"])
        with visitor.scope(visitor.VisitorScope("channel", frozenset({"spawn_subtask", "lookup"}))):
            reply = await _manager().run_agent("PLAN IT", agent["id"], "telegram_ch2_7", channel_id="ch2",
                                               chat_id="7")
        assert reply == "All checked."
        assert not _offered_background(llm.calls_for("PLAN IT")[0])
        assert llm.calls_for("CHECK A"), "it ran, while the turn waited"
        await asyncio.sleep(0.1)
        assert not sent

    async def test_the_engine_refuses_it_on_a_visitors_turn_whoever_opened_an_inbox(self, agent, tools, llm):
        """A code guard, not the callers' care: an inbox handed to a visitor's turn offers no background."""
        import core.server as server
        from core import helpers, visitor
        from core.react_engine import run_agent_step

        llm.on("PLAN IT", [BACKGROUND, "All checked."]).on("CHECK A", ["A is fine"])
        inbox = helpers.Inbox(helpers.Conversation(session_id="s_engine_visitor", agent_key=agent["id"],
                                                   agent_id=agent["id"])).open()
        try:
            with visitor.scope(visitor.VisitorScope("widget", frozenset({"spawn_subtask", "lookup"}))):
                events = [e async for e in run_agent_step(message="PLAN IT", agent_id=agent["id"],
                                                          session_id="s_engine_visitor", server_module=server,
                                                          inbox=inbox, background=True)]
        finally:
            inbox.close()
        assert not _offered_background(llm.calls_for("PLAN IT")[0])
        assert not [e for e in events if e.get("type") == "helper_started" and e.get("mode") == "background"]

    async def test_a_caller_with_no_chat_runs_it_as_a_wait(self, agent, tools, llm, sent):
        llm.on("PLAN IT", [BACKGROUND, "All checked."]).on("CHECK A", ["A is fine"])
        reply = await _manager().run_agent("PLAN IT", agent["id"], "tg_1")
        assert reply == "All checked."
        assert not _offered_background(llm.calls_for("PLAN IT")[0])
