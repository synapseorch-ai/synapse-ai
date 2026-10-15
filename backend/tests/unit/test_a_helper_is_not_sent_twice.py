"""A background helper is not sent twice by a model repeating itself; an explicit `tools: []` is a helper with none (F570, F571).

SUB1b's dev pass, Nova Pro, three runs of three: told "Helper 'Example check' started in
the background…", its next model call sent the identical helper again, and again; the
repeat guard stopped the third, and the person read "I stopped here because I was
repeating myself". Every copy was a job of its own, billed in cloud, and the follow-up
read the same result twice. Naveen: those are lower models' repeats, and a genuine ask
must not be blocked. So (`react_engine._send_to_background`):

* a background send identical to one an EARLIER model call of this turn started (its
  name, task and tools), whose ending this turn has not read, starts nothing: the model
  is told it is already working and nothing new was started;
* one more such send ends the turn with the engine's plain line ("I've sent 'X' to the
  background; its result comes back here.") instead of the repeat guard's;
* identical sends in ONE response all start (someone may ask for copies), a send after
  this turn read the first one's ending starts (a retry), and a helper the turn waits for
  is never held back.

F571: `tools: []` gave the helper the parent's first five tools (F266's fallback for
names it does not hold); an explicit empty list now means a helper with no tools.
"""
from __future__ import annotations

import asyncio
import json
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
        held = [LOOKUP] if "all" in wanted or "lookup" in wanted else []
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
    # Six model calls, not the default three: a repeat ends the turn, not its length.
    return await seed_agent(name="Stockist help", tools=["spawn_subtask", "lookup"], skip_default_tools=True,
                            max_turns=6)


class FakeHost:
    """Cloud's seam, recorded: what was started in the background."""

    def __init__(self):
        self.started: list = []

    async def start_background(self, helper, *, conversation, live_here, untrusted):
        self.started.append(helper)

    async def elsewhere(self, key):
        return 0

    async def deliver(self, conversation, items):
        return None


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
    """The fleet's chat job in this process, as cloud runs every chat turn."""
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

    async def run(session_id, agent_id, message, **kw):
        ctx = {"redis": fake_redis, "session_factory": await get_store(), "job_try": 1, "job_id": f"job-{session_id}"}
        return await worker.run_agent_chat_job(ctx, session_id, agent_id, message, **kw)

    return run


def spawn(task, tools, **args):
    return json.dumps({"tool": "spawn_subtask", "arguments": {"task": task, "tools": tools, **args}})


EXAMPLE = spawn("Read example.com and report its title.", ["lookup"], name="Example check", background=True)
FRANCE = spawn("Find the capital of France.", [], name="France", background=True)
JAPAN = spawn("Find the capital of Japan.", [], name="Japan", background=True)
ALREADY = "is already working on exactly this"


async def _answer(session_id, agent_id) -> dict:
    row = await store.get(session_id, agent_id)
    return ((row or {}).get("messages") or [{}])[-1]


def _results(llm, starts) -> list[str]:
    """What each later model call of the turn was told, by its prompt."""
    return [c.get("prompt_msg") or "" for c in llm.calls_for(starts)]


class TestALowModelsRepeat:
    async def test_a_later_reply_sending_the_same_helper_starts_nothing_and_is_told_why(
            self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", [EXAMPLE, EXAMPLE, "Sent it."])
        await fleet("s_twice", agent["id"], "PLAN IT", background_ok=True)

        assert [h.name for h in host.started] == ["Example check"]
        told = _results(llm, "PLAN IT")[2]
        assert f"Helper 'Example check' {ALREADY}" in told and "nothing new was started" in told
        answer = await _answer("s_twice", agent["id"])
        assert answer["content"] == "Sent it."
        assert [h["name"] for h in answer["helpers"]] == ["Example check"], "one lane, the one that works"

    async def test_one_more_ends_the_turn_with_the_engines_plain_line(self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", [EXAMPLE, EXAMPLE, EXAMPLE, EXAMPLE, "never read"])
        await fleet("s_thrice", agent["id"], "PLAN IT", background_ok=True)

        assert len(host.started) == 1
        assert len(llm.calls_for("PLAN IT")) == 3, "the third send ends it; no fourth model call"
        answer = await _answer("s_thrice", agent["id"])
        assert answer["content"] == "I've sent 'Example check' to the background; its result comes back here."
        assert "repeating myself" not in answer["content"]
        assert not answer.get("no_answer"), "it answered: the helper was sent"

    async def test_a_whole_reply_of_helpers_repeated_starts_none_again(self, fleet, agent, tools, llm, host):
        """Nova Pro's two-helper run: [France, Japan], then the same two again, twice."""
        both = "\n".join([FRANCE, JAPAN])
        llm.on("PLAN IT", [both, both, both, "never read"])
        await fleet("s_round", agent["id"], "PLAN IT", background_ok=True)

        assert [h.name for h in host.started] == ["France", "Japan"]
        answer = await _answer("s_round", agent["id"])
        assert answer["content"] == ("I've sent 'France' and 'Japan' to the background; their results come back "
                                     "here.")


class TestNothingGenuineIsHeldBack:
    async def test_identical_helpers_asked_for_in_one_reply_all_start(self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", ["\n".join([EXAMPLE, EXAMPLE]), "Sent both."])
        await fleet("s_copies", agent["id"], "PLAN IT", background_ok=True)

        assert len(host.started) == 2
        assert ALREADY not in _results(llm, "PLAN IT")[1]

    async def test_a_different_task_name_or_tools_is_another_helper(self, fleet, agent, tools, llm, host):
        other_task = spawn("Read iana.org and report its title.", ["lookup"], name="Example check", background=True)
        other_name = spawn("Read example.com and report its title.", ["lookup"], name="Second look",
                           background=True)
        other_tools = spawn("Read example.com and report its title.", [], name="Example check", background=True)
        llm.on("PLAN IT", [EXAMPLE, other_task, other_name, other_tools, "Sent four."])
        await fleet("s_others", agent["id"], "PLAN IT", background_ok=True)

        assert len(host.started) == 4

    async def test_the_next_turn_may_send_it_again(self, fleet, agent, tools, llm, host):
        llm.on("PLAN IT", [EXAMPLE, "Sent it."]).on("AGAIN", [EXAMPLE, "Sent it again."])
        await fleet("s_next_turn", agent["id"], "PLAN IT", background_ok=True)
        await fleet("s_next_turn", agent["id"], "AGAIN", background_ok=True)

        assert len(host.started) == 2

    async def test_a_helper_the_turn_waits_for_is_never_held_back(self, fleet, agent, tools, llm):
        wait = spawn("CHECK A", ["lookup"], name="Check A")
        llm.on("PLAN IT", [wait, wait, "Checked twice."]).on("CHECK A", ["A is fine", "A is still fine"])
        await fleet("s_wait_twice", agent["id"], "PLAN IT", background_ok=True)

        assert len(llm.calls_for("CHECK A")) == 2

    async def test_a_send_after_the_turn_read_its_ending_is_a_retry_and_starts(self, agent, tools, llm):
        """In one process a result can reach the turn while it works: having read it, the agent may send again."""
        import core.server as server
        from core.react_engine import run_agent_step

        sent = asyncio.Event()

        async def first_ends(_kw):
            return "It failed: the page did not load."

        async def wait_for_its_result(_kw):
            # The helper's result reaches the inbox while this model call runs: read before the next.
            await asyncio.wait_for(sent.wait(), 10)
            await asyncio.sleep(0.2)
            return tool_call("lookup", item="meanwhile")

        def delivered(*_a, **_k):
            sent.set()

        llm.on("PLAN IT", [EXAMPLE, wait_for_its_result, EXAMPLE, "Sent it again."])
        llm.on("Read example.com", [first_ends, "Example Domain"])
        inbox = helpers.Inbox(helpers.Conversation(session_id="s_retry", agent_key=agent["id"],
                                                   agent_id=agent["id"])).open()
        original_put = inbox.put

        def put(item):
            original_put(item)
            delivered()

        inbox.put = put
        try:
            events = [e async for e in run_agent_step(message="PLAN IT", agent_id=agent["id"], session_id="s_retry",
                                                      server_module=server, inbox=inbox, background=True)]
        finally:
            inbox.close()
        started = [e for e in events if e.get("type") == "helper_started"]
        assert len(started) == 2, "the first ended and the turn read it: sending again is a retry"
        assert any(e.get("type") == "helper_results_read" for e in events)


class TestNoToolsMeansNone:
    def _build(self, tools_arg):
        return helpers.build({"task": "Name the capital of France.", "tools": tools_arg, "name": "France"},
                             parent_agent={"tools": ["lookup", "spawn_subtask"]}, parent_model="m",
                             available_tools=["lookup"], policy=None, declarations={"lookup": LOOKUP})

    def test_an_empty_list_is_a_helper_with_no_tools(self):
        helper = self._build([])
        assert helper.tools == [] and helper.agent["tools"] == []

    def test_names_it_does_not_hold_still_fall_back_to_what_it_may_run(self):
        assert self._build(["web_search"]).tools == ["lookup"]

    def test_tools_it_holds_are_given(self):
        assert self._build(["lookup"]).tools == ["lookup"]
