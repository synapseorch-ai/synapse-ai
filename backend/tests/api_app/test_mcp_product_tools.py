"""The MCP server is an adapter over the product tools (MCP2, D292).

What a connection is offered and may call follows the caller's role and scopes, from the one registry; a build
act is told to the host's audit (F693), and the builder tools nothing had held back are served (F694).
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from core.mcp_server import server
from core.product_tools import hooks
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role

from api_app.test_mcp_server import mcp_app, mcp_session  # noqa: F401  mcp_app is a fixture


def _names(caller: Caller) -> set[str]:
    return {t.name for t in server._fixed_tools(caller)}


def _as(role: Role, scopes=None) -> Caller:
    return Caller(role=role, via="mcp", user_id="u1", scopes=scopes)


class TestWhatEachRoleIsOffered:
    def test_a_viewer_reads_and_does_nothing(self):
        names = _names(_as(Role.VIEWER))
        assert {"list_agents", "get_orchestration", "get_run", "list_runs"} <= names
        assert not names & {"create_agent", "run_orchestration", "answer_run", "cancel_run", "chat_with_agent"}

    def test_an_approver_may_also_answer(self):
        names = _names(_as(Role.APPROVER))
        assert "answer_run" in names
        assert not names & {"create_agent", "run_orchestration", "cancel_run"}

    def test_a_builder_builds_and_runs(self):
        names = _names(_as(Role.BUILDER))
        assert {"create_agent", "update_orchestration", "run_orchestration", "answer_run", "cancel_run"} <= names

    def test_scopes_narrow_the_role(self):
        names = _names(_as(Role.OWNER, scopes=frozenset({"read"})))
        assert "list_agents" in names and "create_agent" not in names and "answer_run" not in names

    def test_f694_deploy_and_update_schedule_are_served(self):
        assert {"deploy_orchestration", "update_schedule"} <= _names(_as(Role.BUILDER))

    def test_a_workflows_own_tool_is_a_builders(self):
        assert not server._offers_workflows(_as(Role.APPROVER))
        assert not server._offers_workflows(_as(Role.OWNER, scopes=frozenset({"read"})))
        assert server._offers_workflows(_as(Role.BUILDER))


class TestTheCallerOfAPrincipal:
    def test_a_self_hosted_key_is_the_owner(self):
        caller = server.caller_from({"id": "key_1", "name": "laptop"})
        assert (caller.role, caller.credential_id, caller.scopes, caller.user_id) == (Role.OWNER, "key_1", None,
                                                                                      None)

    def test_an_embedders_principal_says_its_role_scopes_and_person(self):
        person = uuid.uuid4()
        principal = SimpleNamespace(role=SimpleNamespace(value="approver"), user_id=person,
                                    credential_id="grant:g1", scopes=frozenset({"read", "run"}))
        caller = server.caller_from(principal)
        assert caller.role is Role.APPROVER
        assert caller.user_id == str(person) and caller.credential_id == "grant:g1"
        assert caller.scopes == frozenset({"read", "run"})
        assert caller.principal is principal


class TestARefusalByRole:
    async def test_a_viewer_cannot_create(self, test_app):
        with pytest.raises(ToolRefusal, match="builder role.*viewer"):
            await server._call("create_agent", {"name": "x"}, caller=_as(Role.VIEWER))

    async def test_an_approver_cannot_run_a_workflow_by_its_own_tool(self, test_app, seed_orchestration):
        await seed_orchestration(id="orch_brief", name="Client brief")
        with pytest.raises(ToolRefusal, match="builder role.*approver"):
            await server._call("run_client_brief", {"message": "go"}, caller=_as(Role.APPROVER))


@pytest.fixture
def audited():
    events: list[hooks.AuditEvent] = []

    async def record(event):
        events.append(event)

    hooks.set_audit(record)
    yield events
    hooks.set_audit(None)


class TestF693ABuildIsAudited:
    async def test_a_create_over_mcp_is_told_to_the_audit_once_and_a_read_never(self, mcp_app, api_key, audited):
        async with mcp_session(mcp_app, api_key) as s:
            made = await s.call_tool("create_agent", {
                "name": "Audit probe", "description": "probe", "type": "conversational", "tools": [],
                "system_prompt": "You help.",
            })
            assert not made.isError, made.content[0].text
            await s.call_tool("list_agents", {})
        assert [(e.action, e.tool, e.via) for e in audited] == [("agent.created", "create_agent", "mcp")]
        assert audited[0].target_type == "agent" and audited[0].target_id
        assert audited[0].credential_id == api_key["record"]["id"]

    async def test_a_refused_build_is_not_audited(self, mcp_app, api_key, audited):
        async with mcp_session(mcp_app, api_key) as s:
            res = await s.call_tool("update_agent", {"agent_id": "agent_nope", "fields": {"name": "x"}})
        assert res.isError
        assert "error" in json.loads(res.content[0].text) or "not found" in res.content[0].text.lower()
        assert audited == []

