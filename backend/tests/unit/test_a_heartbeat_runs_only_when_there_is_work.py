"""A heartbeat schedule runs only while a board it works holds an open task (F1727).

A schedule that waits for work (`run_when: has_work`) checks at its time and runs only when a board its run would
work on — its own board, a workflow's board, or a shared board it may `work` — has an open task card. Otherwise
the occurrence passes with no run, no log and no model call, so an idle heartbeat costs nothing.

A task added to one of those boards brings its next check forward to now (`wake_on_new_task`, on by default), so
the work is picked up within a tick rather than at the next interval. `max_runs_per_day` caps how many occurrences
any schedule starts in a day on its own clock, so a burst of cards never becomes a burst of spend. A heartbeat
never runs twice at once: a due check while it runs waits for that run to end. Run Now is a person's ask and is
none of these: it runs at once.
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest

from _fakes import seed as S
from _fakes.knowledge_env import use_saved_settings
from core.store import boards as board_store
from core.store import schedules as schedule_store

PERSON = board_store.Actor(person="owner@example.com", source="person")


@pytest.fixture(autouse=True)
async def _store_ready():
    from core.store import get_store
    await get_store()


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings
    await save_settings({"boards_enabled": True})


@pytest.fixture
def wakes():
    """The wake on a new task, installed as the server's scheduler installs it at start."""
    from core.boards import heartbeat
    heartbeat.install()
    yield
    heartbeat.uninstall()


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


async def _agent(agent_id="agent_hb", **extra):
    await S.seed_agents([S.make_agent(id=agent_id, name="Inbox clerk", **extra)])
    return agent_id


async def _schedule(sid, *, target_type="agent", target_id="agent_hb", run_when="has_work", **extra):
    item = {"id": sid, "name": "Inbox", "target_type": target_type, "target_id": target_id,
            "prompt": "Work the open tasks on your board.", "enabled": True, "schedule_type": "interval",
            "interval_value": 15, "interval_unit": "minutes", "run_when": run_when, **extra}
    await schedule_store.save(item)
    return item


async def _task(scope, scope_id, title="Reply to the Harbor & Pine enquiry", kind="task"):
    board = await board_store.get_or_create_scoped_board(scope, scope_id, by=PERSON)
    card, _ = await board_store.add_card(board.id, title=title, kind=kind, by=PERSON)
    return board, card


def _when(row) -> datetime:
    return datetime.fromisoformat(row["next_run_at"].replace("Z", "+00:00"))


class TestItRunsOnlyWithOpenWork:
    async def test_an_empty_board_passes_with_no_run(self, manager, boards_on):
        await _agent()
        await _schedule("sched_hb1")

        await manager._execute_schedule("sched_hb1")

        assert manager.ran == []
        row = await schedule_store.get("sched_hb1")
        assert _when(row) > datetime.now(timezone.utc), "the occurrence passed: its next check is ahead"
        assert row["last_run_at"] is None

    async def test_an_open_task_on_its_own_board_runs_it(self, manager, boards_on):
        await _agent()
        await _schedule("sched_hb2")
        await _task("schedule", "sched_hb2")

        await manager._execute_schedule("sched_hb2")

        assert manager.ran == ["sched_hb2"]

    async def test_a_note_a_claimed_task_and_a_closed_task_are_not_work(self, manager, boards_on):
        await _agent()
        await _schedule("sched_hb3")
        board, _ = await _task("schedule", "sched_hb3", title="FYI: office closed Friday", kind="note")
        _, held = await _task("schedule", "sched_hb3", title="Being handled")
        holder = board_store.Holder(agent="agent_other", key="run_x", label="Another run")
        await board_store.claim(held.id, holder, by=PERSON)
        _, done = await _task("schedule", "sched_hb3", title="Already answered")
        claimed, _ = await board_store.claim(done.id, holder, by=PERSON)
        await board_store.update_card(done.id, version=claimed.version, by=PERSON, holder=holder,
                                      status="done", result="sent")

        await manager._execute_schedule("sched_hb3")

        assert manager.ran == []

    async def test_a_shared_board_it_may_work_counts_and_one_it_may_only_add_to_does_not(self, manager, boards_on):
        await _agent()
        await _schedule("sched_hb4")
        add_only = await board_store.create_shared_board("Leads", by=PERSON)
        await board_store.set_access(add_only.id, [("agent", "agent_hb", "add")], by=PERSON)
        await board_store.add_card(add_only.id, title="New lead", by=PERSON)

        await manager._execute_schedule("sched_hb4")
        assert manager.ran == []

        work = await board_store.create_shared_board("Support", by=PERSON)
        await board_store.set_access(work.id, [("schedule", "sched_hb4", "work")], by=PERSON)
        await board_store.add_card(work.id, title="Refund question", by=PERSON)
        await schedule_store.set_next_run("sched_hb4", datetime.now(timezone.utc))

        await manager._execute_schedule("sched_hb4")
        assert manager.ran == ["sched_hb4"]

    async def test_a_workflow_waits_for_a_task_on_its_own_board(self, manager, boards_on):
        await S.seed_orchestrations([S.make_orchestration(id="orch_hb", name="Triage")])
        await _schedule("sched_hb5", target_type="orchestration", target_id="orch_hb")

        await manager._execute_schedule("sched_hb5")
        assert manager.ran == []

        await _task("workflow", "orch_hb")
        await schedule_store.set_next_run("sched_hb5", datetime.now(timezone.utc))
        await manager._execute_schedule("sched_hb5")
        assert manager.ran == ["sched_hb5"]

    async def test_a_workflow_agent_waits_on_its_workflows_board(self, manager, boards_on):
        """An Orchestrator agent runs its workflow, whose steps read the workflow's board, not the schedule's."""
        await S.seed_orchestrations([S.make_orchestration(id="orch_hb6", name="Triage")])
        await S.seed_agents([S.make_orchestrator_agent("orch_hb6", id="agent_orch6")])
        await _schedule("sched_hb6", target_id="agent_orch6")
        await _task("schedule", "sched_hb6", title="On a board its run never reads")

        await manager._execute_schedule("sched_hb6")
        assert manager.ran == []

        await _task("workflow", "orch_hb6")
        await schedule_store.set_next_run("sched_hb6", datetime.now(timezone.utc))
        await manager._execute_schedule("sched_hb6")
        assert manager.ran == ["sched_hb6"]

    async def test_an_agent_with_its_own_board_off_does_not_wait_on_it(self, manager, boards_on):
        await _agent(boards={"own": False})
        await _schedule("sched_hb7")
        await _task("schedule", "sched_hb7")

        await manager._execute_schedule("sched_hb7")

        assert manager.ran == []

    async def test_with_boards_off_it_never_runs(self, manager, monkeypatch):
        use_saved_settings(monkeypatch)
        await _agent()
        await _schedule("sched_hb8")
        await _task("schedule", "sched_hb8")

        await manager._execute_schedule("sched_hb8")

        assert manager.ran == []

    async def test_a_schedule_that_always_runs_is_unchanged(self, manager, boards_on):
        await _agent()
        await _schedule("sched_hb9", run_when="always")

        await manager._execute_schedule("sched_hb9")

        assert manager.ran == ["sched_hb9"]


class TestANewTaskWakesIt:
    async def test_a_task_added_brings_its_next_check_to_now(self, boards_on, wakes):
        await _agent()
        await _schedule("sched_w1")
        await schedule_store.set_next_run("sched_w1", datetime.now(timezone.utc) + timedelta(hours=1))

        await _task("schedule", "sched_w1")

        assert _when(await schedule_store.get("sched_w1")) <= datetime.now(timezone.utc)

    async def test_a_task_on_a_shared_board_it_works_wakes_it(self, boards_on, wakes):
        await _agent()
        await _schedule("sched_w2")
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await schedule_store.set_next_run("sched_w2", later)
        support = await board_store.create_shared_board("Support", by=PERSON)
        await board_store.set_access(support.id, [("agent", "agent_hb", "work")], by=PERSON)

        await board_store.add_card(support.id, title="Refund question", by=PERSON)

        assert _when(await schedule_store.get("sched_w2")) <= datetime.now(timezone.utc)

    async def test_a_note_does_not_wake_it(self, boards_on, wakes):
        await _agent()
        await _schedule("sched_w3")
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await schedule_store.set_next_run("sched_w3", later)

        await _task("schedule", "sched_w3", title="FYI", kind="note")

        assert _when(await schedule_store.get("sched_w3")) > datetime.now(timezone.utc)

    async def test_wake_turned_off_waits_for_its_interval(self, boards_on, wakes):
        await _agent()
        await _schedule("sched_w4", wake_on_new_task=False)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await schedule_store.set_next_run("sched_w4", later)

        await _task("schedule", "sched_w4")

        assert _when(await schedule_store.get("sched_w4")) > datetime.now(timezone.utc)

    async def test_a_schedule_that_always_runs_keeps_its_time(self, boards_on, wakes):
        await S.seed_orchestrations([S.make_orchestration(id="orch_w5", name="Triage")])
        await _schedule("sched_w5", target_type="orchestration", target_id="orch_w5", run_when="always")
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await schedule_store.set_next_run("sched_w5", later)

        await _task("workflow", "orch_w5")

        assert _when(await schedule_store.get("sched_w5")) > datetime.now(timezone.utc)

    async def test_a_turned_off_heartbeat_is_not_woken(self, boards_on, wakes):
        await _agent()
        await _schedule("sched_w6", enabled=False)
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await schedule_store.set_next_run("sched_w6", later)

        await _task("schedule", "sched_w6")

        assert _when(await schedule_store.get("sched_w6")) > datetime.now(timezone.utc)

    async def test_a_wake_that_fails_never_fails_the_card(self, boards_on, monkeypatch, wakes):
        from core.boards import heartbeat

        async def broken(*_a, **_k):
            raise RuntimeError("the schedules table is unreachable")

        monkeypatch.setattr(heartbeat, "wake_for", broken)
        await _agent()
        await _schedule("sched_w7")

        _, card = await _task("schedule", "sched_w7")

        assert card.status == "open"

    async def test_a_wake_is_told_to_the_save_observer_so_a_host_that_arms_schedules_arms_it(
            self, boards_on, wakes):
        """Cloud fires a schedule by arming a job for its next run; a wake that only moved the column would be
        listed as due and never fire there."""
        await _agent()
        await _schedule("sched_w8")
        await schedule_store.set_next_run("sched_w8", datetime.now(timezone.utc) + timedelta(hours=1))
        told: list[dict] = []

        async def observer(item):
            told.append(item)

        schedule_store.set_saved_observer(observer)
        try:
            await _task("schedule", "sched_w8")
        finally:
            schedule_store.set_saved_observer(None)

        [item] = told
        assert item["id"] == "sched_w8"
        assert datetime.fromisoformat(item["next_run_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)


class TestWhatANewTaskWakesIsOnlyRead:
    """`to_wake` names the heartbeats a new task should wake and moves none, so a host that fires schedules its own
    way (and must not go through the save observer) wakes each as it does."""

    async def _support(self, sids):
        board = await board_store.create_shared_board("Support", by=PERSON)
        await board_store.set_access(board.id, [("schedule", sid, "work") for sid in sids], by=PERSON)
        return board

    async def test_it_names_only_a_woken_heartbeat_not_yet_due_and_moves_nothing(self, boards_on):
        from core.boards import heartbeat

        await _agent()
        later = datetime.now(timezone.utc) + timedelta(hours=1)
        await _schedule("sched_tw1")
        await _schedule("sched_tw2", run_when="always")
        await _schedule("sched_tw3", wake_on_new_task=False)
        await _schedule("sched_tw4", enabled=False)
        await _schedule("sched_tw5")
        for sid in ("sched_tw1", "sched_tw2", "sched_tw3", "sched_tw4"):
            await schedule_store.set_next_run(sid, later)
        await schedule_store.set_next_run("sched_tw5", datetime.now(timezone.utc) - timedelta(minutes=1))
        board = await self._support(["sched_tw1", "sched_tw2", "sched_tw3", "sched_tw4", "sched_tw5"])
        card, _ = await board_store.add_card(board.id, title="Refund question", by=PERSON)

        named = await heartbeat.to_wake(board, card)

        assert [s["id"] for s in named] == ["sched_tw1"]
        assert _when(await schedule_store.get("sched_tw1")) > datetime.now(timezone.utc), "it moved nothing"

    async def test_it_reads_the_schedules_it_is_given(self, boards_on):
        from core.boards import heartbeat

        await _agent()
        given = await _schedule("sched_tw6")
        board = await self._support(["sched_tw6"])
        card, _ = await board_store.add_card(board.id, title="Refund question", by=PERSON)

        assert await heartbeat.to_wake(board, card, [{**given, "next_run_at": None}]) == [
            {**given, "next_run_at": None}]
        assert await heartbeat.to_wake(board, card, []) == []

    async def test_a_note_wakes_nothing(self, boards_on):
        from core.boards import heartbeat

        await _agent()
        await _schedule("sched_tw7")
        board, note = await _task("schedule", "sched_tw7", title="FYI", kind="note")

        assert await heartbeat.to_wake(board, note) == []


class TestTheDayIsTheSchedulesOwn:
    def test_day_of_reads_the_schedules_clock(self):
        from core.scheduler import day_of

        at = datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc)
        assert day_of({"timezone": "America/New_York"}, at) == "2026-11-02"
        assert day_of({}, at) == "2026-11-03"
        assert day_of({"timezone": "UTC"}, at) == "2026-11-03"
        assert day_of({"timezone": "Not/A_Zone"}, at) == "2026-11-03", "a zone it cannot read counts on UTC"


class TestADailyLimit:
    async def test_it_starts_at_most_its_limit_in_a_day_then_again_the_next(self, manager, boards_on, monkeypatch):
        import core.scheduler as scheduler

        day = [datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)]
        monkeypatch.setattr(scheduler, "_utc_now", lambda: day[0])
        await _agent()
        await _schedule("sched_cap", max_runs_per_day=2)
        await _task("schedule", "sched_cap")

        for _ in range(3):
            await manager._execute_schedule("sched_cap")
        assert manager.ran == ["sched_cap", "sched_cap"]

        day[0] = day[0] + timedelta(days=1)
        await manager._execute_schedule("sched_cap")
        assert manager.ran == ["sched_cap"] * 3

    async def test_the_day_is_the_schedules_own(self, manager, boards_on, monkeypatch):
        """23:30 and 00:30 UTC are one day in New York: the limit holds across UTC midnight there."""
        import core.scheduler as scheduler

        now = [datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc)]
        monkeypatch.setattr(scheduler, "_utc_now", lambda: now[0])
        await _agent()
        await _schedule("sched_tz", max_runs_per_day=1, timezone="America/New_York")
        await _task("schedule", "sched_tz")

        await manager._execute_schedule("sched_tz")
        now[0] = now[0] + timedelta(hours=1)
        await manager._execute_schedule("sched_tz")

        assert manager.ran == ["sched_tz"]

    async def test_a_limit_holds_for_a_schedule_that_always_runs(self, manager, boards_on):
        await _agent()
        await _schedule("sched_cap2", run_when="always", max_runs_per_day=1)

        await manager._execute_schedule("sched_cap2")
        await manager._execute_schedule("sched_cap2")

        assert manager.ran == ["sched_cap2"]

    async def test_no_limit_is_zero(self, manager, boards_on):
        await _agent()
        await _schedule("sched_cap3", run_when="always", max_runs_per_day=0)

        for _ in range(3):
            await manager._execute_schedule("sched_cap3")

        assert manager.ran == ["sched_cap3"] * 3


class TestNeverTwiceAtOnce:
    async def test_a_due_check_while_it_runs_waits_for_that_run(self, boards_on, monkeypatch):
        from core.scheduler import ScheduleManager

        m = ScheduleManager()
        m._server_module = types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})
        release = asyncio.Event()
        started: list[str] = []

        async def _run(s, sched_log):
            started.append(s["id"])
            await release.wait()
            sched_log.run_end("completed")
            sched_log.close()

        monkeypatch.setattr(m, "_run", _run)
        await _agent()
        await _schedule("sched_busy")
        await _task("schedule", "sched_busy")

        first = asyncio.create_task(m._execute_schedule("sched_busy"))
        try:
            # Bounded: a second run that starts waits on the same event, and would hold the test forever.
            async with asyncio.timeout(10):
                while not started:
                    await asyncio.sleep(0.01)
            now = datetime.now(timezone.utc)
            await schedule_store.set_next_run("sched_busy", now)
            second = asyncio.create_task(m._execute_schedule("sched_busy"))
            done, _ = await asyncio.wait({second}, timeout=5)

            assert second in done, "a second run started while the first still runs"
            assert started == ["sched_busy"]
            assert _when(await schedule_store.get("sched_busy")) <= now, "still due: it fires once the run ends"
        finally:
            release.set()
            await asyncio.wait_for(first, 10)


class TestRunNowIsAPersonsAsk:
    async def test_run_now_runs_with_no_work_and_past_its_limit(self, manager, boards_on):
        await _agent()
        await _schedule("sched_now", max_runs_per_day=1)

        await manager.trigger_now("sched_now")
        await manager.trigger_now("sched_now")
        await asyncio.gather(*manager._manual_runs)

        assert manager.ran == ["sched_now", "sched_now"]


class TestWhatASaveRefuses:
    def test_an_unknown_run_when_and_a_limit_out_of_range(self):
        from core.models_schedule import ScheduleInvalid, validate_timing

        base = {"schedule_type": "interval", "interval_value": 15, "interval_unit": "minutes"}
        with pytest.raises(ScheduleInvalid, match="always.*has_work"):
            validate_timing({**base, "run_when": "sometimes"})
        with pytest.raises(ScheduleInvalid, match="0 to 1,440"):
            validate_timing({**base, "max_runs_per_day": 5000})
        with pytest.raises(ScheduleInvalid, match="0 to 1,440"):
            validate_timing({**base, "max_runs_per_day": -1})
        validate_timing({**base, "max_runs_per_day": 0})

    async def test_waiting_for_work_needs_boards_on(self, monkeypatch):
        use_saved_settings(monkeypatch)
        from core.models_schedule import ScheduleInvalid, validate_timing

        base = {"schedule_type": "interval", "interval_value": 15, "interval_unit": "minutes"}
        with pytest.raises(ScheduleInvalid, match="Boards"):
            validate_timing({**base, "run_when": "has_work"})
        validate_timing({**base, "run_when": "always"})
