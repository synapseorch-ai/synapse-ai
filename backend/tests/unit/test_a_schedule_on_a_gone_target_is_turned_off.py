"""A schedule whose target is gone is skipped, said once, and turned off (F468).

A schedule kept firing at what it targets after that was deleted: a workflow
deleted before F408 refused it — self-hosted deletes the row, so every
occurrence logged "Orchestration … not found"; cloud keeps a deleted workflow's
row and went on *running* it. An agent schedule whose agent was deleted ran
whichever agent is active (F518) on every occurrence, and one whose workflow
agent's workflow was deleted failed on "no longer exists" each time. Nothing
turned any of them off, and nothing said why to anyone not reading the log.

Naveen's call (D276): the occurrence is skipped and said once — the bell, the
schedule's log — and the schedule is turned off, so it is never fired again
until someone points it at something that exists.
"""
import types

import pytest

from _fakes import seed as S
from core.notifications import hub


@pytest.fixture
def manager(monkeypatch):
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


async def _schedule(sid, target_type, target_id, name="Nightly"):
    from core.store import schedules as schedule_store
    await schedule_store.save({"id": sid, "name": name, "target_type": target_type, "target_id": target_id,
                               "prompt": "go", "enabled": True, "schedule_type": "interval",
                               "interval_minutes": 60})


async def _row(sid):
    from core.store import schedules as schedule_store
    return await schedule_store.get(sid)


def _told(sid):
    return [n for n in hub.list() if n["kind"] == "schedule_skipped" and n["data"].get("schedule_id") == sid]


class TestAGoneTargetTurnsItOff:
    async def test_a_workflow_deleted_outright(self, manager):
        await _schedule("sched_g1", "orchestration", "orch_gone", name="Month end")

        await manager._execute_schedule("sched_g1")

        assert manager.ran == []
        assert (await _row("sched_g1"))["enabled"] is False
        [item] = _told("sched_g1")
        assert item["title"] == "Month end was turned off"
        assert item["body"].startswith("The workflow it runs was deleted, so this schedule was turned off.")

    async def test_a_workflow_deleted_and_kept_as_a_record(self, manager):
        await S.seed_orchestrations([S.make_orchestration(id="orch_ret", name="Retired", is_active=False)])
        await _schedule("sched_g2", "orchestration", "orch_ret")

        await manager._execute_schedule("sched_g2")

        assert manager.ran == []
        assert (await _row("sched_g2"))["enabled"] is False

    async def test_an_agent_deleted(self, manager):
        """It used to run whichever agent is active instead (F518)."""
        await S.seed_agents([S.make_agent(id="agent_other", name="Someone else")])
        await _schedule("sched_g3", "agent", "agent_gone")

        await manager._execute_schedule("sched_g3")

        assert manager.ran == []
        assert (await _row("sched_g3"))["enabled"] is False
        assert _told("sched_g3")[0]["body"].startswith("The agent it runs was deleted")

    async def test_a_workflow_agent_whose_workflow_was_deleted(self, manager):
        await S.seed_agents([S.make_agent(id="agent_wf", name="Invoice desk", type="orchestrator",
                                          orchestration_id="orch_gone")])
        await _schedule("sched_g4", "agent", "agent_wf")

        await manager._execute_schedule("sched_g4")

        assert manager.ran == []
        assert (await _row("sched_g4"))["enabled"] is False
        assert _told("sched_g4")[0]["body"].startswith(
            "The workflow its agent 'Invoice desk' runs was deleted")


class TestALiveTargetRunsAsBefore:
    async def test_a_live_workflow(self, manager):
        await S.seed_orchestrations([S.make_orchestration(id="orch_live", name="Live")])
        await _schedule("sched_l1", "orchestration", "orch_live")

        await manager._execute_schedule("sched_l1")

        assert manager.ran == ["sched_l1"]
        assert (await _row("sched_l1"))["enabled"] is True
        assert _told("sched_l1") == []

    async def test_a_live_agent(self, manager):
        await S.seed_agents([S.make_agent(id="agent_live", name="Live")])
        await _schedule("sched_l2", "agent", "agent_live")

        await manager._execute_schedule("sched_l2")

        assert manager.ran == ["sched_l2"]

    async def test_an_agent_schedule_naming_no_agent_runs_the_active_one_as_before(self, manager):
        await S.seed_agents([S.make_agent(id="agent_live", name="Live")])
        await _schedule("sched_l3", "agent", "")

        await manager._execute_schedule("sched_l3")

        assert manager.ran == ["sched_l3"]


class TestTheSentence:
    def test_cloud_ends_its_notice_with_the_same_words(self):
        from core.scheduler import gone_target_sentence
        assert gone_target_sentence("the workflow it runs", "workflow") == (
            "the workflow it runs was deleted, so this schedule was turned off. "
            "Point it at another workflow, or delete it"
        )
