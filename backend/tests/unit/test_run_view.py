"""The one run list v1 and MCP both read (D125, D126).

It is the workspace's runs only: the AI builder's own orchestrations and
orchestrations since deleted are not the workspace's to open, from any surface.
"""
from __future__ import annotations

import pytest


async def _checkpoint(run_id: str, orch_id: str, status: str) -> None:
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState

    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id=orch_id, status=status)).checkpoint()


@pytest.fixture
async def workspace():
    from _fakes import seed

    # One call: seeding replaces the whole list.
    await seed.seed_orchestrations([
        seed.make_orchestration(id="orch_mine", name="Mine"),
        seed.make_orchestration(id="orch_other", name="Other"),
        seed.make_orchestration(id="orch_builderish", name="Builder", system=True),
        seed.make_orchestration(id="orch_retired", name="Retired", is_active=False),
    ])
    for i in range(5):
        await _checkpoint(f"run_orch_mine_{i}", "orch_mine", "completed")
    for i in range(2):
        await _checkpoint(f"run_orch_mine_f{i}", "orch_mine", "failed")
    await _checkpoint("run_orch_other_1", "orch_other", "paused")
    await _checkpoint("run_orch_builderish_1", "orch_builderish", "completed")
    await _checkpoint("run_orch_retired_1", "orch_retired", "completed")
    await _checkpoint("run_orch_gone_1", "orch_gone", "completed")



class TestTheWorkspacesRunsOnly:
    async def test_builder_retired_and_deleted_orchestrations_are_left_out(self, workspace):
        from core.orchestration.run_view import list_runs

        runs = await list_runs(limit=200)
        assert {r["orchestration_id"] for r in runs} == {"orch_mine", "orch_other"}
        assert len(runs) == 8

    async def test_the_items_are_the_app_lists_shape(self, workspace):
        from core.orchestration.run_view import list_runs

        run = (await list_runs(limit=1))[0]
        assert set(run) >= {
            "run_id", "orchestration_id", "status", "started_at", "ended_at", "session_id",
            "current_step_id", "steps_completed", "last_step_name", "waiting_for_human",
            "total_cost_usd",
        }


class TestFilters:
    async def test_a_status_fills_its_page_before_the_limit(self, workspace):
        """Five newer completed runs must not crowd the failed ones out of a page of two."""
        from core.orchestration.run_view import list_runs

        runs = await list_runs(limit=2, status="failed")
        assert [r["status"] for r in runs] == ["failed", "failed"]

    async def test_one_orchestration(self, workspace):
        from core.orchestration.run_view import list_runs

        runs = await list_runs(orchestration_id="orch_other")
        assert [r["run_id"] for r in runs] == ["run_orch_other_1"]

    async def test_a_hidden_orchestration_by_id_is_still_hidden(self, workspace):
        from core.orchestration.run_view import list_runs

        assert await list_runs(orchestration_id="orch_builderish") == []

    @pytest.mark.parametrize("asked, got", [(0, 1), (999, 8)])
    async def test_the_limit_is_clamped(self, workspace, asked, got):
        from core.orchestration.run_view import list_runs

        assert len(await list_runs(limit=asked)) == got
