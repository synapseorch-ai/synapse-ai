"""The builder's two old run tools name the workflow and say why a run failed (F756, BWA1, D274).

BWA1's run of ASSIST1's personas on dev (2026-10-17, Nova Pro): asked "is the invoice sync still running?", "did the
digest finish?", "why did it fail?", Synapse AI called `list_recent_runs` and `get_run_summary`, and every answer
built from those two alone was wrong (6 of 6). They gave a run id, a workflow id, a status and times: no workflow
name, no error and nothing a step wrote, so the digest's run was reported as Invoice sync's and no failure had a
reason. `read_workspace`'s `runs` / `run` already said all of it. Naveen: the old tools answer from the readers.

Each tool keeps every field it had; what it adds is the readers'.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState

OLD_LIST_FIELDS = {"run_id", "orchestration_id", "status", "current_step_id", "started_at", "ended_at"}
OLD_SUMMARY_FIELDS = {"run_id", "orchestration_id", "status", "current_step_id", "waiting_for_human",
                      "started_at", "ended_at"}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


def _ago(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _call(tool: str, **args):
    raw, _ = await execute_builder_tool_with_events(tool, args, _Server())
    return json.loads(raw)


@pytest.fixture
async def runs():
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    await save_orchestration(make_orchestration(id="orch_digest", name="Nightly digest"))
    await save_orchestration(make_orchestration(id="orch_invoices", name="Invoice sync"))
    failed = OrchestrationRun(
        run_id="run_invoices", orchestration_id="orch_invoices", status="failed", started_at=_ago(days=4),
        ended_at=_ago(days=4), current_step_id="step_fetch",
        step_history=[{"step_id": "step_fetch", "step_name": "Fetch invoices", "status": "failed",
                       "error": "HTTP 401 from 'fetch_invoices': Requires authentication"}])
    done = OrchestrationRun(
        run_id="run_digest", orchestration_id="orch_digest", status="completed", started_at=_ago(hours=3),
        ended_at=_ago(hours=3), shared_state={"digest": "3 visits booked, 1 cancelled"},
        step_history=[{"step_id": "step_print", "step_name": "Print digest", "status": "completed",
                       "output_key": "digest"}])
    for run in (failed, done):
        await SharedState(run).checkpoint()


async def test_recent_runs_name_each_workflow_and_say_why_a_run_failed(runs):
    listed = await _call("list_recent_runs")
    assert isinstance(listed, list)
    by_id = {r["run_id"]: r for r in listed}
    assert OLD_LIST_FIELDS <= set(by_id["run_digest"]), "every field the tool had stays"
    assert by_id["run_digest"]["workflow"] == "Nightly digest"
    assert by_id["run_invoices"]["workflow"] == "Invoice sync"
    assert "401" in by_id["run_invoices"]["error"]
    assert not by_id["run_digest"]["error"]
    assert by_id["run_digest"]["waiting_for_human"] is False


async def test_recent_runs_of_one_workflow_by_its_name(runs):
    listed = await _call("list_recent_runs", name="invoice")
    assert [r["run_id"] for r in listed] == ["run_invoices"]


async def test_a_name_no_workflow_has_says_so(runs):
    got = await _call("list_recent_runs", name="payroll")
    assert "error" in got and "list_orchestrations" in got["error"]


async def test_a_run_summary_is_the_run_step_by_step_with_its_workflow_and_error(runs):
    got = await _call("get_run_summary", run_id="run_invoices")
    assert OLD_SUMMARY_FIELDS <= set(got), "every field the tool had stays"
    assert got["workflow"] == "Invoice sync" and got["status"] == "failed"
    assert "401" in got["error"]
    [step] = got["steps"]
    assert step["step_name"] == "Fetch invoices"

    done = await _call("get_run_summary", run_id="run_digest")
    assert done["workflow"] == "Nightly digest"
    assert "3 visits booked" in json.dumps(done["steps"])


async def test_a_workflows_id_reads_its_latest_run(runs):
    """did-02 passed the workflow's id (`orch_…`) and was told the run "has no checkpoint"."""
    got = await _call("get_run_summary", run_id="orch_invoices")
    assert got["run_id"] == "run_invoices" and got["workflow"] == "Invoice sync"


async def test_a_workflow_that_never_ran_says_so(runs):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    await save_orchestration(make_orchestration(id="orch_new", name="Brand new"))
    got = await _call("get_run_summary", run_id="orch_new")
    assert "error" in got and "Brand new" in got["error"] and "not run" in got["error"]


async def test_read_workspace_names_a_failed_runs_workflow(runs):
    """The `run` reader took a failed run's `error` (its reason) for a refusal and returned before naming the
    workflow, so "why did the invoice sync fail" came back without "Invoice sync"."""
    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "run", "run_id": "run_invoices"},
                                                    _Server())
    got = json.loads(raw)
    assert got["workflow"] == "Invoice sync" and got["started_at"] and "401" in got["error"]
