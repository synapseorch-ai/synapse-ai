"""`update_orchestration` sets only what it declares, and only on the workflow it was given (F784).

It took an undeclared, "legacy" `fields` object and wrote all of it into the workflow it saved. Measured over cloud's
MCP door (BF10, D302): updating one's own workflow with `fields: {"id": <another workflow>, "name": …, "rubric_id":
<not the workspace's>}` overwrote the other workflow, a system or deleted one as well (the visibility check read only
`orch_id`), and set a rubric no save in the app would accept. Its siblings were already guarded:
`set_orchestration_meta` keeps an allowlist, `update_agent` pins the id. Now `fields` still works for what the tool
declares; an echo of the workflow's own id or times is ignored; anything else is refused by name, and the id stays
`orch_id`'s.
"""
from __future__ import annotations

import json
import types

import pytest

from _fakes import seed as S
from core.builder_tools import execute_builder_tool

STEPS = [{"id": "p", "name": "P", "type": "print", "print_content": "x", "next_step_id": None}]


@pytest.fixture
def server():
    return types.SimpleNamespace(agent_sessions={})


async def call(tool: str, args: dict, server) -> dict:
    return json.loads(await execute_builder_tool(tool, args, server))


async def _get(orch_id: str) -> dict | None:
    from core.store.resources import get_orchestration
    return await get_orchestration(orch_id)


@pytest.fixture
async def two():
    await S.seed_orchestrations([
        S.make_orchestration(id="orch_mine", name="Mine", entry_step_id="p", steps=STEPS),
        S.make_orchestration(id="orch_other", name="Other", entry_step_id="p", steps=STEPS),
    ])


class TestAnotherWorkflowIsNeverWritten:
    async def test_an_id_in_fields_is_refused_and_the_other_workflow_is_untouched(self, server, two):
        result = await call("update_orchestration", {"orch_id": "orch_mine", "fields": {
            "id": "orch_other", "name": "Overwritten"}}, server)
        assert "error" in result and "id" in result["error"]
        assert (await _get("orch_other"))["name"] == "Other"
        assert (await _get("orch_mine"))["name"] == "Mine"

    async def test_a_system_workflow_cannot_be_reached_through_fields(self, server, two):
        await S.seed_orchestrations([S.make_orchestration(id="orch_sys", name="System", entry_step_id="p",
                                                          steps=STEPS, system=True)])
        result = await call("update_orchestration", {"orch_id": "orch_mine", "fields": {
            "id": "orch_sys", "name": "Overwritten"}}, server)
        assert "error" in result
        assert (await _get("orch_sys"))["name"] == "System"


class TestOnlyItsOwnFields:
    @pytest.mark.parametrize("key,value", [("rubric_id", "rub_not_this_workspaces"), ("system", True),
                                           ("is_active", False)])
    async def test_an_undeclared_field_is_refused_by_name(self, server, two, key, value):
        result = await call("update_orchestration", {"orch_id": "orch_mine", "fields": {key: value}}, server)
        assert "error" in result and key in result["error"]
        assert key not in (await _get("orch_mine")) or (await _get("orch_mine")).get(key) != value

    async def test_its_declared_fields_still_work_through_fields(self, server, two):
        result = await call("update_orchestration", {"orch_id": "orch_mine", "fields": {
            "name": "Renamed", "description": "Now described", "timeout_minutes": 7}}, server)
        assert result.get("status") == "updated", result
        saved = await _get("orch_mine")
        assert (saved["name"], saved["description"], saved["timeout_minutes"]) == ("Renamed", "Now described", 7)

    async def test_an_echo_of_its_own_id_and_times_is_ignored(self, server, two):
        mine = await _get("orch_mine")
        result = await call("update_orchestration", {"orch_id": "orch_mine", "fields": {
            "id": "orch_mine", "created_at": mine.get("created_at"), "updated_at": "2020-01-01T00:00:00Z",
            "name": "Echoed"}}, server)
        assert result.get("status") == "updated", result
        saved = await _get("orch_mine")
        assert saved["id"] == "orch_mine" and saved["name"] == "Echoed"
        assert saved["updated_at"] != "2020-01-01T00:00:00Z"

    async def test_steps_through_fields_still_meet_the_step_guards(self, server, two):
        steps = [{"id": "t", "name": "T", "type": "transform", "transform_code": "result = 2", "next_step_id": None}]
        result = await call("update_orchestration", {"orch_id": "orch_mine", "entry_step_id": "t",
                                                     "fields": {"steps": steps}}, server)
        # The transform guard decides as it did (on OSS, code execution is the install's own setting); the point is
        # that `steps` through `fields` is still a declared field and still reaches it.
        assert "error" not in result or "transform" in result["error"].lower(), result
