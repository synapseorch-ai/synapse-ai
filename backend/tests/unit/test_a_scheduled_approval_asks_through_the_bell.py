"""A scheduled agent's gated call asks through the bell (F268).

Schedule → an agent whose tool needs approval → Run Now: the log said "SCHEDULE
RUN FINISHED — Status: paused" after 3.6 s, and that was all. No notification,
no conversation holding the question, and no way anywhere to answer it — the
run was parked for ever.

Now the call is parked like a chat turn's (in the schedule's own conversation),
the bell asks with Approve / Refuse, and the answer resumes the same schedule
run: the call runs once (or not at all), under the schedule's own source and run
id. Nobody answering for 24 hours expires it unrun; the schedule firing again
replaces the question (Naveen, 2026-09-28).
"""
import types
import unittest.mock as _m
from datetime import datetime, timedelta, timezone

import pytest

from _fakes import seed as S
from _fakes.fake_llm import tool_call
from core.notifications import hub
from core.store import sessions as store

AGENT = "agent_f268"
SCHED = "sched_f268"
SESSION = f"schedule_{SCHED}"
DANGER = {"type": "function", "function": {"name": "danger", "description": "deletes rows",
                                           "parameters": {"type": "object",
                                                          "properties": {"rows": {"type": "string"}}}}}


@pytest.fixture
async def schedule():
    from core.scheduler import schedule_store
    await S.seed_agents([S.make_agent(id=AGENT, name="Nightly cleaner", tools=["all"], skip_default_tools=True,
                                      tool_policy={"undeclared": "confirm"})])
    s = {"id": SCHED, "name": "Nightly cleanup", "target_type": "agent", "target_id": AGENT,
         "prompt": "clean up yesterday's rows", "enabled": True, "schedule_type": "interval",
         "interval_minutes": 60}
    await schedule_store.save(s)
    return s


@pytest.fixture
def manager(test_app):
    import core.server as server
    from core.scheduler import ScheduleManager
    m = ScheduleManager()
    m._server_module = types.SimpleNamespace(agent_sessions={"x": object()}, memory_store=None, tool_router={})
    test_app.state.schedule_manager = m
    server.schedule_manager = m
    return m


@pytest.fixture
def executed():
    import core.react_engine as re_mod
    real = re_mod.run_agent_step
    ran: list[tuple[str, dict]] = []

    async def wrapped(**kwargs):
        if kwargs.get("agent_id") == AGENT:
            async def executor(name, args):
                ran.append((name, args))
                return f"deleted {args.get('rows')}"
            kwargs.setdefault("tools_override", [DANGER])
            kwargs.setdefault("tool_executor", executor)
        async for ev in real(**kwargs):
            yield ev

    with _m.patch.object(re_mod, "run_agent_step", wrapped):
        yield ran


async def _fire(manager, fake_llm, rows="yesterday"):
    fake_llm.script([tool_call("danger", rows=rows)])
    sched_log = manager._schedule_logger({"id": SCHED, "name": "Nightly cleanup", "target_type": "agent",
                                          "target_id": AGENT, "prompt": "clean up yesterday's rows"})
    from core.store import schedules as schedule_store
    await manager._run(await schedule_store.get(SCHED), sched_log)
    return sched_log.run_id


def _asks():
    return [i for i in hub.list() if i["kind"] == "tool_approval"]


async def _settled():
    from core import chat_turns
    turn = chat_turns.find(SESSION)
    if turn is not None:
        await turn.wait()


class TestItAsks:
    async def test_the_call_is_parked_and_the_bell_asks(self, schedule, manager, executed, fake_llm):
        run_id = await _fire(manager, fake_llm)

        pending = await store.get_pending_approval(SESSION, AGENT)
        assert pending and pending["tool_name"] == "danger"
        assert pending["schedule"]["run_id"] == run_id and pending["schedule"]["id"] == SCHED
        [ask] = _asks()
        assert ask["title"] == "Nightly cleanup needs approval"
        assert ask["data"]["approval_id"] == pending["approval_id"]
        assert ask["data"]["schedule_id"] == SCHED and ask["data"]["tool_args"] == {"rows": "yesterday"}
        assert not ask.get("resolved")
        assert executed == []

        from core.schedule_logger import ScheduleLogger
        assert "waiting for approval" in ScheduleLogger.get_log(run_id).lower()


class TestAnswering:
    async def test_approve_runs_it_once_in_the_same_run(self, schedule, manager, executed, fake_llm, client):
        run_id = await _fire(manager, fake_llm)
        pending = await store.get_pending_approval(SESSION, AGENT)
        fake_llm.script(["Deleted yesterday's rows."])

        resp = await client.post(f"/api/schedules/{SCHED}/approve",
                                 json={"approve": True, "approval_id": pending["approval_id"]})
        assert resp.status_code == 200, resp.text
        await _settled()

        assert executed == [("danger", {"rows": "yesterday"})]
        assert await store.get_pending_approval(SESSION, AGENT) is None
        [ask] = _asks()
        assert ask["resolved"] and ask["resolved_as"] == "allowed"
        from core.schedule_logger import ScheduleLogger
        log = ScheduleLogger.get_log(run_id)
        assert "Deleted yesterday's rows." in log, "the same run's log carries on"
        finished = log.split("SCHEDULE RUN FINISHED")
        assert len(finished) == 3, "paused, then finished again after the answer"
        assert "paused" in finished[1] and "completed" in finished[2]
        turns = (await client.get(f"/api/sessions/{SESSION}/history", params={"agent_id": AGENT})).json()["turns"]
        assert [t["assistant"] for t in turns] == ["Deleted yesterday's rows."]

    async def test_the_resumed_turn_is_the_schedules(self, schedule, manager, executed, fake_llm, client):
        """The source and the run id carry over: the remember gate, the memory
        trace and Usage's per-schedule rows all read them."""
        seen = []
        import core.react_engine as re_mod
        inner = re_mod.run_agent_step

        async def spy(**kwargs):
            seen.append((kwargs.get("source"), kwargs.get("run_id"), kwargs.get("resume_state") is not None))
            async for ev in inner(**kwargs):
                yield ev

        run_id = await _fire(manager, fake_llm)
        pending = await store.get_pending_approval(SESSION, AGENT)
        fake_llm.script(["ok"])
        with _m.patch.object(re_mod, "run_agent_step", spy):
            await client.post(f"/api/schedules/{SCHED}/approve",
                              json={"approve": True, "approval_id": pending["approval_id"]})
            await _settled()
        assert seen == [("schedule", run_id, True)]

    async def test_refuse_runs_nothing(self, schedule, manager, executed, fake_llm, client):
        await _fire(manager, fake_llm)
        pending = await store.get_pending_approval(SESSION, AGENT)
        fake_llm.script(["Understood, nothing deleted."])
        resp = await client.post(f"/api/schedules/{SCHED}/approve",
                                 json={"approve": False, "approval_id": pending["approval_id"]})
        assert resp.status_code == 200
        await _settled()
        assert executed == []
        [ask] = _asks()
        assert ask["resolved_as"] == "refused"

    async def test_a_stale_answer_is_refused(self, schedule, manager, executed, fake_llm, client):
        await _fire(manager, fake_llm)
        resp = await client.post(f"/api/schedules/{SCHED}/approve", json={"approve": True, "approval_id": "nope"})
        assert resp.status_code == 409
        assert await store.get_pending_approval(SESSION, AGENT) is not None
        again = await client.post("/api/schedules/sched_none/approve", json={"approve": True})
        assert again.status_code == 409

    async def test_the_chat_door_answers_it_the_same_way(self, schedule, manager, executed, fake_llm, client):
        """The schedule's conversation is in History; its card's Allow there is
        the same answer, not a chat turn that forgets it was a schedule's."""
        run_id = await _fire(manager, fake_llm)
        fake_llm.script(["Deleted."])
        resp = await client.post(f"/chat/{SESSION}/approve", json={"approve": True, "agent_id": AGENT})
        assert resp.status_code == 200
        await _settled()
        assert executed == [("danger", {"rows": "yesterday"})]
        from core.schedule_logger import ScheduleLogger
        assert "Deleted." in ScheduleLogger.get_log(run_id)
        assert _asks()[0]["resolved_as"] == "allowed"


class TestNobodyAnswers:
    async def test_it_expires_unrun_after_24_hours(self, schedule, manager, executed, fake_llm, client):
        await _fire(manager, fake_llm)
        pending = await store.get_pending_approval(SESSION, AGENT)
        assert datetime.fromisoformat(pending["schedule"]["expires_at"]) - datetime.now(timezone.utc) \
            > timedelta(hours=23, minutes=59)

        await manager._expire_parked_approvals()
        assert await store.get_pending_approval(SESSION, AGENT) is not None, "not yet"

        pending["schedule"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await store.set_pending_approval(SESSION, AGENT, pending)
        calls = fake_llm.call_count
        await manager._expire_parked_approvals()

        assert await store.get_pending_approval(SESSION, AGENT) is None
        assert fake_llm.call_count == calls, "no model call"
        assert executed == []
        [ask] = _asks()
        assert ask["resolved_as"] == "expired"
        turns = (await client.get(f"/api/sessions/{SESSION}/history", params={"agent_id": AGENT})).json()["turns"]
        assert turns[-1]["approval"]["outcome"] == "expired"
        assert "24 hours" in turns[-1]["approval"]["why"]

    async def test_a_newer_run_replaces_the_question(self, schedule, manager, executed, fake_llm, client):
        await _fire(manager, fake_llm, rows="monday")
        first = await store.get_pending_approval(SESSION, AGENT)
        await _fire(manager, fake_llm, rows="tuesday")
        second = await store.get_pending_approval(SESSION, AGENT)

        assert second["tool_args"] == {"rows": "tuesday"} and second["approval_id"] != first["approval_id"]
        asks = sorted(_asks(), key=lambda i: i["id"])
        assert [a.get("resolved_as") for a in asks] == ["expired", None]
        turns = (await client.get(f"/api/sessions/{SESSION}/history", params={"agent_id": AGENT})).json()["turns"]
        assert turns[-1]["approval"]["why"] == "a newer run of this schedule started"
        assert executed == []
