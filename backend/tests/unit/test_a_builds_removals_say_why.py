"""F1281 (a0; BLD1's F1279 look): every removal a build makes says why.

`finish_build` removes what its run started and never finished: a workflow that never passed validation, an agent its
saver never used. A workflow the validator had never been asked about went out as `resource_saved deleted` with no
reason, so whatever reads the event (the run's summary, its log) had to make up its own. Each of the build's removals
now carries its reason, in the words the summary already said ("it never passed validation"); a delete the person
approved is theirs, and unchanged.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import builder_tools
from core.builder_tools import execute_builder_tool, execute_builder_tool_with_events


class _Server:
    agent_sessions: dict = {}


RUN = "run_f1281"


async def _in_run(tool: str, args: dict) -> dict:
    out, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=RUN)
    return json.loads(out)


async def _finish(monkeypatch, writes: list[dict]) -> list[dict]:
    from core.orchestration import state as state_mod

    async def restore(run_id):
        return SimpleNamespace(run=SimpleNamespace(shared_state={"_run_writes": writes}))

    monkeypatch.setattr(state_mod.SharedState, "restore", staticmethod(restore))
    _, events = await execute_builder_tool_with_events("finish_build", {}, _Server(), run_id=RUN)
    return [e for e in events if e.get("type") == "resource_saved" and e.get("action") == "deleted"]


class TestEveryRemovalABuildMakesSaysWhy:
    async def test_a_workflow_never_checked_a_workflow_that_failed_and_an_agent_each_carry_a_reason(self,
                                                                                                  monkeypatch):
        never = (await _in_run("create_orchestration_skeleton", {"name": "Half", "entry_step_id": "step_a"}))
        failed = (await _in_run("create_orchestration_skeleton", {"name": "Broken", "entry_step_id": "step_1"}))
        await _in_run("add_single_step", {"orch_id": failed["orchestration"]["id"], "step_id": "step_1",
                                          "name": "Step 1", "type": "llm", "prompt_template": "Say hi.",
                                          "output_key": "out_1", "next_step_id": "step_missing"})
        assert not (await _in_run("validate_orchestration", {"orch_id": failed["orchestration"]["id"]}))["ok"]
        made = json.loads(await execute_builder_tool(
            "create_agent", {"name": "Form Fetcher", "description": "d", "type": "conversational", "tools": [],
                             "system_prompt": "s"}, _Server()))
        agent_id = made.get("id") or made["agent"]["id"]

        gone = await _finish(monkeypatch, [
            {"kind": "orchestration", "action": "created", "id": never["orchestration"]["id"], "name": "Half"},
            {"kind": "orchestration", "action": "created", "id": failed["orchestration"]["id"], "name": "Broken"},
            {"kind": "agent", "action": "created", "id": agent_id, "name": "Form Fetcher"}])

        assert {e["name"] for e in gone} == {"Half", "Broken", "Form Fetcher"}
        assert all(str(e.get("reason") or "").strip() for e in gone), gone
        said = {e["name"]: e["reason"] for e in gone}
        assert said["Half"] == "it never passed validation", "the words the summary already said"
        assert said["Broken"].startswith("it never passed validation: ")

    async def test_a_delete_the_person_approved_is_unchanged(self):
        made = json.loads(await execute_builder_tool(
            "create_orchestration_skeleton", {"name": "Old digest", "entry_step_id": "step_a"}, _Server()))
        _, events = await execute_builder_tool_with_events(
            "delete_orchestration", {"orch_id": made["orchestration"]["id"], "confirm": True}, _Server())
        (gone,) = [e for e in events if e.get("type") == "resource_saved"]
        assert gone["action"] == "deleted" and "reason" not in gone


@pytest.fixture(autouse=True)
def _no_run_left():
    yield
    assert builder_tools._current_run.get() is None
