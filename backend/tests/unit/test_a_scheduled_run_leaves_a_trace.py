"""A scheduled run of a memory-enabled agent leaves a trace (F277).

Schedule → memory agent → Run Now: 200 session files before, 200 after, and
nothing anywhere mentioned the scheduled prompt. The per-turn trace was written
for `source == "chat"` only, the scheduler runs its turn as `"schedule"`, and an
agent schedule gets no run-end summary either. So "what did you do overnight?"
had nothing to find.

A scheduled turn now leaves the same trace a chat turn does, one entry per
firing in the schedule's own session file (Naveen, 2026-09-28).
"""
import types

import pytest

from _fakes import seed as S

from core import agent_memory as am


@pytest.fixture(autouse=True)
def _fresh_index():
    am.set_memory_index(None)
    yield
    am.set_memory_index(None)


def _server():
    return types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})


async def _fire(agent_id, prompt="Reconcile yesterday's invoices"):
    from core.scheduler import ScheduleManager

    manager = ScheduleManager()
    manager._server_module = _server()
    sched_log = types.SimpleNamespace(run_id="schedulerun_f277", log_event=lambda ev: None)
    return await manager._run_agent_schedule(
        {"id": "sched_night", "name": "Nightly", "target_id": agent_id, "prompt": prompt}, sched_log)


def _trace(agent_id):
    return am._store().get(am._session_key(agent_id, am._session_stem("schedule_sched_night"))) or ""


class TestAScheduledRun:
    async def test_it_leaves_a_trace_the_agent_can_find(self, fake_llm):
        agent = S.make_agent(id="agent_f277", tools=[], memory=True, skip_default_tools=True)
        await S.seed_agents([agent])
        fake_llm.script(["Reconciled 3 invoices; INV-2231 is short by $40."])

        assert await _fire(agent["id"]) == "Reconciled 3 invoices; INV-2231 is short by $40."

        trace = _trace(agent["id"])
        assert "Reconcile yesterday's invoices" in trace and "INV-2231" in trace
        hits = await am.search_memory(agent["id"], "invoices reconciled overnight")
        assert any(h["kind"] == "session" for h in hits), "what did you do overnight? — findable"

    async def test_each_firing_adds_its_own_entry(self, fake_llm):
        agent = S.make_agent(id="agent_f277b", tools=[], memory=True, skip_default_tools=True)
        await S.seed_agents([agent])
        fake_llm.script(["first night", "second night"])
        await _fire(agent["id"])
        await _fire(agent["id"])
        trace = _trace(agent["id"])
        assert "first night" in trace and "second night" in trace

    async def test_an_agent_without_memory_leaves_none(self, fake_llm):
        agent = S.make_agent(id="agent_f277c", tools=[], skip_default_tools=True)
        await S.seed_agents([agent])
        fake_llm.script(["done"])
        await _fire(agent["id"])
        assert _trace(agent["id"]) == ""
