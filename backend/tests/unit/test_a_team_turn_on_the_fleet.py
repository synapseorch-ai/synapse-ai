"""A Team agent's turn on the fleet keeps its members and its task list, as the app's own save does (TEAM1d, F656).

TEAM1a's engine saves a team turn's `members` (each lane: its task, calls and answer) and `team` (the task list, the
tasks used, whether the lead answered alone) through the in-process save. The fleet's chat job saves the turn itself
(`save_here=False`) and builds the answer field by field: F567 added `helpers` and `inbox_read` at every site, and
`members` and `team` were at none, so a cloud team turn reopened with no lanes. Now the normal save keeps both from the
answer's `final`, and a turn saved from outside the loop (a Stop, a failure, a cut) keeps its members as far as they
got, from their events, as OSS's Stop does.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from _fakes.routed_llm import RoutedLLM, install
from core.store import sessions as store
from core.tools import VirtualTool

LEAD, ANA = "lead_f656", "ana_f656"


def _ann(**kw):
    from mcp.types import ToolAnnotations
    return ToolAnnotations(**kw)


LOOKUP = VirtualTool("lookup", "looks a thing up", {}, _ann(readOnlyHint=True, idempotentHint=True))


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
async def team():
    await S.seed_agents([
        S.make_agent(id=ANA, name="Ana", tools=["lookup"], skip_default_tools=True),
        S.make_agent(id=LEAD, name="Lead", type="delegate", tools=[], delegate_agent_ids=[ANA],
                     team={"mode": "team"}, max_turns=8),
    ])


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

    async def run(session_id, message, **kw):
        ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1,
               "job_id": f"job-{session_id}"}
        return await worker.run_agent_chat_job(ctx, session_id, LEAD, message, **kw)

    return run


def assign(member, task):
    return tool_call("assign", member=member, task=task)


async def _answer(session_id):
    row = await store.get(session_id, LEAD)
    return ((row or {}).get("messages") or [])[-1]


async def test_the_saved_turn_keeps_its_members_and_task_list(fleet, tools, llm, team):
    llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
    llm.on("CHECK A", [tool_call("lookup", item="A"), "A is fine"])
    result = await fleet("s_f656_done", "PLAN IT")

    assert result["status"] == "completed"
    answer = await _answer("s_f656_done")
    assert answer["content"] == "lead answer"
    assert [(m["name"], m["status"], m["answer"]) for m in answer["members"]] == [("Ana", "completed", "A is fine")]
    assert [c["tool"] for c in answer["members"][0]["calls"]] == ["lookup"]
    assert answer["team"]["tasks"][0]["status"] == "done" and answer["team"]["answered_alone"] is False
    # The job's result carries the team's work meter for a host to price by (F658); the saved turn keeps none.
    assert set(result["team_work"]) == {"lead_idle_s", "members_s", "helpers_s"} and "work" not in answer["team"]


async def test_the_stream_still_carries_them_for_the_page(fleet, tools, llm, team, fake_redis):
    """The answer frame on the stream keeps `members` and `team`: the page draws the turn from it (TEAM1b)."""
    import json

    llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
    llm.on("CHECK A", ["A is fine"])
    await fleet("s_f656_stream", "PLAN IT")
    entries = await fake_redis.xrange("synapse:chat:s_f656_stream:events")
    finals = [e for e in (json.loads(f[b"data"]) for _i, f in entries) if e.get("type") == "final"]
    assert finals and finals[-1]["members"][0]["name"] == "Ana" and "tasks" in finals[-1]["team"]


async def test_a_lead_that_answers_alone_keeps_that(fleet, tools, llm, team):
    llm.on("PLAN IT", ["hello there"])
    await fleet("s_f656_alone", "PLAN IT")
    answer = await _answer("s_f656_alone")
    assert answer["team"]["answered_alone"] is True and "members" not in answer


async def test_a_stopped_turn_keeps_its_members_as_far_as_they_got(fleet, tools, llm, team, fake_redis):
    """Stopped while the lead waits on its member: the member's record is kept, cut short, as OSS's Stop keeps it."""
    from core.scale.pubsub import _chat_cancel_key

    async def stop_now(_kw):
        await fake_redis.set(_chat_cancel_key("s_f656_stop"), "1")
        await asyncio.sleep(5)
        return "too late"

    llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
    llm.on("CHECK A", [stop_now])
    result = await fleet("s_f656_stop", "PLAN IT")

    assert result["status"] == "cancelled"
    assert "team_work" in result   # a stopped turn is charged for what it worked, from its last snapshot
    answer = await _answer("s_f656_stop")
    assert [m["name"] for m in answer["members"]] == ["Ana"]
    assert answer["members"][0]["status"] in ("cancelled", "failed")


class TestAPersonReachesAMemberFromAnotherProcess:
    """F659: a lane's Stop and Message, and the composer's "To: the lead", from a process that does not run the turn.

    A team's members run inside its lead's job, in whichever worker has it, so OSS's in-process routes cannot reach
    them from cloud's API. The person's asks go on a Redis list for the conversation, in order (`publish_team_ask`),
    and the job's watcher hands each to the team it runs, as it hands over TEAM1c's answers.
    """

    async def test_a_stop_ends_that_member_and_the_team_works_on(self, fleet, tools, llm, team, fake_redis):
        from core import team as team_mod
        from core.scale.pubsub import publish_team_ask

        async def stop_me(_kw):
            working = team_mod.find("s_f659_stop")
            await publish_team_ask(fake_redis, "s_f659_stop", {"kind": "stop", "member_id": next(iter(working.members))})
            await asyncio.sleep(0.4)   # the watcher looks every 0.05 s here
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
        llm.on("CHECK A", [stop_me, "never said"])
        result = await fleet("s_f659_stop", "PLAN IT")

        assert result["status"] == "completed"
        answer = await _answer("s_f659_stop")
        assert answer["content"] == "lead answer"
        assert [(m["name"], m["status"]) for m in answer["members"]] == [("Ana", "cancelled")]

    async def test_a_message_reads_as_the_persons_own_words(self, fleet, tools, llm, team, fake_redis):
        from core import team as team_mod
        from core.scale.pubsub import publish_team_ask

        async def write_to_me(_kw):
            working = team_mod.find("s_f659_msg")
            await publish_team_ask(fake_redis, "s_f659_msg", {"kind": "message", "member_id": next(iter(working.members)),
                                                              "text": "Keep it short"})
            await asyncio.sleep(0.4)
            return tool_call("lookup", item="A")

        llm.on("PLAN IT", [assign("Ana", "CHECK A"), tool_call("wait", seconds=10), "lead answer"])
        llm.on("CHECK A", [write_to_me, "short"])
        await fleet("s_f659_msg", "PLAN IT")
        anas = [str(c.get("prompt_msg") or "") for c in llm.calls_for("CHECK A")]
        assert any("From the person (their own words): Keep it short" in p for p in anas)

    async def test_asks_are_taken_once_in_order(self, fake_redis):
        from core.scale.pubsub import publish_team_ask, take_team_asks

        await publish_team_ask(fake_redis, "s_f659_q", {"kind": "message", "member_id": "m1", "text": "one"})
        await publish_team_ask(fake_redis, "s_f659_q", {"kind": "message", "member_id": "m1", "text": "two"})
        assert [a["text"] for a in await take_team_asks(fake_redis, "s_f659_q")] == ["one", "two"]
        assert await take_team_asks(fake_redis, "s_f659_q") == []

    async def test_asks_for_a_conversation_with_no_team_working_wait_for_none(self, fake_redis):
        """Left for their TTL, never handed to a later turn's lead: an ask is for the team working when it was sent."""
        from core.scale.pubsub import TEAM_ASK_TTL_SECONDS, _team_ask_key, publish_team_ask
        from core.scale.worker import _hand_over_team_asks

        await publish_team_ask(fake_redis, "s_f659_none", {"kind": "stop", "member_id": "m1"})
        await _hand_over_team_asks(fake_redis, "s_f659_none")
        assert 0 < await fake_redis.ttl(_team_ask_key("s_f659_none")) <= TEAM_ASK_TTL_SECONDS
