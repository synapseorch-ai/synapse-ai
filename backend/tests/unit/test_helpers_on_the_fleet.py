"""Helpers on the fleet: the chat job keeps them, and a background helper is a job of its own (SUB1b, D281).

SUB1a's helpers (`core/helpers.py`) ran where the app's chat runs: in one process,
a background helper as a task the server held and its result delivered in-process.
The fleet's chat job (`core/scale/worker.py`) knew nothing of them — it never
marked a turn as the app's chat, and its saves dropped what a turn's helpers did —
and its turns run in any worker, so a helper's result cannot be handed to a turn in
another process. Now:

* the job takes the host's word that a turn is the app's (or a member's channel's,
  `background_ok`), a follow-up's mark (`automated`), results carried off a question
  the person moved past (`inbox_seed`), and a tag for each of its events
  (`chat_turn`), and keeps `helpers`, `automated` and `inbox_read` at every save;
* with a host installed (`helpers.install`), a background helper is started by the
  host (cloud: a chat job of its own), counted wherever it runs (`elsewhere`), and its
  result handed to the host (`deliver`: cloud enqueues one follow-up job);
* a background helper's job runs it (`helper=`), saves how it ended and reports it
  once; handed out again, it does not run again, and says it stopped;
* a helper the parent waits for can be stopped from another process
  (`synapse:helper-stop:chat:<session>`).
"""
from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from core import helpers
from core.store import sessions as store
from core.tools import VirtualTool


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))
DANGER = VirtualTool("danger", "changes things", {}, _ann(readOnlyHint=False, destructiveHint=True))
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
        wanted = agent.get("tools") or []
        held = [t for t in (LOOKUP, DANGER) if "all" in wanted or t.name in wanted]
        ollama = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                    "parameters": t.inputSchema}} for t in held]
        return held, {t.name: t.inputSchema for t in held}, ollama, ""

    monkeypatch.setattr(re_mod, "aggregate_all_tools", aggregated)
    monkeypatch.setattr(server, "agent_sessions", {"fake": _Session()}, raising=False)
    monkeypatch.setattr(server, "tool_router", {"lookup": ("fake", "lookup"), "danger": ("fake", "danger")},
                        raising=False)


@pytest.fixture
def llm(monkeypatch):
    return install(monkeypatch, RoutedLLM())


@pytest.fixture
async def agent(seed_agent):
    return await seed_agent(name="Stockist help", tools=["spawn_subtask", "lookup", "danger"],
                            skip_default_tools=True, tool_policy={"destructive": "confirm"})


class FakeHost:
    """What cloud installs, recorded: where a background helper starts, how many work elsewhere, what goes back."""

    def __init__(self):
        self.started: list[tuple] = []
        self.delivered: list[tuple] = []
        self.counts: dict[tuple, int] = {}
        self.refuse: str | None = None

    async def start_background(self, helper, *, conversation, live_here, untrusted):
        if self.refuse:
            raise helpers.Refused(self.refuse)
        self.started.append((helper, conversation, live_here, untrusted))

    async def elsewhere(self, key):
        return self.counts.get(key, 0)

    async def deliver(self, conversation, items):
        self.delivered.append((conversation, [dict(i) for i in items]))


@pytest.fixture
def host():
    fake = FakeHost()
    helpers.install(fake)
    try:
        yield fake
    finally:
        helpers.uninstall()


@pytest.fixture
def fleet(fake_redis, monkeypatch):
    """Run the fleet's chat job in this process, on `core.server` as its tenant's session set."""
    import core.server as server
    from core.scale import mcp_pool, worker
    from core.store import get_store

    async def acquire():
        return server

    async def nothing():
        return None

    monkeypatch.setattr(mcp_pool, "acquire", acquire)
    monkeypatch.setattr(mcp_pool, "release", nothing)
    monkeypatch.setattr(worker, "_load_job_settings", nothing)
    monkeypatch.setattr(worker, "CHAT_CANCEL_POLL_SECONDS", 0.05)

    async def run(session_id, agent_id, message, *, job_try=1, **kw):
        ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": job_try,
               "job_id": f"job-{session_id}"}
        return await worker.run_agent_chat_job(ctx, session_id, agent_id, message, **kw)

    return run


def spawn(task, tools, **args):
    return json.dumps({"tool": "spawn_subtask", "arguments": {"task": task, "tools": tools, **args}})


WAIT = spawn("CHECK A", ["lookup"], name="30-day check")
BACKGROUND = spawn("CHECK A", ["lookup"], name="30-day check", background=True)
MARK = {"kind": "helper_result", "helper_id": "subtask_aaaa1111", "name": "30-day check",
        "status": "completed", "session_id": "helperc_0123456789abcdef"}
ITEM = {**MARK, "text": "3 of the 12 orders are past the 30-day rule.", "at": 0.0}


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


async def _stream(redis, session_id):
    entries = await redis.xrange(f"synapse:chat:{session_id}:events")
    return [json.loads(fields[b"data"]) for _id, fields in entries]


def _offered_background(call) -> bool:
    for tool in call.get("tools") or []:
        if tool["function"]["name"] == "spawn_subtask":
            return "background" in tool["function"]["parameters"]["properties"]
    raise AssertionError("spawn_subtask was not offered")


def _prompts(llm, starts):
    return [c.get("prompt_msg") or "" for c in llm.calls_for(starts)]


class TestTheFleetJobKeepsWhatHelpersDid:
    async def test_a_waiting_helpers_record_is_saved_with_the_answer(self, fleet, agent, tools, llm):
        """S7 on the fleet: its lane survives a reload. The job's save dropped `helpers`."""
        llm.on("PLAN IT", [WAIT, "Three orders are late."]).on("CHECK A", ["3 are late."])
        result = await fleet("s_fleet_wait", agent["id"], "PLAN IT")

        assert result["status"] == "completed"
        answer = (await _messages("s_fleet_wait", agent["id"]))[-1]
        assert [(h["name"], h["mode"], h["status"]) for h in answer["helpers"]] == [
            ("30-day check", "wait", "completed")]
        assert answer["helpers"][0]["answer"] == "3 are late."

    async def test_background_is_offered_only_where_the_host_says(self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", ["no helper today"])
        await fleet("s_fleet_offer_no", agent["id"], "PLAN IT")
        await fleet("s_fleet_offer_yes", agent["id"], "PLAN IT", background_ok=True)

        offered = [_offered_background(c) for c in llm.calls_for("PLAN IT")]
        assert offered == [False, True]

    async def test_a_visitors_turn_is_never_offered_it(self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", ["no helper today"])
        await fleet("s_fleet_offer_visitor", agent["id"], "PLAN IT", background_ok=True,
                    visitor={"surface": "widget", "tools": ["spawn_subtask", "lookup"]})
        assert not _offered_background(llm.calls_for("PLAN IT")[0])

    async def test_a_background_spawn_goes_to_the_host_and_the_answer_keeps_its_lane(
            self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", [BACKGROUND, "I've sent a helper."])
        await fleet("s_fleet_bg", agent["id"], "PLAN IT", background_ok=True)

        [(helper, conversation, live_here, untrusted)] = host.started
        assert helper.mode == "background" and helper.session_id.startswith(helpers.SESSION_PREFIX)
        assert (conversation.session_id, conversation.agent_id) == ("s_fleet_bg", agent["id"])
        assert live_here == 0
        assert not helpers.live(("conversation", conversation.tenant, "s_fleet_bg")), \
            "counted where it runs now, not in this process too"
        answer = (await _messages("s_fleet_bg", agent["id"]))[-1]
        assert answer["content"] == "I've sent a helper."
        [kept] = answer["helpers"]
        assert (kept["mode"], kept["status"], kept["session_id"]) == ("background", "running", helper.session_id)

    async def test_a_refusal_is_said_to_the_agent_word_for_word(self, fleet, agent, tools, llm, host):
        host.refuse = "No helper was started: this workspace's plan allows 1 background helper at once."
        llm.on("PLAN IT", [BACKGROUND, "I'll do it myself."])
        await fleet("s_fleet_refused", agent["id"], "PLAN IT", background_ok=True)

        assert host.refuse in _prompts(llm, "PLAN IT")[1]
        assert not host.started

    async def test_helpers_working_elsewhere_count_toward_the_three(self, fleet, agent, tools, llm, host):
        from core.tenancy import get_tenant
        host.counts[("conversation", get_tenant() or "", "s_fleet_count")] = helpers.MAX_LIVE
        llm.on("PLAN IT", [WAIT, "Fine."]).on("CHECK A", ["never"])
        await fleet("s_fleet_count", agent["id"], "PLAN IT", background_ok=True)

        assert not llm.calls_for("CHECK A")
        assert "No helper was started" in _prompts(llm, "PLAN IT")[1]


class TestAFollowUp:
    async def test_its_question_is_marked_and_read_as_automated(self, fleet, agent, tools, llm):
        llm.on(AUTOMATED, ["Three orders are late."], raw=True)
        result = await fleet("s_fleet_follow", agent["id"], ITEM["text"], automated=MARK)

        assert result["status"] == "completed"
        question, answer = (await _messages("s_fleet_follow", agent["id"]))[-2:]
        assert question["automated"] == MARK and question["content"] == ITEM["text"]
        assert answer["content"] == "Three orders are late."
        assert _prompts(llm, AUTOMATED)[0].startswith(AUTOMATED)

    async def test_a_failed_follow_up_keeps_the_mark(self, fleet, agent, tools, llm):
        def down(_kw):
            raise RuntimeError("the model is down")

        llm.on(AUTOMATED, [down], raw=True)
        with pytest.raises(Exception):
            await fleet("s_fleet_follow_failed", agent["id"], ITEM["text"], automated=MARK)

        question = (await _messages("s_fleet_follow_failed", agent["id"]))[-2]
        assert question["automated"] == MARK

    async def test_it_never_moves_past_a_parked_question_and_offers_no_background(
            self, fleet, agent, tools, llm, host):
        await store.set_pending_approval("s_fleet_follow_parked", agent["id"], {
            "tool_name": "danger", "tool_args": {}, "approval_id": "ap1", "message": "Do it", "progress": {}})
        llm.on(AUTOMATED, ["Noted."], raw=True)
        await fleet("s_fleet_follow_parked", agent["id"], ITEM["text"], automated=MARK, background_ok=True)

        assert (await store.get_pending_approval("s_fleet_follow_parked", agent["id"]))["approval_id"] == "ap1"
        assert not _offered_background(llm.calls_for(AUTOMATED)[0])

    async def test_a_parked_follow_up_keeps_its_mark(self, fleet, agent, tools, llm):
        llm.on(AUTOMATED, [tool_call("danger")], raw=True)
        result = await fleet("s_fleet_follow_park", agent["id"], ITEM["text"], automated=MARK)

        assert result["status"] == "paused"
        assert (await store.get_pending_approval("s_fleet_follow_park", agent["id"]))["automated"] == MARK


class TestResultsCarriedIntoATurn:
    async def test_a_seeded_result_is_read_once_and_kept_as_read(self, fleet, agent, tools, llm, host,
                                                                  fake_redis):
        llm.on("PLAN IT", ["Three orders are late, and the plan stands."])
        await fleet("s_fleet_seed", agent["id"], "PLAN IT", background_ok=True, inbox_seed=[ITEM])

        assert ITEM["text"] in _prompts(llm, "PLAN IT")[0]
        answer = (await _messages("s_fleet_seed", agent["id"]))[-1]
        assert answer["inbox_read"] == [ITEM["helper_id"]]
        await asyncio.sleep(0.05)
        assert not host.delivered, "what the answer read is not sent back again"

    async def test_a_parked_turn_keeps_what_it_read_and_never_publishes_it(self, fleet, agent, tools, llm,
                                                                           host, fake_redis):
        llm.on("PLAN IT", [tool_call("danger")])
        result = await fleet("s_fleet_seed_park", agent["id"], "PLAN IT", background_ok=True, inbox_seed=[ITEM])

        assert result["status"] == "paused"
        kept = (await store.get_pending_approval("s_fleet_seed_park", agent["id"]))["progress"]["inbox"]
        assert [i["helper_id"] for i in kept["in_flight"]] == [ITEM["helper_id"]]
        assert not [e for e in await _stream(fake_redis, "s_fleet_seed_park") if e["type"] == "_inbox_parked"]
        assert not host.delivered

    async def test_the_approved_resume_answers_what_it_read_before_it_parked(self, fleet, agent, tools, llm, host):
        from core.chat_approval import resume_state_for
        llm.on("PLAN IT", [tool_call("danger"), "Done, and three orders are late."])
        await fleet("s_fleet_seed_resume", agent["id"], "PLAN IT", background_ok=True, inbox_seed=[ITEM])
        pending = await store.take_pending_approval("s_fleet_seed_resume", agent["id"])

        result = await fleet("s_fleet_seed_resume", agent["id"], pending["message"], background_ok=True,
                             resume_state=resume_state_for(pending, approve=True))

        assert result["status"] == "completed"
        answer = (await _messages("s_fleet_seed_resume", agent["id"]))[-1]
        assert answer["content"] == "Done, and three orders are late."
        assert answer["inbox_read"] == [ITEM["helper_id"]]
        await asyncio.sleep(0.05)
        assert not host.delivered

    async def test_moving_past_a_parked_question_hands_its_results_to_the_new_turn(
            self, fleet, agent, tools, llm, host):
        await store.set_pending_approval("s_fleet_moved", agent["id"], {
            "tool_name": "danger", "tool_args": {}, "approval_id": "ap1", "message": "Do it",
            "progress": {"inbox": {"pending": [ITEM], "in_flight": []}}})
        llm.on("NEW ASK", ["On the late orders: three."])
        await fleet("s_fleet_moved", agent["id"], "NEW ASK", background_ok=True)

        assert ITEM["text"] in _prompts(llm, "NEW ASK")[0]
        assert (await _messages("s_fleet_moved", agent["id"]))[-1]["inbox_read"] == [ITEM["helper_id"]]
        await asyncio.sleep(0.05)
        assert not host.delivered


def _background_helper(agent, session_id="s_parent"):
    helper = helpers.build({"task": "CHECK A", "tools": ["lookup"], "name": "30-day check"},
                           parent_agent=agent, parent_model="gpt-4.1-mini", available_tools=["lookup"],
                           policy=None, declarations={"lookup": LOOKUP}, mode="background")
    conversation = helpers.Conversation(session_id=session_id, agent_key=agent["id"], agent_id=agent["id"])
    helpers.prepare_background(helper, conversation)
    return helper


class TestABackgroundHelpersJob:
    async def test_it_runs_saves_how_it_ended_and_reports_once(self, fleet, agent, tools, llm, host, fake_redis):
        helper = _background_helper(agent)
        await helpers.open_row(helper, helper.conversation)
        llm.on("CHECK A", [tool_call("lookup", item="A"), "3 of the 12 are late."])

        result = await fleet(helper.session_id, helper.id, helper.task,
                             helper=helper.spec(untrusted=True), chat_turn="t1")

        assert result["status"] == "completed"
        events = await _stream(fake_redis, helper.session_id)
        assert [e["type"] for e in events][0] == "helper_started" and events[-1]["type"] == "done"
        assert "helper_finished" in [e["type"] for e in events]
        assert {e.get("chat_turn") for e in events} == {"t1"}
        row = await store.get(helper.session_id, helper.id)
        assert row["status"] == "completed" and row["messages"][-1]["helper"]["answer"] == "3 of the 12 are late."
        [(conversation, [item])] = host.delivered
        assert conversation.session_id == "s_parent" and item["helper_id"] == helper.id
        assert item["status"] == "completed" and "3 of the 12 are late." in item["text"]

    async def test_it_starts_as_marked_as_its_parent(self, fleet, agent, tools, llm, host):
        helper = _background_helper(agent)
        await helpers.open_row(helper, helper.conversation)
        llm.on("CHECK A", ["fine"])
        spec = helper.spec(untrusted=True)
        assert helpers.Helper.from_spec(spec).untrusted is True

    async def test_a_stopped_helper_reports_nothing(self, fleet, agent, tools, llm, host, fake_redis):
        from core.scale.pubsub import publish_chat_cancellation
        helper = _background_helper(agent)
        await helpers.open_row(helper, helper.conversation)
        never = asyncio.Event()

        async def stopped_mid_call(_kw):
            await publish_chat_cancellation(fake_redis, helper.session_id)
            await never.wait()

        llm.on("CHECK A", [stopped_mid_call])
        result = await fleet(helper.session_id, helper.id, helper.task, helper=helper.spec(untrusted=False))

        assert result["status"] == "cancelled"
        assert (await store.get(helper.session_id, helper.id))["status"] == "cancelled"
        assert not host.delivered

    async def test_handed_out_again_it_does_not_run_and_says_it_stopped(self, fleet, agent, tools, llm, host):
        helper = _background_helper(agent)
        await helpers.open_row(helper, helper.conversation)
        llm.on("CHECK A", ["should not run"])

        result = await fleet(helper.session_id, helper.id, helper.task, helper=helper.spec(untrusted=False),
                             job_try=2)

        assert result["status"] == "interrupted"
        assert not llm.calls_for("CHECK A")
        assert (await store.get(helper.session_id, helper.id))["status"] == "failed"
        [(_conversation, [item])] = host.delivered
        assert item["status"] == "failed"
        assert item["said"] == helpers.RESTARTED.format(name="30-day check")


class TestStoppedFromAnotherProcess:
    async def test_a_waiting_helper_stops_at_its_next_turn(self, fleet, agent, tools, llm, fake_redis):
        from core.scale.pubsub import publish_helper_stop

        async def ask_to_stop(_kw):
            [held] = [h for live in helpers._LIVE.values() for h in live.values()]
            await publish_helper_stop(fake_redis, "s_fleet_stop", held.id)
            await eventually(lambda: held.stop_requested, what="the stop")
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", [WAIT, "It was stopped."]).on("CHECK A", [ask_to_stop, "never said"])
        await fleet("s_fleet_stop", agent["id"], "PLAN IT")

        [kept] = (await _messages("s_fleet_stop", agent["id"]))[-1]["helpers"]
        assert kept["status"] == "cancelled"


class TestEachEventSaysWhoseTurnItIs:
    async def test_a_tagged_turns_every_event_carries_its_tag(self, fleet, agent, tools, llm, fake_redis):
        llm.on("HELLO", ["Hi."])
        await fleet("s_fleet_tag", agent["id"], "HELLO", chat_turn="p:abc")
        events = await _stream(fake_redis, "s_fleet_tag")
        assert events and {e.get("chat_turn") for e in events} == {"p:abc"}
        assert events[-1]["type"] == "done"

    async def test_an_untagged_turn_is_as_before(self, fleet, agent, tools, llm, fake_redis):
        llm.on("HELLO", ["Hi."])
        await fleet("s_fleet_untagged", agent["id"], "HELLO")
        assert not [e for e in await _stream(fake_redis, "s_fleet_untagged") if "chat_turn" in e]


class TestATurnSavedFromItsEvents:
    def test_a_background_helper_it_started_is_kept_running_with_its_conversation(self):
        """F566: a stopped turn's save marked a background helper it had started `cancelled`, and lost its
        conversation, so the reopened lane said it stopped while it worked on."""
        from core.session import helpers_from_events
        [kept] = helpers_from_events([{"type": "helper_started", "helper_id": "subtask_b", "helper_name": "B",
                                       "task": "CHECK B", "tools": [], "model": "m", "mode": "background",
                                       "session_id": "helperc_b"}])
        assert (kept["status"], kept["session_id"], kept["mode"]) == ("running", "helperc_b", "background")

    def test_a_waiting_helper_the_stop_cut_short_is_still_stopped(self):
        from core.session import helpers_from_events
        [kept] = helpers_from_events([{"type": "helper_started", "helper_id": "subtask_w", "helper_name": "W",
                                       "task": "CHECK W", "tools": [], "model": "m", "mode": "wait"}])
        assert kept["status"] == "cancelled"
