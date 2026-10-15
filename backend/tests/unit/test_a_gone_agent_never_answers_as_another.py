"""A binding that names a deleted agent fails in its own words — never as another agent (F518).

`react_engine._resolve_agent_by_id` kept the single-tenant fallback to the
active agent for any miss, not only for no id: so on a self-hosted install a
channel, a phone number, an agent schedule's Run Now, a workflow step or a chat
naming a deleted agent ran whichever agent was active or sorted first — its
prompt, its tools, its model — and nothing said so. F467 refuses the delete
while something uses the agent; this is the other half, for what was bound
before that refusal existed (and for a choice held in memory, such as a chat's
`/agent`).

Naveen's call (D276): a named agent that is gone raises `AgentGone` with one
sentence in both products, and each door says it in its own words. No id — a
chat with no agent selected, or a `"default"` that names none — still means the
active agent.
"""
import types

import pytest

from _fakes import seed as S
from core import react_engine
from test_human_in_containers import _run, _server, driver, worker  # noqa: F401 — fixtures

OTHER = "agent_f518_other"
GONE = "agent_f518_gone"


@pytest.fixture
async def another_agent():
    """The agent the fallback would have picked: the only one there is."""
    await S.seed_agents([S.make_agent(id=OTHER, name="Someone else", skip_default_tools=True)])


class TestTheResolver:
    async def test_a_named_agent_that_is_gone_raises_with_its_id(self, another_agent, fake_llm):
        with pytest.raises(react_engine.AgentGone) as caught:
            await react_engine._resolve_agent_by_id(GONE)
        assert str(caught.value) == f"Agent '{GONE}' no longer exists. It was deleted. Pick another agent."

    async def test_under_a_provider_it_is_agent_gone_too(self):
        """Cloud's worker reads the type to leave such a turn uncharged."""
        from core.scale.context import set_resource_provider
        from test_agent_resolution import _KnowsNoAgents
        set_resource_provider(_KnowsNoAgents())
        try:
            with pytest.raises(react_engine.AgentGone):
                await react_engine.resolve_agent_for_run(GONE)
        finally:
            set_resource_provider(None)

    async def test_it_is_a_runtime_error_as_before(self):
        """Every door's `except RuntimeError` / `except Exception` still catches it."""
        assert issubclass(react_engine.AgentGone, RuntimeError)

    async def test_resolve_agent_for_run_raises_too(self, another_agent):
        with pytest.raises(react_engine.AgentGone):
            await react_engine.resolve_agent_for_run(GONE)

    async def test_no_id_still_means_the_active_agent(self, another_agent):
        assert (await react_engine._resolve_agent_by_id(None))["id"] == OTHER
        assert (await react_engine._resolve_agent_by_id(""))["id"] == OTHER

    async def test_default_naming_no_agent_still_means_the_active_agent(self, another_agent):
        """A conversation keyed "default" (no agent chosen) and a parked schedule call send it."""
        assert (await react_engine._resolve_agent_by_id("default"))["id"] == OTHER

    async def test_a_live_agent_resolves_as_before(self, another_agent):
        assert (await react_engine._resolve_agent_by_id(OTHER))["id"] == OTHER


class TestAWorkflowStep:
    async def test_a_step_naming_a_gone_agent_fails_and_runs_no_one(self, driver, another_agent, fake_llm):
        orch = S.make_orchestration(id="orch_f518", entry_step_id="ask", steps=[
            {"id": "ask", "name": "Ask", "type": "agent", "agent_id": GONE, "prompt_template": "Hello",
             "output_key": "answer", "next_step_id": "after"},
            {"id": "after", "name": "After", "type": "print", "print_content": "ran", "output_key": "o_after",
             "next_step_id": None}])
        await S.seed_orchestrations([orch])
        fake_llm.set_default("I am someone else.")

        assert await driver.start(orch, "run_f518_step") == "failed"

        run = await _run("run_f518_step")
        assert fake_llm.call_count == 0, "no other agent answered for it"
        assert "answer" not in run.shared_state and "o_after" not in run.shared_state
        (entry,) = [h for h in run.step_history if h.get("step_id") == "ask"]
        assert entry["status"] == "failed"
        assert f"Agent '{GONE}' no longer exists" in entry["error"]

    async def test_resolve_for_run_finds_nothing_and_caches_nothing(self, another_agent):
        from core.orchestration.resolved import resolve_for_run
        engine = types.SimpleNamespace()

        assert await resolve_for_run(engine, GONE, tools=False) is None
        assert GONE not in engine.__dict__.get("_resolved", {})


class TestAChannel:
    @pytest.fixture
    def manager(self):
        import core.server as server
        from core.messaging.manager import MessagingManager
        return MessagingManager(server_module=server)

    async def test_it_replies_the_agent_was_deleted_and_runs_no_one(self, manager, another_agent, fake_llm):
        from core.messaging.manager import AGENT_GONE_REPLY
        fake_llm.set_default("I am someone else.")

        reply = await manager.run_agent(message="hi", agent_id=GONE, session_id="msg_f518",
                                        channel_id="ch_f518", chat_id="c1")

        assert reply == AGENT_GONE_REPLY
        assert AGENT_GONE_REPLY == ("The agent this chat answers with was deleted. "
                                    "Assign another in Synapse, or pick one with /agent.")
        assert fake_llm.call_count == 0

    async def test_a_chats_agent_choice_naming_it_is_forgotten(self, manager, another_agent):
        manager._active_agents["ch_f518"] = {"c1": GONE, "c2": OTHER}

        await manager.run_agent(message="hi", agent_id=GONE, session_id="msg_f518",
                                channel_id="ch_f518", chat_id="c1")

        assert manager.get_chat_agent("ch_f518", "c1") is None, "the next message goes to the channel's own"
        assert manager.get_chat_agent("ch_f518", "c2") == OTHER, "another chat's choice is its own"

    async def test_a_live_agent_answers_as_before(self, manager, another_agent, fake_llm):
        fake_llm.set_default("Hello from the right one.")

        reply = await manager.run_agent(message="hi", agent_id=OTHER, session_id="msg_f518_live")

        assert reply == "Hello from the right one."
        assert fake_llm.last_call.get("agent_id") == OTHER


class TestRunNow:
    @pytest.fixture
    def scheduler(self, monkeypatch):
        from core.scheduler import ScheduleManager
        m = ScheduleManager()
        m._server_module = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        ran: list[str] = []

        async def _run(s, sched_log):
            ran.append(s["id"])
            sched_log.run_end("completed")
            sched_log.close()

        monkeypatch.setattr(m, "_run", _run)
        m.ran = ran
        return m

    async def test_run_now_on_a_gone_agent_turns_it_off_and_says_why(self, scheduler, another_agent):
        """A timed firing does this since F468; Run Now ran the active agent instead."""
        from core.notifications import hub
        from core.scheduler import TargetGone
        from core.store import schedules as schedule_store
        await schedule_store.save({"id": "sched_f518", "name": "Nightly", "target_type": "agent",
                                   "target_id": GONE, "prompt": "go", "enabled": True,
                                   "schedule_type": "interval", "interval_minutes": 60})

        with pytest.raises(TargetGone) as caught:
            await scheduler.trigger_now("sched_f518")

        assert str(caught.value).startswith("The agent it runs was deleted, so this schedule was turned off.")
        assert scheduler.ran == []
        assert (await schedule_store.get("sched_f518"))["enabled"] is False
        assert [n["title"] for n in hub.list() if n["kind"] == "schedule_skipped"
                and n["data"].get("schedule_id") == "sched_f518"] == ["Nightly was turned off"]
