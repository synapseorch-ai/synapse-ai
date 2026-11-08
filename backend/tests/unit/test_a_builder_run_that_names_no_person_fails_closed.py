"""A builder run that names no person fails closed (F1315, REACH1; security, 79 11-03).

Synapse AI acts for the person whose run it is (`builder_access`, ASSIST1/F681): each tool is checked against their
role now. A run of the builder's orchestration that named no person was let through with no check at all, and its
reads and acts fell back to the workspace's owner. These pin the class, closed at its two ends:

- on a host that tells people apart (cloud), inside a builder run that names no person, every builder tool is refused,
  a read as much as a write, and nothing is done; there is no owner to fall back to inside a run;
- self-hosted's own chat, which names its one person as `{"user_id": None}`, still reads and acts as the owner, and so
  does a live run whose person is in its state before its first checkpoint; so does a builder run paused before its
  person was recorded (an older release's), since self-hosted has one person and the run is theirs;
- the builder's orchestration is never started, deployed or nested as an ordinary workflow: the run route, deploy and
  a Workflow step each refuse a system orchestration, and so does a save that nests one.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID
from core.orchestration.state import SharedState
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _print, _run, _server, driver, worker,
)


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


async def _builder_run(run_id: str, state: dict) -> str:
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id=NATIVE_BUILDER_ORCH_ID,
                                       shared_state=state)).checkpoint()
    return run_id


async def _call(tool: str, args: dict, run_id: str | None) -> dict:
    raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run_id)
    return json.loads(raw)


@pytest.fixture
def people():
    """A host that tells people apart: each person's role now (cloud's)."""
    from core.product_tools import hooks
    from core.product_tools.roles import Role

    async def source(user_id):
        return {"u1": Role.OWNER}.get(user_id)

    hooks.set_role_source(source)
    yield
    hooks.set_role_source(None)


@pytest.mark.usefixtures("people")
class TestARunThatNamesNoOne:
    @pytest.mark.parametrize("tool, args", [
        ("read_workspace", {"topic": "agents"}),
        ("list_agents", {}),
        ("prepare_action", {"action": "feature_on", "feature": "boards"}),
    ])
    async def test_every_tool_is_refused(self, client, tool, args):
        run_id = await _builder_run("builder_noone", {})
        got = await _call(tool, args, run_id)
        assert got.get("error") == builder_access.NO_ONE, got

    async def test_a_write_does_nothing(self, client):
        from core.store.resources import load_agents

        run_id = await _builder_run("builder_noone_w", {})
        got = await _call("create_agent", {"name": "Made by no one", "description": "x", "system_prompt": "x"},
                          run_id)
        assert got.get("error") == builder_access.NO_ONE
        assert not [a for a in await load_agents() if a.get("name") == "Made by no one"]


class TestItsOwnPersonStillActs:
    async def test_self_hosted_chat_names_its_one_person(self, client):
        run_id = await _builder_run("builder_selfhosted", {builder_access.CALLER_KEY: {"user_id": None}})
        got = await _call("read_workspace", {"topic": "agents"}, run_id)
        assert "error" not in got, got

    async def test_a_live_run_before_its_first_checkpoint(self, client):
        from core.orchestration.engine import live

        run = OrchestrationRun(run_id="builder_live_only", orchestration_id=NATIVE_BUILDER_ORCH_ID,
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}})
        with live(run):
            got = await _call("read_workspace", {"topic": "agents"}, run.run_id)
        assert "error" not in got, got

    async def test_an_older_run_with_no_person_recorded_is_the_installs_one_person(self, client):
        run_id = await _builder_run("builder_older_release", {})
        got = await _call("read_workspace", {"topic": "agents"}, run_id)
        assert "error" not in got, got

    async def test_a_direct_call_outside_any_run_is_the_installs_owner(self, client):
        got = await _call("read_workspace", {"topic": "agents"}, None)
        assert "error" not in got, got


def _system(id: str) -> dict:
    return S.make_orchestration(id=id, name="Synapse AI", system="native_builder", entry_step_id="c",
                                steps=[_print("c", "ran", key="made")])


class TestNoDoorStartsIt:
    async def test_the_run_route_refuses_a_system_orchestration(self, client):
        await S.seed_orchestrations([_system("orch_sys_run")])
        refused = await client.post("/api/orchestrations/orch_sys_run/run", json={"message": "go"})
        assert refused.status_code == 404, refused.text

    async def test_deploy_refuses_a_system_orchestration(self, client):
        from core.routes.agents import load_user_agents

        await S.seed_orchestrations([_system("orch_sys_deploy")])
        refused = await client.post("/api/orchestrations/orch_sys_deploy/deploy")
        assert refused.status_code == 404, refused.text
        assert not [a for a in await load_user_agents() if a.get("orchestration_id") == "orch_sys_deploy"]

    async def test_a_workflow_step_never_runs_one(self, driver):
        parent = S.make_orchestration(id="orch_nests_sys", entry_step_id="nested", steps=[
            {"id": "nested", "name": "Nested", "type": "workflow", "workflow_id": "orch_sys_child",
             "output_key": "result", "next_step_id": None}])
        await S.seed_orchestrations([parent, _system("orch_sys_child")])
        assert await driver.start(parent, "run_nests_sys") == "failed"
        run = await _run("run_nests_sys")
        assert "made" not in run.shared_state
        [entry] = [h for h in run.step_history if h.get("step_id") == "nested"]
        assert "Synapse AI's own" in entry["error"]

    async def test_a_save_that_nests_one_is_refused(self, client):
        from core.builder_tools import _workflows_called

        await S.seed_orchestrations([_system("orch_sys_saved")])
        orch = {"id": "orch_wants_sys", "steps": [{"id": "n", "type": "workflow", "workflow_id": "orch_sys_saved"}]}
        _, issues = await _workflows_called(orch, {})
        assert any("Synapse AI's own" in i for i in issues), issues
