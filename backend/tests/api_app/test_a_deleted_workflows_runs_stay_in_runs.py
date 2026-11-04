"""A deleted workflow's runs stay in Runs, named as their page names them (F1337, REACH1; REL3's 1.9 → 2.0 upgrade).

The runs list left out every run whose workflow had since been deleted ("the UI cannot open them"). 2.0's run page opens
such a run from the definition it kept (F443), titled "Deleted workflow", so after an upgrade a person's history from
1.9 silently left Runs while each of its pages still opened. These pin the class:

- the runs list keeps a deleted workflow's runs, and each one's page opens;
- Synapse AI's own runs and nested runs are left out as before (the store's own rule, by their ids);
- the screens name such a run in one set of words, from one constant, as its page does: a row whose workflow is not
  in the workspace's list is "Deleted workflow", never its workflow's bare id.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState

FRONTEND = Path(__file__).resolve().parents[3] / "frontend/src"


async def _ran(run_id: str, orchestration_id: str) -> None:
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id=orchestration_id, status="completed",
                                       shared_state={})).checkpoint()


class TestTheListKeepsThem:
    async def test_a_deleted_workflows_run_is_listed_and_opens(self, client):
        await _ran("run_of_a_gone_workflow", "orch_gone_since")
        listed = {r["run_id"] for r in (await client.get("/api/orchestrations/runs", params={"limit": 200})).json()}
        assert "run_of_a_gone_workflow" in listed
        opened = await client.get("/api/orchestrations/runs/run_of_a_gone_workflow")
        assert opened.status_code == 200, opened.text

    async def test_synapse_ais_and_nested_runs_stay_out(self, client):
        await _ran("builder_a_conversation", "orch_native_builder")
        await _ran("run_parent__child_1", "orch_gone_since")
        listed = {r["run_id"] for r in (await client.get("/api/orchestrations/runs", params={"limit": 200})).json()}
        assert not {"builder_a_conversation", "run_parent__child_1"} & listed


class TestTheScreensNameThemAsTheirPageDoes:
    def test_a_row_whose_workflow_is_gone_is_named_deleted_workflow(self):
        host = (FRONTEND / "lib/runHost.tsx").read_text()
        row = host[host.index("export function ossRunRow"):]
        row = row[:row.index("\n}\n")]
        assert "?? run.orchestration_id" not in row.split("workflowName:", 1)[1].split("\n", 1)[0], "never its bare id"

    def test_the_words_are_one_constant(self):
        model = (FRONTEND / "components/run/model.ts").read_text()
        assert "export const DELETED_WORKFLOW = 'Deleted workflow';" in model
        spelled = [str(p.relative_to(FRONTEND)) for p in FRONTEND.rglob("*.ts*")
                   if re.search(r"['\"`]Deleted workflow['\"`]", p.read_text())]
        assert spelled == ["components/run/model.ts"], spelled
