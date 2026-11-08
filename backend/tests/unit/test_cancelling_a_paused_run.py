"""Cancelling a run that was waiting for a person (F141).

`cancel.cancel_run` wrote `status` and `ended_at` and left everything else as
it stood: `waiting_for_human` still true, `human_prompt` still the question,
and the `paused` entry in `step_history` still open — so its duration went on
accruing against the wall clock for as long as anyone looked at it.

Every reader then believed the run was still asking. The orchestration screen
kept its answer strip up, offering an Answer the API refuses with a 409, and
nothing cleared it: an in-process cancel writes the checkpoint directly while
the engine loop is suspended, so no `orchestration_complete` and no
`orchestration_error` ever reach the page.

The fleet already did this (`worker_engine_adapter._sync_final_state`), and so
does cloud (`api/control/run_cancel.py`, whose `_close_paused_step` this
follows). This is the in-process path catching up.
"""
import pytest

from _fakes import seed as S
from test_human_in_containers import _human, _print, _run, _server, driver, worker  # noqa: F401


async def _paused(driver, name):
    """A run parked on a question. The id carries the driver: `cancel_run`
    remembers cancelled ids in a module-level set for the life of the process,
    so reusing one across the two drivers cancels the second before it starts.
    """
    run_id = f"{name}_{driver.kind}"
    orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="ask", steps=[
        _human("ask", nxt="tail"), _print("tail"),
    ])
    await S.seed_orchestrations([orch])
    assert await driver.start(orch, run_id) == "paused"
    return run_id


class TestACancelledRunIsNotAsking:
    async def test_it_is_no_longer_waiting_for_a_human(self, driver):
        from core.orchestration.cancel import cancel_run

        run_id = await _paused(driver, "run_cancel_wait")
        assert (await _run(run_id)).waiting_for_human is True

        result = await cancel_run(run_id)
        assert result.status == "cancelled"

        run = await _run(run_id)
        assert run.status == "cancelled"
        assert run.waiting_for_human is False

    async def test_its_open_question_is_closed_where_it_stands(self, driver):
        from core.orchestration.cancel import cancel_run

        run_id = await _paused(driver, "run_cancel_entry")
        await cancel_run(run_id)

        run = await _run(run_id)
        entries = [h for h in run.step_history if h.get("step_id") == "ask"]
        assert [h["status"] for h in entries] == ["cancelled"], entries
        assert entries[0].get("ended_at"), "a closed entry has an end"
        assert isinstance(entries[0].get("duration_seconds"), int | float)

    async def test_a_finished_run_is_not_rewritten(self, driver):
        """The guard that was already there, kept: cancelling something that
        completed must not reopen or relabel it."""
        from core.orchestration.cancel import cancel_run

        run_id = f"run_cancel_done_{driver.kind}"
        orch = S.make_orchestration(id=f"orch_{run_id}", entry_step_id="tail",
                                    steps=[_print("tail")])
        await S.seed_orchestrations([orch])
        assert await driver.start(orch, run_id) == "completed"

        result = await cancel_run(run_id)
        assert result.stopped is False
        assert (await _run(run_id)).status == "completed"
