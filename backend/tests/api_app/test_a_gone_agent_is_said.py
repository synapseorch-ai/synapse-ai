"""The app's doors say a deleted agent is gone, rather than answering as another (F518).

A chat naming a deleted agent (a conversation reopened after its agent was
deleted) was answered by the active agent; Run Now on an agent schedule whose
agent was deleted ran the active agent too, though a timed firing turns the
schedule off since F468. See `tests/unit/test_a_gone_agent_never_answers_as_another.py`.
"""
import asyncio

import pytest

from _fakes.fake_redis_stream import data_events
from tests._fakes import seed as _seed

OTHER = "agent_f518_other"
GONE = "agent_f518_gone"


@pytest.fixture(autouse=True)
async def _another_agent():
    await _seed.seed_agents([_seed.make_agent(id=OTHER, name="Someone else", skip_default_tools=True)])


async def test_a_chat_naming_a_gone_agent_says_so(client, monkeypatch, fake_llm):
    import core.server as server
    monkeypatch.setitem(server._session_tools, "_test", [])
    fake_llm.set_default("I am someone else.")

    resp = await client.post("/chat/stream", json={"message": "hi", "agent_id": GONE, "session_id": "s_f518"})

    errors = [e for e in data_events(resp.text.split("\n\n")) if e.get("type") == "error"]
    assert errors and errors[0]["message"] == f"Agent '{GONE}' no longer exists. It was deleted. Pick another agent."
    assert fake_llm.call_count == 0


@pytest.fixture
async def scheduler(test_app):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._server_module = test_app.state.server_module
    test_app.state.schedule_manager = mgr
    yield mgr
    await asyncio.gather(*mgr._manual_runs, return_exceptions=True)


async def test_run_now_on_a_gone_agent_is_refused_in_words(client, scheduler, fake_llm):
    from core.store import schedules as schedule_store
    await schedule_store.save({"id": "sched_f518_api", "name": "Nightly", "target_type": "agent",
                               "target_id": GONE, "prompt": "go", "enabled": True,
                               "schedule_type": "interval", "interval_minutes": 60})

    resp = await client.post("/api/schedules/sched_f518_api/run")

    assert resp.status_code == 409
    assert resp.json()["detail"] == (
        "The agent it runs was deleted, so this schedule was turned off. Point it at another agent, or delete it.")
    assert scheduler._manual_runs == set()
    assert fake_llm.call_count == 0
    assert (await schedule_store.get("sched_f518_api"))["enabled"] is False
