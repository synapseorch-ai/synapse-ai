"""What workflow and run tabs read about the runs they don't stream (TABS1 v2, D319; F930, F1022).

Only the editor in front streams its run. A tab behind learns whether its run
is still going or waiting on a person from one short request for every open
tab's run (`POST /api/orchestrations/runs/status`) — by the runs' own ids, so a
live run is never missed past the newest twenty a list would show.
"""
from __future__ import annotations


async def _run(run_id: str, status: str, *, waiting: bool = False) -> None:
    import core.orchestration.state as state_mod
    from core.models_orchestration import OrchestrationRun

    await state_mod.SharedState(OrchestrationRun(
        run_id=run_id, orchestration_id="orch_tabs", status=status, waiting_for_human=waiting,
    )).checkpoint()


async def _status(client, *run_ids: str) -> dict[str, dict]:
    response = await client.post("/api/orchestrations/runs/status", json={"run_ids": list(run_ids)})
    assert response.status_code == 200, response.text
    return {row["run_id"]: row for row in response.json()["runs"]}


class TestOneReadForEveryTabsRun:
    async def test_says_how_each_run_stands(self, client):
        await _run("run_tab_live", "running")
        await _run("run_tab_asks", "paused", waiting=True)
        await _run("run_tab_done", "completed")

        rows = await _status(client, "run_tab_live", "run_tab_asks", "run_tab_done")

        assert rows["run_tab_live"]["status"] == "running"
        assert rows["run_tab_live"]["waiting_for_human"] is False
        assert rows["run_tab_asks"]["waiting_for_human"] is True
        assert rows["run_tab_done"]["status"] == "completed"
        assert rows["run_tab_live"]["orchestration_id"] == "orch_tabs"

    async def test_a_run_with_no_record_is_left_out(self, client):
        await _run("run_tab_known", "running")
        assert set(await _status(client, "run_tab_known", "run_never")) == {"run_tab_known"}

    async def test_ten_runs_at_most(self, client):
        response = await client.post("/api/orchestrations/runs/status",
                                     json={"run_ids": [f"run_{i}" for i in range(11)]})
        assert response.status_code == 422
