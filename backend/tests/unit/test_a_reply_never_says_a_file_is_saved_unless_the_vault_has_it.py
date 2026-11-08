"""F1211 (BLD1; CAP3's G4): a builder reply never says a file is in the Vault without the Vault saying so.

Asked where a workflow saved its chart, Synapse AI answered "The workflow saved two files in your vault:" with two
paths and their totals, none of them ever written (CAP3 stage 0, cap-04). The reply is a model's own words with no
finish tool to refuse it, so where every run ends (F1093's net) a passage that says a named file was saved is held to
the Vault: when every path it names is there it stands; when one is not, code says what is there, what a saved
workflow writes when it runs, and what it could not find, in place of the passage.
"""
from __future__ import annotations

import pytest

from core.models_orchestration import OrchestrationRun
from core.native_builder import vault_claims
from core.orchestration.state import SharedState

CAP_04 = """The workflow saved two files in your vault:

1. **Chart image** – `cap3/last_week_sales_by_region.png`
   This is a bar chart showing total sales (GBP) for each region.

2. **Summary text file** – `cap3/last_week_sales_summary.txt`
   It contains the numeric totals per region, e.g.:

   ```
   North    12,340.00

   South    9,870.50
   ```

Let me know if you need anything else."""


def _put(path: str, text: str = "x") -> None:
    from core.storage import get_blob_store

    get_blob_store().put(f"vault/{path}", text)


async def _ends(said: str, orchestration_id: str = "orch_native_builder", status: str = "completed") -> str:
    from core.native_builder.change_tools import said_without_secret_asks

    run = OrchestrationRun(run_id="builder_vault_said", orchestration_id=orchestration_id, status=status,
                           shared_state={"summary": said})
    await SharedState(run).checkpoint()
    await said_without_secret_asks(run)
    stored = (await SharedState.restore(run.run_id)).run.shared_state["summary"]
    assert stored == run.shared_state["summary"]
    return stored


class TestWhatIsAClaim:
    def test_cap_04_claims_both_paths_through_its_list_and_its_code(self):
        [(first, last, paths)] = vault_claims.regions(CAP_04)
        assert paths == ["cap3/last_week_sales_by_region.png", "cap3/last_week_sales_summary.txt"]
        assert vault_claims._blocks(CAP_04)[last + 1] == "Let me know if you need anything else."

    @pytest.mark.parametrize("text", [
        "When it runs, it will save the chart to `reports/chart.png`.",
        "Each run writes `reports/weekly.md` to the Vault.",
        "Read `cap3/sales.csv` and total it by region.",
        "Your data is at https://example.com/files/sales.csv and the workflow is saved.",
        "The workflow is saved and runs every Monday.",
    ])
    def test_a_promise_a_read_or_no_path_is_no_claim(self, text):
        assert not vault_claims.claimed(text)

    @pytest.mark.parametrize("text, path", [
        ("I saved the summary to `notes/summary.md`.", "notes/summary.md"),
        ("The report was written to reports/q3/summary.txt for you.", "reports/q3/summary.txt"),
        ("The file @[cap3/out.json] is in your vault.", "cap3/out.json"),
    ])
    def test_a_past_tense_save_with_a_path_is(self, text, path):
        [(_, _, paths)] = vault_claims.regions(text)
        assert paths == [path]


class TestTheReply:
    async def test_cap_04_s_files_that_are_not_there_are_said_so(self, client):
        said = await _ends(CAP_04)
        assert "The workflow saved two files" not in said and "12,340.00" not in said
        assert said.startswith("I checked the Vault:\n- I could not find `cap3/last_week_sales_by_region.png` in the "
                               "Vault: nothing was saved there.\n- I could not find `cap3/last_week_sales_summary.txt`")
        assert said.endswith("Let me know if you need anything else.")

    async def test_files_that_are_there_stand_as_said(self, client):
        _put("cap3/last_week_sales_by_region.png")
        _put("cap3/last_week_sales_summary.txt")
        assert await _ends(CAP_04) == CAP_04

    async def test_one_there_and_one_not_says_each(self, client):
        _put("cap3/last_week_sales_by_region.png")
        said = await _ends(CAP_04)
        assert "- `cap3/last_week_sales_by_region.png` is in the Vault." in said
        assert "- I could not find `cap3/last_week_sales_summary.txt` in the Vault" in said

    async def test_a_file_a_saved_workflow_writes_is_said_as_a_promise(self, client):
        from core.store.resources import save_orchestration

        await save_orchestration({"id": "orch_sales", "name": "Weekly sales", "description": "", "entry_step_id": "s",
                                  "steps": [{"id": "s", "name": "Save", "type": "tool", "forced_tool": "vault_write",
                                             "tool_arguments": {"path": "reports/weekly.md"}}]})
        said = await _ends("Done! I saved the totals to `reports/weekly.md`.")
        assert said == ("I checked the Vault:\n- `reports/weekly.md` is not in the Vault yet: the workflow saves it "
                        "there when it runs.")

    async def test_another_run_s_words_are_never_read(self, client):
        assert await _ends(CAP_04, orchestration_id="orch_someone_elses") == CAP_04
        assert await _ends(CAP_04, status="failed") == CAP_04

    async def test_a_path_that_is_no_vault_path_is_not_found(self, client):
        said = await _ends("I saved it to `../outside/secret.txt`.")
        assert "I could not find `../outside/secret.txt` in the Vault" in said
