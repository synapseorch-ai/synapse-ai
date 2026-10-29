"""A schedule's log and a workflow's log are kept as a chat turn is (F1185, BRW3 D331; after F1184).

A schedule's log opens with a banner that carries its prompt, a member's own text. It was written before anything in
the scheduler read the tenant's variables, so a secret typed into the prompt went into the log as typed. Every way a
schedule's log opens — an occurrence, Run Now, the schedule turned off — now reads the variables first. And both
logs take the storage scrub (`secret_scrub.for_storage`): secrets by name, card numbers masked, ids left alone.
"""
from __future__ import annotations

import types

import pytest
from _fakes import canary
from _fakes import seed as S

canary_secret = canary.canary_secret
CANARY, MARK = canary.CANARY, canary.MARK

CARD = "4111 1111 1111 1111"  # passes Luhn
NOT_A_CARD = "4111 1111 1111 1112"
PROMPT = f"Charge {CARD} for order {NOT_A_CARD} with key {CANARY}."
SCHEDULE = "sched_4222222222222"  # an id whose digits pass the card test


@pytest.fixture
def manager(monkeypatch):
    from core.scheduler import ScheduleManager
    m = ScheduleManager()
    m._server_module = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})

    async def _run(s, sched_log):  # the log as it opens: the run itself is another test's
        sched_log.run_end("completed")
        sched_log.close()

    monkeypatch.setattr(m, "_run", _run)
    return m


async def _schedule(target_id: str) -> None:
    from core.store import schedules as schedule_store
    await schedule_store.save({"id": SCHEDULE, "name": "Nightly", "target_type": "agent", "target_id": target_id,
                               "prompt": PROMPT, "enabled": True, "schedule_type": "interval",
                               "interval_minutes": 60})


def _logs(root) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in root.rglob("*.log")]


def _kept_right(root) -> None:
    logs = _logs(root)
    assert logs, "the log was written"
    # The logs, not the schedule itself: its saved prompt is the member's own text, kept as they wrote it.
    joined = "\n".join(logs)
    assert CANARY not in joined, [p.name for p in root.rglob("*.log") if CANARY in p.read_text(encoding="utf-8")]
    assert CARD not in joined and "[card number]" in joined and NOT_A_CARD in joined
    assert MARK in joined
    assert SCHEDULE in joined, "an id is not a card"


class TestAScheduleLog:
    async def test_an_occurrence(self, canary_secret, manager, tmp_path):
        await S.seed_agents([S.make_agent(id="agent_live", name="Billing")])
        await _schedule("agent_live")
        await manager._execute_schedule(SCHEDULE)
        _kept_right(tmp_path)

    async def test_run_now(self, canary_secret, manager, tmp_path):
        import asyncio

        await S.seed_agents([S.make_agent(id="agent_live", name="Billing")])
        await _schedule("agent_live")
        await manager.trigger_now(SCHEDULE)
        await asyncio.gather(*manager._manual_runs)
        _kept_right(tmp_path)

    async def test_the_schedule_turned_off(self, canary_secret, manager, tmp_path):
        await _schedule("agent_gone")
        await manager._execute_schedule(SCHEDULE)
        _kept_right(tmp_path)


async def test_a_workflows_log_holds_no_card_number(tmp_path):
    from core.models_orchestration import Orchestration
    from core.orchestration.engine import OrchestrationEngine

    orch = S.make_orchestration(id="orch_kept", entry_step_id="say", steps=[
        {"id": "say", "name": "Say", "type": "print", "print_content": "noted", "next_step_id": None}])
    await S.seed_orchestrations([orch])
    engine = OrchestrationEngine(Orchestration.model_validate(orch),
                                 types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={}))
    [e async for e in engine.run(f"pay {CARD} for order {NOT_A_CARD}", run_id="run_4222222222222")]

    joined = "\n".join(_logs(tmp_path))
    assert joined, "the run was logged"
    assert CARD not in joined and "[card number]" in joined and NOT_A_CARD in joined
    assert "run_4222222222222" in joined, "an id is not a card"
