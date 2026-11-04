"""An ordinary run's builder tools act for the run's person (F1319, REACH1; Naveen's B2, eb 11-04).

A workflow's or an agent's own run (not Synapse AI's) reached the builder tools usable anywhere as the workspace's
owner with no person: nothing it did was audited, and on a host that tells people apart a read keyed on a person
answered nothing. The host knows whose a run is (who pressed Run, a key's creator, a schedule's last saver, a chat's
starter); now it says so (`builder_access.set_run_person_source`), and the run acts for that person:

- with their role now, read on every call, and their record (principal); a write needs the builder role, as in
  Synapse AI's own run, and a person no longer in the workspace acts for nothing; the refusal names the person, their
  role and the tool, and a Tool step it refuses fails the run in those words;
- each build write it makes is audited once, as them, through the run (`via="workflow"`); the MCP door, which the
  registry audits, is never audited twice;
- a run with no person acts as before (the workspace, unaudited), and so does every run on a host with no source
  (self-hosted); Synapse AI's own run still reads its person from its own key, never from the source.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.product_tools import caller as current, hooks
from core.product_tools.roles import Role
from tests.unit.test_a_failed_child_fails_its_parent import (  # noqa: F401 — fixtures
    _run, _server, driver, worker,
)


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
def host(monkeypatch):
    """A host that tells people apart and knows whose each run is."""
    from core.product_tools import boards

    state = {"roles": {}, "people": {}, "audited": [], "asked": []}

    async def role_of(user_id):
        return state["roles"].get(user_id)

    async def person_of(run_id):
        state["asked"].append(run_id)
        return state["people"].get(run_id)

    async def principal_of(user_id):
        return {"record_of": user_id}

    async def audit(event):
        bound = current.current()
        state["audited"].append((event, getattr(bound, "principal", None)))

    async def name_of(caller):
        return {"u_dana": "Dana Reyes"}.get(getattr(caller, "user_id", None), "someone")

    hooks.set_role_source(role_of)
    hooks.set_audit(audit)
    builder_access.set_run_person_source(person_of)
    builder_access.set_principal_source(principal_of)
    monkeypatch.setattr(boards, "_host", boards.BoardHost(
        enabled=boards._HOST.enabled, directory=boards._HOST.directory, person_name=name_of, caps=boards._HOST.caps))
    builder_access.forget_runs()
    yield state
    hooks.set_role_source(None)
    hooks.set_audit(None)
    builder_access.set_run_person_source(None)
    builder_access.set_principal_source(None)
    builder_access.forget_runs()


def _plain(run_id: str, state: dict | None = None) -> OrchestrationRun:
    return OrchestrationRun(run_id=run_id, orchestration_id="orch_plain", shared_state=dict(state or {}))


async def _call(tool: str, args: dict, run: OrchestrationRun | None, door: str | None = "step") -> dict:
    if run is None:
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=None, door=door)
        return json.loads(raw)
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id, door=door)
    return json.loads(raw)


@pytest.fixture
def peek(monkeypatch):
    """`read_workspace`, answering with whom the builder acts for."""
    from core import builder_tools

    seen: list = []

    async def handler(args, server_module, events):
        caller = builder_access.acting_caller()
        seen.append(caller)
        return {"ok": True}

    monkeypatch.setitem(builder_tools._REGISTERED, "read_workspace", handler)
    return seen


async def _made(name: str) -> bool:
    from core.store.resources import load_agents

    return any(a.get("name") == name for a in await load_agents())


class TestItActsForTheRunsPerson:
    async def test_a_builders_run_writes_as_them_audited_once(self, client, host):
        host["roles"]["u_dana"], host["people"]["run_dana"] = Role.BUILDER, "u_dana"
        got = await _call("create_agent", {"name": "Dana's helper", "system_prompt": "x"}, _plain("run_dana"))
        assert "error" not in got, got
        [(event, principal)] = host["audited"]
        assert (event.action, event.tool, event.via, event.user_id) == ("agent.created", "create_agent", "workflow",
                                                                         "u_dana")
        assert principal == {"record_of": "u_dana"}, "the host's audit reads the person's own record"

    async def test_its_reads_are_theirs(self, client, host, peek):
        host["roles"]["u_dana"], host["people"]["run_dana_r"] = Role.APPROVER, "u_dana"
        assert "error" not in await _call("read_workspace", {"topic": "agents"}, _plain("run_dana_r"))
        [caller] = peek
        assert (caller.user_id, caller.role, caller.via) == ("u_dana", Role.APPROVER, "workflow")
        assert caller.principal == {"record_of": "u_dana"}
        assert host["audited"] == [], "a read is not an act"

    async def test_a_chat_its_starter_began(self, client, host, peek):
        host["roles"]["u_dana"], host["people"][None] = Role.BUILDER, "u_dana"
        assert "error" not in await _call("read_workspace", {"topic": "agents"}, None, door="agent")
        assert peek[0].user_id == "u_dana"


class TestARefusalSaysWhoAndWhy:
    async def test_a_person_below_builder_writes_nothing(self, client, host, peek):
        host["roles"]["u_dana"], host["people"]["run_viewer"] = Role.VIEWER, "u_dana"
        refused = await _call("create_agent", {"name": "Not by a viewer", "system_prompt": "x"}, _plain("run_viewer"))
        said = refused.get("error") or ""
        assert all(w in said for w in ("Dana Reyes", "Viewer", "create_agent", "Builder")), said
        assert not await _made("Not by a viewer") and host["audited"] == []
        assert "error" not in await _call("read_workspace", {"topic": "agents"}, _plain("run_viewer")), "reads stay"

    async def test_a_person_no_longer_here_acts_for_nothing(self, client, host):
        host["people"]["run_gone"] = "u_dana"  # no role: removed from the workspace
        refused = await _call("create_agent", {"name": "Left behind", "system_prompt": "x"}, _plain("run_gone"))
        said = refused.get("error") or ""
        assert "Dana Reyes" in said and "no longer a member" in said, said
        assert not await _made("Left behind")

    async def test_a_tool_step_it_refuses_fails_the_run_in_those_words(self, host, driver):
        orch = S.make_orchestration(id="orch_viewer_builds", entry_step_id="make", steps=[
            {"id": "make", "name": "Make the helper", "type": "tool", "forced_tool": "create_agent",
             "tool_arguments": {"name": "Helper by a viewer", "system_prompt": "x"}, "output_key": "made",
             "next_step_id": None}])
        await S.seed_orchestrations([orch])
        host["roles"]["u_dana"], host["people"]["run_viewer_builds"] = Role.VIEWER, "u_dana"
        assert await driver.start(orch, "run_viewer_builds") == "failed"
        run = await _run("run_viewer_builds")
        [entry] = [h for h in run.step_history if h.get("step_id") == "make"]
        assert all(w in entry["error"] for w in ("Make the helper", "Dana Reyes", "Viewer", "create_agent")), entry
        assert "made" not in run.shared_state


class TestWithNoPersonItIsAsBefore:
    async def test_a_run_with_no_person(self, client, host, peek):
        assert "error" not in await _call("create_agent", {"name": "Nobody's", "system_prompt": "x"},
                                          _plain("run_nobody"))
        await _call("read_workspace", {"topic": "agents"}, _plain("run_nobody"))
        assert (peek[0].user_id, peek[0].role, peek[0].via) == (None, Role.OWNER, "assistant")
        assert host["audited"] == []

    async def test_a_workflow_that_writes_the_key_still_names_no_one(self, client, host, peek):
        host["roles"]["u_owner"] = Role.OWNER
        await _call("read_workspace", {"topic": "agents"},
                    _plain("run_writes_key", {builder_access.CALLER_KEY: {"user_id": "u_owner"}}))
        assert peek[0].user_id is None

    async def test_synapse_ais_own_run_never_asks_the_source(self, client, host, peek):
        from core.native_builder.seed import NATIVE_BUILDER_ORCH_ID

        host["roles"]["u_dana"] = Role.BUILDER
        own = OrchestrationRun(run_id="builder_dana", orchestration_id=NATIVE_BUILDER_ORCH_ID,
                               shared_state={builder_access.CALLER_KEY: {"user_id": "u_dana"}})
        await _call("read_workspace", {"topic": "agents"}, own)
        assert peek[0].user_id == "u_dana" and peek[0].via == "assistant"
        assert "builder_dana" not in host["asked"]

    async def test_a_host_with_no_source(self, client, peek):
        assert "error" not in await _call("create_agent", {"name": "Self-hosted", "system_prompt": "x"},
                                          _plain("run_selfhosted"))
        await _call("read_workspace", {"topic": "agents"}, _plain("run_selfhosted"))
        assert peek[0].user_id is None


class TestTheMcpDoorIsAuditedOnce:
    async def test_the_registry_audits_it_and_the_run_does_not_again(self, client, host):
        from core.product_tools import areas, registry
        from core.product_tools.caller import Caller

        areas.ensure_loaded()
        host["people"][None] = "u_dana"  # a source that would name someone, which the MCP door never asks
        host["roles"]["u_dana"] = Role.BUILDER
        await registry.invoke("create_agent", Caller(role=Role.BUILDER, via="mcp", user_id="u_mcp"),
                              {"name": "From a client", "system_prompt": "x"})
        assert [(e.user_id, e.via) for e, _ in host["audited"]] == [("u_mcp", "mcp")]
