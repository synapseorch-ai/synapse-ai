"""A helper can work in the background, and its result comes back exactly once (SUB1a, D279; S2, S3, S4, S6).

`spawn_subtask` with `background: true`, in the app's chat: the helper runs as a chat
turn of its own (`helperc_…`), the agent's turn goes on and may end, and the result
comes back once, labelled as the helper's and never as the person's:

* while that agent's turn is running there, between its model calls (its inbox);
* while a question is parked there, kept with it — read when it is answered, or by the
  next turn when the person moves past it;
* otherwise as one follow-up turn of the agent, which waits for any turn running there.

Never on a visitor's turn, a workflow step, OAI1, `/api/v1` or a channel, where it runs
while its parent waits. A server restart leaves it failed, and says so in its parent's
conversation.
"""
from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from core import chat_turns, helpers
from core.store import sessions as store
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))
DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))
AUTOMATED = "Automated input, not from the person"


class _Session:
    def __init__(self):
        self.calls: list[str] = []

    async def send_ping(self):
        return None

    async def call_tool(self, name, args, **_kw):
        self.calls.append(name)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=f"{name} says ok")], isError=False)


@pytest.fixture
def tools(monkeypatch):
    import core.react_engine as re_mod
    import core.server as server

    async def aggregated(server_module, agent, custom_tools, **_):
        wanted = agent.get("tools") or []
        held = [t for t in (LOOKUP, DANGER) if "all" in wanted or t.name in wanted]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    session = _Session()
    monkeypatch.setattr(server, "agent_sessions", {"fake": session}, raising=False)
    monkeypatch.setattr(server, "tool_router", {"lookup": ("fake", "lookup"), "danger": ("fake", "danger")},
                        raising=False)
    return session


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


@pytest.fixture
async def agent(seed_agent):
    return await seed_agent(name="Stockist help", tools=["spawn_subtask", "lookup", "danger"],
                            skip_default_tools=True, tool_policy={"destructive": "confirm"})


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


async def _messages(session_id, agent_id):
    row = await store.get(session_id, agent_id)
    return (row or {}).get("messages") or []


async def _turns(session_id, agent_id):
    from core.session import _get_conversation_history
    return await _get_conversation_history(session_id, agent_id)


async def _chat(client, agent, message, session_id):
    reply = await client.post("/chat/stream", json={"message": message, "session_id": session_id,
                                                    "agent_id": agent["id"]})
    assert reply.status_code == 200, reply.text
    return [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]


def _offered_background(call) -> bool:
    for tool in call.get("tools") or []:
        if tool["function"]["name"] == "spawn_subtask":
            return "background" in tool["function"]["parameters"]["properties"]
    raise AssertionError("spawn_subtask was not offered")


class TestWhereBackgroundIsOffered:
    async def test_the_apps_chat_offers_it(self, client, agent, tools, llm):
        llm.on("PLAN IT", ["no helper today"])
        await _chat(client, agent, "PLAN IT", "s_bg_offer")
        assert _offered_background(llm.calls_for("PLAN IT")[0])

    async def test_a_turn_with_no_inbox_does_not_and_runs_one_asked_for_as_a_wait(self, agent, tools, llm):
        """An API caller, a channel, a schedule: `run_react_loop` without the app's mark."""
        import core.server as server
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        llm.on("PLAN IT", [BACKGROUND, "all done"]).on("CHECK A", ["A is fine"])
        request = ChatRequest(message="PLAN IT", session_id="s_bg_api", agent_id=agent["id"])
        events = [e async for e in run_react_loop(request, server)]
        assert not _offered_background(llm.calls_for("PLAN IT")[0])
        assert [e for e in events if e.get("type") == "helper_finished"][0]["record"]["mode"] == "wait"

    async def test_a_visitors_turn_does_not(self, agent, tools, llm):
        """S6: an anonymous visitor cannot start work that outlives their message."""
        import core.server as server
        from core import visitor
        from core.models import ChatRequest
        from core.react_engine import run_react_loop

        await store.append_turn("s_bg_visitor", agent["id"], [])
        llm.on("PLAN IT", [BACKGROUND, "all done"]).on("CHECK A", ["A is fine"])
        request = ChatRequest(message="PLAN IT", session_id="s_bg_visitor", agent_id=agent["id"])
        request._app_chat = True
        with visitor.scope(visitor.VisitorScope("widget", frozenset({"spawn_subtask", "lookup"}))):
            events = [e async for e in run_react_loop(request, server)]
        assert not [e for e in events if e.get("type") == "helper_started" and e.get("mode") == "background"]
        assert not helpers._OPEN


class TestTheResultComesBackOnce:
    async def test_after_the_turn_ends_it_is_one_follow_up_turn(self, client, agent, tools, llm):
        finish = asyncio.Event()

        async def helper_works(_kw):
            await finish.wait()
            return "3 of the 12 orders are past the 30-day rule."

        llm.on("PLAN IT", [BACKGROUND, "I've sent a helper; I'll come back with the late ones."])
        llm.on("CHECK A", [helper_works])
        llm.on(AUTOMATED, ["Three orders are late: HP-2044, HP-2049, HP-2051."], raw=True)
        frames = await _chat(client, agent, "PLAN IT", "s_bg_follow")
        started = [f for f in frames if f["type"] == "helper_started"][0]
        assert started["mode"] == "background" and started["session_id"].startswith("helperc_")
        assert [f for f in frames if f["type"] == "response"][0]["content"].startswith("I've sent a helper")
        assert len(await _turns("s_bg_follow", agent["id"])) == 1, "the turn ended while the helper works"

        finish.set()
        turns = await eventually(lambda: _two(agent), what="the follow-up")
        follow = turns[1]
        assert follow["automated"]["kind"] == "helper_result"
        assert follow["automated"]["name"] == "30-day check" and follow["automated"]["status"] == "completed"
        assert "3 of the 12 orders" in follow["user"]
        assert follow["assistant"] == "Three orders are late: HP-2044, HP-2049, HP-2051."
        prompt = llm.calls_for(AUTOMATED)[0]["prompt_msg"]
        assert prompt.startswith(AUTOMATED) and "30-day check" in prompt
        await asyncio.sleep(0.2)
        assert len(await _turns("s_bg_follow", agent["id"])) == 2, "exactly one follow-up"

    async def test_while_the_turn_runs_it_is_read_between_its_model_calls(self, client, agent, tools, llm):
        async def wait_for_it(_kw):
            await eventually(lambda: any(i._pending for i in helpers._OPEN.values()), what="the result")
            return tool_call("lookup", item="B")

        llm.on("PLAN IT", [BACKGROUND, wait_for_it, "HP-2044 is late, and lookup B is fine."])
        llm.on("CHECK A", ["HP-2044 is late."])
        await _chat(client, agent, "PLAN IT", "s_bg_inbox")
        third = llm.calls_for("PLAN IT")[2]["prompt_msg"]
        assert helpers.INBOX_HEADING in third and "HP-2044 is late." in third
        assert "not from the person" in third
        await asyncio.sleep(0.2)
        turns = await _turns("s_bg_inbox", agent["id"])
        assert len(turns) == 1, "read by the turn, so no follow-up"
        messages = await _messages("s_bg_inbox", agent["id"])
        helper_id = turns[0]["helpers"][0]["id"]
        assert messages[-1]["inbox_read"] == [helper_id]

    async def test_a_parked_question_keeps_it_and_its_answer_reads_it(self, client, agent, tools, llm):
        async def helper_works(_kw):
            await eventually(lambda: store.get_pending_approval("s_bg_park", agent["id"]), what="the park")
            return "HP-2044 is late."

        llm.on("PLAN IT", [BACKGROUND, tool_call("danger"), "Done: and HP-2044 is late."])
        llm.on("CHECK A", [helper_works])
        frames = await _chat(client, agent, "PLAN IT", "s_bg_park")
        assert any(f["type"] == "human_input_required" for f in frames)
        await eventually(lambda: _parked_inbox("s_bg_park", agent["id"]), what="the result kept with the question")
        assert len(await _turns("s_bg_park", agent["id"])) == 0, "no follow-up over a parked question"

        reply = await client.post("/chat/s_bg_park/approve", json={"approve": True, "agent_id": agent["id"]})
        assert reply.status_code == 200
        last = llm.calls_for("PLAN IT")[-1]["prompt_msg"]
        assert helpers.INBOX_HEADING in last and "HP-2044 is late." in last
        await asyncio.sleep(0.2)
        assert len(await _turns("s_bg_park", agent["id"])) == 1

    async def test_moving_past_a_parked_question_hands_it_to_the_next_turn(self, client, agent, tools, llm):
        async def helper_works(_kw):
            await eventually(lambda: store.get_pending_approval("s_bg_moved", agent["id"]), what="the park")
            return "HP-2044 is late."

        llm.on("PLAN IT", [BACKGROUND, tool_call("danger")])
        llm.on("CHECK A", [helper_works])
        llm.on("SOMETHING ELSE", ["Sure — and my helper says HP-2044 is late."])
        await _chat(client, agent, "PLAN IT", "s_bg_moved")
        await eventually(lambda: _parked_inbox("s_bg_moved", agent["id"]), what="the result kept")
        await _chat(client, agent, "SOMETHING ELSE", "s_bg_moved")
        prompt = llm.calls_for("SOMETHING ELSE")[0]["prompt_msg"]
        assert helpers.INBOX_HEADING in prompt and "HP-2044 is late." in prompt
        await asyncio.sleep(0.2)
        turns = await _turns("s_bg_moved", agent["id"])
        assert len(turns) == 2 and turns[0]["approval"]["outcome"] == "expired"
        assert not any(t.get("automated") for t in turns)

    async def test_a_follow_up_waits_for_the_conversation(self, agent, tools, llm, monkeypatch):
        """The approve route claims and then reads: a claim with no task yet must be waited on, not spun on."""
        looks = []
        real = helpers.already_delivered

        async def counted(*a, **kw):
            looks.append(1)
            return await real(*a, **kw)

        monkeypatch.setattr(helpers, "already_delivered", counted)
        llm.on(AUTOMATED, ["Here is what my helper found."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_wait", agent_key=agent["id"], agent_id=agent["id"])
        item = {"kind": "helper_result", "helper_id": "subtask_wait", "name": "Waiter", "status": "completed",
                "text": "It found the answer."}
        with chat_turns.claim("s_bg_wait", agent_key=agent["id"]):
            delivery = asyncio.ensure_future(helpers.deliver(conversation, item))
            await asyncio.sleep(0.3)
            assert not delivery.done() and not llm.calls_for(AUTOMATED)
            assert len(looks) <= 2, f"it looked {len(looks)} times while the claim was held"
        assert await asyncio.wait_for(delivery, 5) == "follow_up"
        await eventually(lambda: _turns("s_bg_wait", agent["id"]), what="the follow-up")

    async def test_delivered_twice_it_arrives_once(self, agent, tools, llm):
        llm.on(AUTOMATED, ["Here is what my helper found."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_twice", agent_key=agent["id"], agent_id=agent["id"])
        item = {"kind": "helper_result", "helper_id": "subtask_twice", "name": "Twice", "status": "completed",
                "text": "It found the answer."}
        first, second = await asyncio.gather(helpers.deliver(conversation, item),
                                             helpers.deliver(conversation, item))
        assert sorted([first, second]) == ["already", "follow_up"]
        await eventually(lambda: _turns("s_bg_twice", agent["id"]), what="the follow-up")
        helpers._DELIVERED.clear()  # as after a restart: the conversation itself says it arrived
        assert await helpers.deliver(conversation, item) == "already"

    async def test_an_inbox_closed_before_reading_hands_back_what_it_held(self):
        conversation = helpers.Conversation(session_id="s_inbox", agent_key="a", agent_id="a")
        inbox = helpers.Inbox(conversation).open()
        assert inbox.put({"kind": "helper_result", "helper_id": "one"})
        assert [i["helper_id"] for i in inbox.drain()] == ["one"]
        assert inbox.put({"kind": "helper_result", "helper_id": "two"})
        left = inbox.close()
        assert [i["helper_id"] for i in left] == ["one", "two"], "read but never answered, and unread"
        assert not inbox.put({"kind": "helper_result", "helper_id": "three"})
        assert conversation.key not in helpers._OPEN


class TestTheFollowUpIsNeverThePersons:
    async def test_it_is_drawn_and_read_as_the_helpers(self, client, agent, tools, llm):
        from core.session import get_recent_history_messages

        conversation = helpers.Conversation(session_id="s_bg_label", agent_key=agent["id"], agent_id=agent["id"])
        llm.on(AUTOMATED, ["Three are late."], raw=True)
        await helpers.deliver(conversation, {"kind": "helper_result", "helper_id": "subtask_label",
                                             "name": "30-day check", "status": "completed",
                                             "text": "3 of 12 are late."})
        await eventually(lambda: _turns("s_bg_label", agent["id"]), what="the follow-up")
        history = (await client.get(f"/api/sessions/s_bg_label/history?agent_id={agent['id']}")).json()
        assert history["turns"][0]["automated"]["name"] == "30-day check"
        said = await get_recent_history_messages("s_bg_label", agent_id=agent["id"])
        assert said[0]["role"] == "user" and said[0]["content"].startswith("[Automated: the result of your helper")

    async def test_it_sends_no_helper_of_its_own_to_the_background(self, agent, tools, llm):
        conversation = helpers.Conversation(session_id="s_bg_nochain", agent_key=agent["id"], agent_id=agent["id"])
        llm.on(AUTOMATED, ["Noted."], raw=True)
        await helpers.deliver(conversation, {"kind": "helper_result", "helper_id": "subtask_nochain",
                                             "name": "Chain", "status": "completed", "text": "done"})
        await eventually(lambda: llm.calls_for(AUTOMATED), what="the follow-up's model call")
        assert not _offered_background(llm.calls_for(AUTOMATED)[0])


def _result(helper_id, name, text, status="completed"):
    return {"kind": "helper_result", "helper_id": helper_id, "name": name, "status": status, "text": text,
            "session_id": f"helperc_{helper_id}"}


class TestResultsThatFinishTogetherComeBackTogether:
    """Naveen, SUB1a's live check: three helpers that finished seconds apart posted three follow-ups. Results
    that finish close together come back as one follow-up holding them all, each still exactly once."""

    async def test_two_results_close_together_are_one_follow_up(self, agent, tools, llm, monkeypatch):
        monkeypatch.setattr(helpers, "SETTLE_SECONDS", 0.5, raising=False)
        llm.on(AUTOMATED, ["SKU-11 has 41 in stock; SKU-12 is out, 120 on order."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_together", agent_key=agent["id"], agent_id=agent["id"])
        helpers.redeliver(conversation, [_result("subtask_a", "Stock: 11", "SKU-11: 41 in stock.")])
        await asyncio.sleep(0.2)
        helpers.redeliver(conversation, [_result("subtask_b", "Stock: 12", "SKU-12: out, 120 on order.",
                                                 status="timed_out")])
        turns = await eventually(lambda: _turns("s_bg_together", agent["id"]), what="the follow-up")
        await asyncio.sleep(1.0)
        turns = await _turns("s_bg_together", agent["id"])
        assert len(turns) == 1, "one follow-up for both"
        mark = turns[0]["automated"]
        assert mark["helper_ids"] == ["subtask_a", "subtask_b"] and mark["names"] == ["Stock: 11", "Stock: 12"]
        assert mark["statuses"] == ["completed", "timed_out"]
        assert mark["helper_id"] == "subtask_a" and mark["name"] == "Stock: 11", "a reader of one still reads it"
        assert "SKU-11: 41 in stock." in turns[0]["user"] and "SKU-12: out, 120 on order." in turns[0]["user"]
        assert len(llm.calls_for(AUTOMATED)) == 1
        prompt = llm.calls_for(AUTOMATED)[0]["prompt_msg"]
        assert prompt.startswith(f"{AUTOMATED}: your helpers 'Stock: 11' and 'Stock: 12' came back")
        assert "ran out of time" in prompt, "each result says how its helper ended"

    async def test_one_arriving_while_the_conversation_is_busy_joins_the_one_waiting(self, agent, tools, llm,
                                                                                      monkeypatch):
        monkeypatch.setattr(helpers, "SETTLE_SECONDS", 0.1, raising=False)
        llm.on(AUTOMATED, ["Both are done."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_busy", agent_key=agent["id"], agent_id=agent["id"])
        with chat_turns.claim("s_bg_busy", agent_key=agent["id"]):
            helpers.redeliver(conversation, [_result("subtask_c", "First", "the first result")])
            await asyncio.sleep(0.5)  # settled, and waiting for the conversation
            helpers.redeliver(conversation, [_result("subtask_d", "Second", "the second result")])
            await asyncio.sleep(0.3)
            assert not llm.calls_for(AUTOMATED)
        await eventually(lambda: _turns("s_bg_busy", agent["id"]), what="the follow-up")
        await asyncio.sleep(0.5)
        turns = await _turns("s_bg_busy", agent["id"])
        assert len(turns) == 1 and turns[0]["automated"]["helper_ids"] == ["subtask_c", "subtask_d"]

    async def test_each_result_of_a_combined_follow_up_arrives_once(self, agent, tools, llm):
        llm.on(AUTOMATED, ["Both are done."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_once", agent_key=agent["id"], agent_id=agent["id"])
        first, second = _result("subtask_e", "E", "e's result"), _result("subtask_f", "F", "f's result")
        assert await helpers.deliver(conversation, [first, second, first]) == "follow_up"
        await eventually(lambda: _turns("s_bg_once", agent["id"]), what="the follow-up")
        assert (await _turns("s_bg_once", agent["id"]))[0]["automated"]["helper_ids"] == ["subtask_e", "subtask_f"]
        helpers._DELIVERED.clear()  # as after a restart: the conversation itself says they arrived
        assert await helpers.deliver(conversation, second) == "already"
        assert await helpers.deliver(conversation, [first, second]) == "already"

    async def test_one_result_alone_keeps_its_mark_as_it_was(self, agent, tools, llm):
        llm.on(AUTOMATED, ["Done."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_alone", agent_key=agent["id"], agent_id=agent["id"])
        await helpers.deliver(conversation, _result("subtask_g", "G", "g's result"))
        turns = await eventually(lambda: _turns("s_bg_alone", agent["id"]), what="the follow-up")
        assert turns[0]["automated"] == {"kind": "helper_result", "helper_id": "subtask_g", "name": "G",
                                         "status": "completed", "session_id": "helperc_subtask_g"}
        assert turns[0]["user"] == "g's result"
        assert llm.calls_for(AUTOMATED)[0]["prompt_msg"].startswith(f"{AUTOMATED}: your helper 'G' finished.")

    async def test_a_parked_question_keeps_them_all(self, agent, tools, llm):
        await store.set_pending_approval("s_bg_park_both", agent["id"], {
            "tool_name": "danger", "tool_args": {}, "approval_id": "ap1", "message": "Do it", "progress": {}})
        conversation = helpers.Conversation(session_id="s_bg_park_both", agent_key=agent["id"], agent_id=agent["id"])
        both = [_result("subtask_h", "H", "h's result"), _result("subtask_i", "I", "i's result")]
        assert await helpers.deliver(conversation, both) == "parked"
        kept = await _parked_inbox("s_bg_park_both", agent["id"])
        assert [i["helper_id"] for i in kept] == ["subtask_h", "subtask_i"]
        assert not await _turns("s_bg_park_both", agent["id"]) and not llm.calls_for(AUTOMATED)

    async def test_it_is_read_back_as_every_helpers(self, client, agent, tools, llm):
        from core.session import get_recent_history_messages, render_session_history

        llm.on(AUTOMATED, ["Both are done."], raw=True)
        conversation = helpers.Conversation(session_id="s_bg_read", agent_key=agent["id"], agent_id=agent["id"])
        await helpers.deliver(conversation, [_result("subtask_j", "J", "j's result"),
                                             _result("subtask_k", "K", "k's result")])
        await eventually(lambda: _turns("s_bg_read", agent["id"]), what="the follow-up")
        said = await get_recent_history_messages("s_bg_read", agent_id=agent["id"])
        assert said[0]["content"].startswith("[Automated: the results of your helpers 'J' and 'K', not something")
        listed = await render_session_history("s_bg_read", agent["id"], turn=1)
        assert "Automated (the results of your helpers 'J' and 'K', not the person's words)" in listed
        history = (await client.get(f"/api/sessions/s_bg_read/history?agent_id={agent['id']}")).json()
        assert history["turns"][0]["automated"]["names"] == ["J", "K"]


class TestABackgroundHelpersLimits:
    async def test_a_person_stops_it_and_no_follow_up_comes(self, client, agent, tools, llm):
        async def helper_works(_kw):
            await asyncio.Event().wait()

        llm.on("PLAN IT", [BACKGROUND, "Sent."]).on("CHECK A", [helper_works])
        frames = await _chat(client, agent, "PLAN IT", "s_bg_stop")
        started = [f for f in frames if f["type"] == "helper_started"][0]
        await eventually(lambda: llm.calls_for("CHECK A"), what="the helper's model call")
        reply = await client.post(f"/chat/s_bg_stop/helpers/{started['helper_id']}/stop")
        assert reply.json()["stopped"] is True
        row = await eventually(lambda: _row_ended(started["session_id"], started["helper_id"]), what="its end")
        assert row["status"] == "cancelled"
        await asyncio.sleep(0.3)
        assert len(await _turns("s_bg_stop", agent["id"])) == 1, "stopped by the person: nothing posted"

    async def test_a_helper_its_agent_waits_for_is_stopped_from_its_lane(self, client, agent, tools, llm):
        go = asyncio.Event()

        async def helper_works(_kw):
            await go.wait()
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", [spawn("CHECK A", ["lookup"], name="Waited for"), "Carried on without it."])
        llm.on("CHECK A", [helper_works, "never reached"])
        chat = asyncio.ensure_future(_chat(client, agent, "PLAN IT", "s_wait_stop"))
        helper_id = await eventually(lambda: next(iter(helpers.live(("conversation", "default", "s_wait_stop"))),
                                                  None), what="the helper")
        reply = await client.post(f"/chat/s_wait_stop/helpers/{helper_id}/stop")
        assert reply.json()["stopped"] is True
        go.set()
        frames = await asyncio.wait_for(chat, 10)
        assert [f for f in frames if f["type"] == "helper_finished"][0]["status"] == "cancelled"
        assert [f for f in frames if f["type"] == "response"][0]["content"] == "Carried on without it."
        assert (await client.post(f"/chat/s_wait_stop/helpers/{helper_id}/stop")).json()["stopped"] is False

    async def test_background_helpers_count_against_the_three(self, client, agent, tools, llm):
        async def helper_works(_kw):
            await asyncio.Event().wait()

        three = "\n".join(spawn(f"CHECK {x}", ["lookup"], background=True) for x in "ABC")
        llm.on("PLAN IT", [three, spawn("CHECK D", ["lookup"]), "done"])
        for x in "ABCD":
            llm.on(f"CHECK {x}", [helper_works])
        frames = await _chat(client, agent, "PLAN IT", "s_bg_cap")
        results = [f["preview"] for f in frames if f["type"] == "tool_result" and f["tool_name"] == "spawn_subtask"]
        assert len(results) == 4 and "No helper was started" in results[3]
        assert not llm.calls_for("CHECK D")

    async def test_it_stops_at_its_time_limit_and_says_so(self, client, agent, tools, llm, monkeypatch):
        monkeypatch.setattr(helpers, "BACKGROUND_SECONDS", 0)
        llm.on("PLAN IT", [BACKGROUND, "Sent."]).on("CHECK A", ["never"])
        llm.on(AUTOMATED, ["My helper ran out of time."], raw=True)
        await _chat(client, agent, "PLAN IT", "s_bg_time")
        turns = await eventually(lambda: _turns_at_least("s_bg_time", agent["id"], 2), what="the follow-up")
        assert turns[1]["automated"]["status"] == "timed_out"
        assert "ran out of time" in turns[1]["user"]


class TestARestart:
    async def _left_by_a_restart(self, agent, status, record=None):
        session = f"helperc_{status}"
        await store.append_turn(session, "subtask_restart", [{
            "role": "user", "content": "check the orders",
            "automated": {"kind": "helper_brief", "helper_id": "subtask_restart", "name": "30-day check",
                          **helpers.Conversation(session_id="s_restart", agent_key=agent["id"],
                                                 agent_id=agent["id"]).link()}}])
        if record:
            await store.append_turn(session, "subtask_restart", [{"role": "assistant", "content": "",
                                                                   "helper": record}])
        await store.set_status(session, "subtask_restart", status)
        return session

    async def test_a_helper_it_cut_short_is_failed_and_its_conversation_told(self, agent):
        session = await self._left_by_a_restart(agent, "running")
        assert await helpers.sweep_interrupted() == 1
        [turn] = await _turns("s_restart", agent["id"])
        assert turn["automated"]["status"] == "failed"
        assert "stopped when the server restarted" in turn["assistant"]
        assert (await store.get(session, "subtask_restart"))["status"] == "settled"
        assert await helpers.sweep_interrupted() == 0, "told once"

    async def test_a_result_that_never_arrived_is_given(self, agent):
        await self._left_by_a_restart(agent, "completed", record={
            "id": "subtask_restart", "name": "30-day check", "status": "completed", "answer": "3 are late."})
        assert await helpers.sweep_interrupted() == 1
        [turn] = await _turns("s_restart", agent["id"])
        assert turn["user"] == "3 are late." and turn["automated"]["status"] == "completed"


async def _two(agent):
    turns = await _turns("s_bg_follow", agent["id"])
    return turns if len(turns) >= 2 else None


async def _turns_at_least(session_id, agent_id, n):
    turns = await _turns(session_id, agent_id)
    return turns if len(turns) >= n else None


async def _parked_inbox(session_id, agent_id):
    pending = await store.get_pending_approval(session_id, agent_id)
    return ((pending or {}).get("progress") or {}).get("inbox", {}).get("pending")


async def _row_ended(session_id, agent_id):
    row = await store.get(session_id, agent_id)
    return row if row and row.get("status") != "running" else None
