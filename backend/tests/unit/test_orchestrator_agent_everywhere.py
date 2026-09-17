"""
An Orchestrator agent runs its orchestration wherever it is reached (F71).

Only chat routed an orchestrator agent to its orchestration (`run_react_loop`).
A messaging channel and an agent schedule called `run_agent_step` directly, so
the agent answered as a plain ReAct agent with no tools — it chatted instead of
running the workflow it exists for. Both now go through the same routing, and
say what happened: the orchestration's answer, its error, or where it paused.
"""
import types

import pytest

from _fakes import seed as S
from core.messaging import store as channel_store
from tests.unit.test_human_step_channel import messaging_on, rest, wired  # noqa: F401 — fixtures


def _orch(orch_id, steps, entry):
    return S.make_orchestration(id=orch_id, entry_step_id=entry, steps=steps)


PRINTS = [{"id": "p", "name": "Say", "type": "print", "print_content": "workflow ran",
           "output_key": "out", "next_step_id": None}]
RAISES = [{"id": "t", "name": "Boom", "type": "transform", "transform_code": "raise ValueError('nope')",
           "output_key": "out", "next_step_id": None}]


def _asks(channel_id=None):
    return [{"id": "h", "name": "Approve", "type": "human", "human_prompt": "Ship it?",
             "human_channel_id": channel_id, "output_key": "a", "next_step_id": None}]


async def _orchestrator(orch):
    await S.seed_orchestrations([orch])
    agent = S.make_orchestrator_agent(orch["id"], id=f"agent_{orch['id']}", name="Workflow")
    await S.seed_agents([agent])
    return agent


class TestOnAChannel:
    async def _talk(self, wired, agent, text="go"):
        await channel_store.set_assignments(wired.channel["id"], [agent["id"]])
        await wired.adapter._dispatch("c1", text, "u1")
        return wired.adapter.sent

    async def test_it_answers_with_what_the_orchestration_produced(self, wired, fake_llm):
        agent = await _orchestrator(_orch("orch_says", PRINTS, "p"))

        assert await self._talk(wired, agent) == ["workflow ran"]

    async def test_a_failure_is_reported_as_one(self, wired, fake_llm):
        agent = await _orchestrator(_orch("orch_fails", RAISES, "t"))

        sent = await self._talk(wired, agent)

        assert len(sent) == 1 and sent[0].startswith("⚠️"), sent
        assert "nope" in sent[0]

    async def test_a_pause_elsewhere_says_where_to_answer(self, wired, fake_llm):
        agent = await _orchestrator(_orch("orch_pauses", _asks(), "h"))

        sent = await self._talk(wired, agent)

        assert len(sent) == 1
        assert "Ship it?" in sent[0] and "run_orch_pauses_" in sent[0]

    async def test_a_pause_on_this_channel_is_asked_once(self, wired, fake_llm):
        agent = await _orchestrator(_orch("orch_asks_here", _asks(wired.channel["id"]), "h"))

        sent = await self._talk(wired, agent)

        assert sent == []                          # nothing on top of the question
        assert len(wired.rest) == 1 and "Ship it?" in wired.rest[0][1]


class TestOnASchedule:
    @pytest.fixture
    def scheduler(self):
        from core.scheduler import ScheduleManager
        manager = ScheduleManager()
        manager._server_module = types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None,
                                                       tool_router={})
        return manager

    async def test_the_orchestration_runs_under_the_schedule_run_id(self, scheduler, fake_llm):
        from core.orchestration.state import SharedState
        agent = await _orchestrator(_orch("orch_sched", PRINTS, "p"))
        sched_log = types.SimpleNamespace(run_id="schedulerun_x_1", log_event=lambda ev: None)

        out = await scheduler._run_agent_schedule(
            {"id": "sched_1", "name": "Daily", "target_id": agent["id"], "prompt": "go"}, sched_log)

        assert out == "workflow ran"
        assert (await SharedState.restore("schedulerun_x_1")).run.status == "completed"

    async def test_a_paused_run_is_logged_paused(self, scheduler, fake_llm, monkeypatch):
        import core.schedule_logger as logger_mod
        from core.scheduler import schedule_store
        ended = []
        real = logger_mod.ScheduleLogger

        class Recording(real):
            def run_end(self, status):
                ended.append(status)
                super().run_end(status)

        monkeypatch.setattr(logger_mod, "ScheduleLogger", Recording)
        agent = await _orchestrator(_orch("orch_sched_pause", _asks(), "h"))
        await schedule_store.save({"id": "sched_pause", "name": "Daily", "target_type": "agent",
                                   "target_id": agent["id"], "prompt": "go", "enabled": True,
                                   "schedule_type": "interval", "interval_minutes": 60})

        await scheduler._execute_schedule("sched_pause")

        assert ended == ["paused"]
