"""A golden set whose run is cut ends, says why, and never holds its Start for good (F471).

`run_suite` wrote its ending only after its loop. A set whose job was cut — its
host's time limit, a stopping worker — kept `running` with its partial tally,
and Start answered "This set is already running." from then on: measured by
BILL1 on cloud with a three-second job limit (D271). A set its worker was killed
under, and a set the API was running when it restarted, did the same, and a job
handed out again after the kill met its own row and raised.

Now a cut set ends `stopped` with the case it stopped before; a set handed out
again ends instead of running its cases a second time (they may have acted);
and a `running` row nothing holds is over — Start and Stop end it, and so do
the sweeps (in scale mode by its lease, at boot because a set runs in the API).
"""
import asyncio
import types

import pytest

from _fakes import seed as S
from core.evals import store as evals_store
from core.evals import suites
from core.store import evals as store

VERDICT = '{"score": 7.0, "reasoning": "Fine.", "criteria": {}, "steps": {}}'


def _server():
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _setup(case_count=3):
    rub = await evals_store.save_rubric({
        "name": "Quality", "criteria": [{"name": "Overall"}], "scale": {"min": 0, "max": 10},
    })
    orch = S.make_orchestration(
        id="orch_cut", entry_step_id="p",
        steps=[{"id": "p", "name": "Say", "type": "print", "print_content": "hi",
                "output_key": "final", "next_step_id": None}],
        rubric_id=rub["id"],
    )
    await S.seed_orchestrations([orch])
    return await evals_store.save_suite({
        "name": "Regression set", "orchestration_id": "orch_cut",
        "cases": [{"name": f"case {i}", "input": f"input {i}"} for i in range(case_count)],
    })


async def _left_running(suite, esr, status="running", scored=1):
    await store.create_suite_run({
        "id": esr, "suite_id": suite["id"], "orchestration_id": "orch_cut", "status": status,
        "case_count": len(suite["cases"]), "rubric_id": "", "rubric_version": 1,
    })
    await store.update_suite_run(esr, scored_count=scored)


@pytest.fixture
def lease(fake_redis):
    from core.scale import run_lease
    run_lease.install(fake_redis, holder="api-test")
    yield fake_redis
    run_lease.install(None)


class TestACutSetEnds:
    async def test_cut_mid_case_it_ends_stopped_before_that_case(self, fake_llm):
        fake_llm.set_default(VERDICT)
        suite = await _setup(case_count=3)
        started = []

        async def run_case(run_id, session_id, go):
            started.append(run_id)
            if len(started) == 2:
                raise asyncio.CancelledError()  # the job's limit, a stopping worker
            await go()

        with pytest.raises(asyncio.CancelledError):
            await suites.run_suite(suite, _server(), suite_run_id="esr_cut1", run_case=run_case)

        row = await store.get_suite_run("esr_cut1")
        assert row["status"] == suites.STOPPED
        assert row["stop_reason"] == f"Stopped before case 2 of 3: {suites.SET_CUT}"
        assert row["ended_at"] and row["scored_count"] == 1

    async def test_a_cut_set_that_was_asked_to_stop_reads_cancelled(self, fake_llm):
        fake_llm.set_default(VERDICT)
        suite = await _setup(case_count=2)

        async def run_case(run_id, session_id, go):
            await suites.cancel("esr_cut2")
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await suites.run_suite(suite, _server(), suite_run_id="esr_cut2", run_case=run_case)
        row = await store.get_suite_run("esr_cut2")
        assert (row["status"], row["stop_reason"]) == ("cancelled", "")
        assert row["ended_at"]


class TestASetHandedOutAgain:
    async def test_it_ends_and_runs_no_case_again(self, fake_llm):
        """A worker killed under the set: its job is handed out again, and its row is already there."""
        suite = await _setup(case_count=3)
        await _left_running(suite, "esr_again", scored=1)
        ran = []

        async def run_case(run_id, session_id, go):
            ran.append(run_id)

        row = await suites.run_suite(suite, _server(), suite_run_id="esr_again", run_case=run_case)

        assert ran == []
        assert row["status"] == suites.STOPPED
        assert row["stop_reason"] == f"Stopped before case 2 of 3: {suites.SET_HANDED_OUT_AGAIN}"

    async def test_a_set_already_ended_is_left_as_it_was(self, fake_llm):
        suite = await _setup(case_count=3)
        await _left_running(suite, "esr_done", status="completed", scored=3)
        row = await suites.run_suite(suite, _server(), suite_run_id="esr_done")
        assert (row["status"], row["stop_reason"]) == ("completed", "")


class TestARowNothingHoldsIsOver:
    async def test_start_and_stop_end_it_where_sets_hold_leases(self, fake_llm, lease):
        suite = await _setup(case_count=4)
        await _left_running(suite, "esr_dead", scored=2)

        assert await suites.running_for(suite["id"]) is None
        row = await store.get_suite_run("esr_dead")
        assert row["status"] == suites.STOPPED
        assert row["stop_reason"] == f"Stopped before case 3 of 4: {suites.SET_GONE}"

    async def test_a_dead_set_asked_to_stop_reads_cancelled(self, fake_llm, lease):
        suite = await _setup(case_count=2)
        await _left_running(suite, "esr_dead2", status=suites.CANCELLING)
        assert await suites.running_for(suite["id"]) is None
        assert (await store.get_suite_run("esr_dead2"))["status"] == "cancelled"

    async def test_a_held_set_still_counts(self, fake_llm, lease):
        suite = await _setup(case_count=2)
        await _left_running(suite, "esr_live")
        await lease.set("synapse:run-live:esr_live", "api-other", ex=90)
        assert await suites.running_for(suite["id"]) == "esr_live"
        assert (await store.get_suite_run("esr_live"))["status"] == "running"

    async def test_standalone_a_running_row_still_counts(self, fake_llm):
        """The set runs in this process there; a restart's sweep is what ends a dead one."""
        suite = await _setup(case_count=2)
        await _left_running(suite, "esr_sa")
        assert await suites.running_for(suite["id"]) == "esr_sa"

    async def test_a_running_set_holds_its_lease(self, fake_llm, lease):
        fake_llm.set_default(VERDICT)
        suite = await _setup(case_count=1)
        seen = []

        async def run_case(run_id, session_id, go):
            seen.append(await lease.get("synapse:run-live:esr_held"))
            await go()

        await suites.run_suite(suite, _server(), suite_run_id="esr_held", run_case=run_case)
        assert seen and seen[0] in (b"api-test", "api-test")
        assert await lease.get("synapse:run-live:esr_held") is None


class TestTheSweepsEndASetNothingHolds:
    async def test_at_boot_every_set_left_running_ends(self, fake_llm):
        from core.scale.heartbeat import sweep_interrupted_runs
        suite = await _setup(case_count=3)
        await _left_running(suite, "esr_boot1", scored=0)
        await _left_running(suite, "esr_boot2", status=suites.CANCELLING)

        await sweep_interrupted_runs()

        one = await store.get_suite_run("esr_boot1")
        assert (one["status"], one["stop_reason"]) == (
            suites.STOPPED, f"Stopped before case 1 of 3: {suites.SET_GONE}")
        assert (await store.get_suite_run("esr_boot2"))["status"] == "cancelled"

    async def test_at_boot_a_set_another_server_holds_is_left(self, fake_llm, fake_redis):
        from core.scale.heartbeat import sweep_interrupted_runs
        suite = await _setup(case_count=3)
        await _left_running(suite, "esr_boot3")
        await fake_redis.set("synapse:run-live:esr_boot3", "api-other", ex=90)
        await sweep_interrupted_runs(redis=fake_redis)
        assert (await store.get_suite_run("esr_boot3"))["status"] == "running"

    async def test_the_scale_sweep_ends_a_set_whose_lease_is_gone(self, fake_llm, fake_redis):
        from core.scale.heartbeat import sweep_orphaned_runs
        suite = await _setup(case_count=3)
        await _left_running(suite, "esr_or1")
        await _left_running(suite, "esr_or2")
        await fake_redis.set("synapse:run-live:esr_or2", "api-live", ex=90)

        await sweep_orphaned_runs(fake_redis)

        assert (await store.get_suite_run("esr_or1"))["status"] == suites.STOPPED
        assert (await store.get_suite_run("esr_or2"))["status"] == "running"
