"""A paused nest names the question it waits on (F288).

Only the step that asked recorded its question (`HumanStepExecutor` sets
`human_prompt` on its own run), so a root waiting on a question two levels down
recorded none, and v1's read of the paused root named no prompt and no fields.
A tool approval was the same. The fleet copied the question onto the root; the
in-process path did not. Now the pause records the question it waits on, and an
answer clears it — a question already answered is not left for a later read.
"""
from tests.unit.test_a_root_reads_running_while_its_child_works import _three_levels
from tests.unit.test_a_failed_child_fails_its_parent import driver, worker  # noqa: F401 — fixtures


class TestTheRootNamesTheQuestionItWaitsOn:
    """The root recorded no question for a nested pause (only the child's own
    human step did), so v1's read of a paused nest named none."""

    async def test_v1_reads_the_nested_question(self, driver):
        from core.orchestration.run_view import read_run
        l1 = await _three_levels("orch_f250q", [
            {"id": "ask", "name": "Confirm", "type": "human", "human_prompt": "Facts ok?",
             "human_fields": [{"name": "ok", "type": "boolean", "label": "OK?"}],
             "output_key": "approval", "next_step_id": "again"},
            {"id": "again", "name": "Again", "type": "human", "human_prompt": "Ship it?",
             "output_key": "ship", "next_step_id": None},
        ], "ask")
        assert await driver.start(l1, "run_f250q") == "paused"

        first = (await read_run("run_f250q"))["human_input_required"]
        assert first["prompt"] == "Facts ok?"
        assert first["fields"] == [{"name": "ok", "type": "boolean", "label": "OK?"}]

        assert await driver.answer("run_f250q", {"ok": True}) == "paused"
        assert (await read_run("run_f250q"))["human_input_required"]["prompt"] == "Ship it?"
