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

from api_app.test_mcp_server import _body, mcp_app, mcp_session  # noqa: F401  mcp_app is a fixture
# The scheduled approval's own fixtures (F268), to answer one over MCP.
from unit.test_a_scheduled_approval_asks_through_the_bell import executed, manager, schedule  # noqa: F401


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
        await seed_orchestration(id="orch_brief", name="Client brief", offer_to_mcp=True)
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
        assert audited == []

    async def test_a_builder_tools_refusal_reads_in_its_own_words_once(self, mcp_app, api_key):
        """The builder's {"error": ...} came back wrapped again in a second {"error": ...} (found by ASSIST1)."""
        async with mcp_session(mcp_app, api_key) as s:
            res = await s.call_tool("get_agent", {"agent_id": "agent_nope"})
        assert res.isError
        error = json.loads(res.content[0].text)["error"]
        assert isinstance(error, str) and not error.lstrip().startswith("{") and "agent_nope" in error



class TestF696WhatWaitsAndItsAnswers:
    def test_a_viewer_sees_what_waits_and_an_approver_may_answer_it(self):
        assert "list_waiting" in _names(_as(Role.VIEWER))
        names = _names(_as(Role.APPROVER))
        assert {"answer_run", "answer_tool_approval"} <= names and "chat_with_agent" not in names

    async def test_the_bell_s_open_questions_name_the_tool_that_answers_each(self, mcp_app, api_key):
        from core.notifications import hub

        hub.publish("human_input", "MCP2 probe run needs your input", body="Which client?", run_id="run_mcp2_q",
                    data={"step_id": "ask"})
        hub.publish("tool_approval", "MCP2 probe schedule needs approval", body="send_email",
                    data={"schedule_id": "sched_mcp2", "approval_id": "appr_mcp2", "tool_name": "send_email"})
        done = hub.publish("human_input", "MCP2 probe answered", run_id="run_mcp2_done")
        hub._resolve([done], "answered")
        hub.publish("run_completed", "MCP2 probe completed", run_id="run_mcp2_q")

        async with mcp_session(mcp_app, api_key) as s:
            listed = _body(await s.call_tool("list_waiting", {}))
        mine = [i for i in listed["items"] if str(i.get("title", "")).startswith("MCP2 probe")]
        assert [i["kind"] for i in mine] == ["tool_approval", "run_question"]  # newest first
        assert {k: mine[0].get(k) for k in ("schedule_id", "approval_id", "answer_with")} == {
            "schedule_id": "sched_mcp2", "approval_id": "appr_mcp2", "answer_with": "answer_tool_approval"}
        assert mine[1]["run_id"] == "run_mcp2_q" and mine[1]["answer_with"] == "answer_run"

    async def test_a_chat_tool_call_is_answered_over_mcp_once(self, mcp_app, api_key, monkeypatch):
        """F696: chat_with_agent stopped on an approval used to say "approve it in the app"."""
        import core.react_engine as re
        from _fakes import engine_events as E
        from _fakes import seed as S
        from tests.api_v1.test_v1_cli_surface import APPROVAL

        await S.seed_agents([S.make_agent(id="agent_mail", name="Mailer")])
        monkeypatch.setattr(re, "run_react_loop",
                            E.gen_from([E.status(), {**APPROVAL, "approval_id": "appr-mcp2"}]))
        async with mcp_session(mcp_app, api_key) as s:
            asked = _body(await s.call_tool("chat_with_agent", {"message": "email them", "agent": "agent_mail",
                                                                "wait_seconds": 10}))
            assert asked["status"] == "awaiting_approval", asked
            assert "answer_tool_approval" in asked["note"]

            async def _loop(request, *args, **kwargs):
                assert kwargs.get("resume_state") is not None
                yield {"type": "final", "response": "sent it"}

            monkeypatch.setattr(re, "run_react_loop", _loop)
            answered = _body(await s.call_tool("answer_tool_approval", {
                "approve": True, "session_id": asked["session_id"], "approval_id": "appr-mcp2",
                "wait_seconds": 10}))
            assert answered.get("status") == "completed" and answered.get("response") == "sent it", answered
            again = await s.call_tool("answer_tool_approval", {
                "approve": True, "session_id": asked["session_id"], "approval_id": "appr-mcp2"})
        assert again.isError and "not waiting" in again.content[0].text

    async def test_an_answer_must_say_yes_or_no(self, test_app):
        with pytest.raises(ToolRefusal, match="approve: true"):
            await server._call("answer_tool_approval", {"session_id": "s"}, caller=_as(Role.APPROVER))

    async def test_a_host_without_the_answer_says_where_it_is_given(self, test_app, monkeypatch):
        from core.mcp_server import runs

        class Old:  # an embedder's backend from before MCP2
            pass

        monkeypatch.setattr(runs, "_backend", Old())
        with pytest.raises(ToolRefusal, match="in the Synapse app"):
            await server._call("answer_tool_approval", {"approve": True, "session_id": "s"},
                               caller=_as(Role.APPROVER))

    async def test_a_question_the_run_does_not_ask_is_refused_by_name(self, mcp_app, api_key, seed_orchestration):
        from api_app.test_mcp_server import _ASK_THEN_THINK, _paused_run

        orch = await seed_orchestration(entry_step_id="ask", steps=_ASK_THEN_THINK)
        async with mcp_session(mcp_app, api_key) as s:
            run = await _paused_run(s, orch["id"])
            res = await s.call_tool("answer_run", {"run_id": run["run_id"], "answer": "Acme",
                                                   "question_id": "q_nobody_asked"})
        assert res.isError and "not asking question 'q_nobody_asked'" in res.content[0].text


class TestF696AScheduledRunsCall:
    """The bell's Approve for a scheduled run's parked call, over MCP: the call runs once, in the same run."""

    async def test_listed_then_approved_once(self, test_app, fake_llm):
        from unit.test_a_scheduled_approval_asks_through_the_bell import SCHED, _fire, _settled

        manager, executed = self._manager, self._executed
        await _fire(manager, fake_llm)
        waiting = await server._call("list_waiting", {"kind": "tool_approval"}, caller=_as(Role.VIEWER))
        [item] = [i for i in json.loads(waiting.content[0].text)["items"] if i.get("schedule_id") == SCHED]
        assert item["answer_with"] == "answer_tool_approval" and item["tool_name"] == "danger"

        fake_llm.script(["Deleted yesterday's rows."])
        answered = await server._call("answer_tool_approval", {
            "approve": True, "schedule_id": SCHED, "approval_id": item["approval_id"], "wait_seconds": 10,
        }, caller=_as(Role.APPROVER))
        assert not answered.isError, answered.content[0].text
        await _settled()
        assert executed == [("danger", {"rows": "yesterday"})]

        with pytest.raises(ToolRefusal, match="not waiting"):
            await server._call("answer_tool_approval", {
                "approve": True, "schedule_id": SCHED, "approval_id": item["approval_id"],
            }, caller=_as(Role.APPROVER))
        assert executed == [("danger", {"rows": "yesterday"})]

    @pytest.fixture(autouse=True)
    def _armed(self, schedule, manager, executed):
        self._manager, self._executed = manager, executed


class TestF466AWorkflowIsOfferedByItsSwitch:
    """One tool per live workflow filled every client's context (acme: ~109 tools). Naveen (D292): a per-workflow
    "Offer to MCP clients" switch, off by default; every workflow still runs by id or name."""

    async def test_only_an_offered_workflow_is_a_tool_of_its_own(self, mcp_app, api_key):
        from _fakes import seed as S

        await S.seed_orchestrations([S.make_orchestration(id="orch_on", name="Client brief", offer_to_mcp=True),
                                     S.make_orchestration(id="orch_off", name="Weekly digest")])
        async with mcp_session(mcp_app, api_key) as s:
            names = {t.name for t in (await s.list_tools()).tools}
        assert "run_client_brief" in names and "run_weekly_digest" not in names

    async def test_any_workflow_runs_by_its_name(self, mcp_app, api_key, seed_orchestration):
        await seed_orchestration(id="orch_off", name="Weekly digest")
        async with mcp_session(mcp_app, api_key) as s:
            run = _body(await s.call_tool("run_orchestration", {"orchestration_id": "weekly DIGEST",
                                                               "message": "go", "wait_seconds": 10}))
        assert run["status"] == "completed", run

    async def test_a_name_two_workflows_share_asks_for_the_id(self, mcp_app, api_key):
        from _fakes import seed as S

        await S.seed_orchestrations([S.make_orchestration(id="orch_a", name="Digest"),
                                     S.make_orchestration(id="orch_b", name="digest")])
        async with mcp_session(mcp_app, api_key) as s:
            res = await s.call_tool("run_orchestration", {"orchestration_id": "Digest", "message": "go"})
        assert res.isError and "2 workflows are named" in res.content[0].text
        assert "orch_a" in res.content[0].text and "orch_b" in res.content[0].text

    def test_the_switch_is_off_unless_saved_on(self):
        from core.models_orchestration import Orchestration

        assert Orchestration(id="o", name="n").offer_to_mcp is False
        assert Orchestration(id="o", name="n", offer_to_mcp=True).offer_to_mcp is True


class TestADeleteIsPreviewedThenConfirmed:
    """Deletes over MCP (Naveen, D292): the first call says what goes, or what still uses it; the second, with the
    preview's token, deletes and is audited."""

    async def test_previewed_then_deleted_and_audited(self, mcp_app, api_key, seed_agent, audited):
        from core.store.resources import get_agent

        await seed_agent(id="agent_old", name="Old helper")
        async with mcp_session(mcp_app, api_key) as s:
            first = _body(await s.call_tool("delete_agent", {"agent_id": "agent_old"}))
            assert first["status"] == "needs_confirmation"
            assert first["preview"]["will_delete"] == {"type": "agent", "id": "agent_old", "name": "Old helper"}
            assert await get_agent("agent_old") is not None and audited == []
            done = await s.call_tool("delete_agent", {"agent_id": "agent_old",
                                                      "confirm_token": first["confirm_token"]})
        assert not done.isError, done.content[0].text
        assert await get_agent("agent_old") is None
        assert [(e.action, e.target_id) for e in audited] == [("agent.deleted", "agent_old")]

    async def test_a_token_for_one_agent_deletes_no_other(self, mcp_app, api_key, seed_agent):
        from _fakes import seed as S
        from core.store.resources import get_agent

        await S.seed_agents([S.make_agent(id="agent_a", name="A"), S.make_agent(id="agent_b", name="B")])
        async with mcp_session(mcp_app, api_key) as s:
            token = _body(await s.call_tool("delete_agent", {"agent_id": "agent_a"}))["confirm_token"]
            res = await s.call_tool("delete_agent", {"agent_id": "agent_b", "confirm_token": token})
        assert res.isError and "different call" in res.content[0].text
        assert await get_agent("agent_a") is not None and await get_agent("agent_b") is not None

    async def test_one_in_use_is_refused_at_the_preview_in_the_apps_words(self, mcp_app, api_key):
        from _fakes import seed as S

        await S.seed_agents([S.make_agent(id="agent_used", name="Used")])
        await S.seed_orchestrations([S.make_orchestration(
            id="orch_uses", name="Uses it", entry_step_id="a",
            steps=[{"id": "a", "name": "A", "type": "agent", "agent_id": "agent_used", "output_key": "o",
                    "next_step_id": None}])])
        async with mcp_session(mcp_app, api_key) as s:
            res = await s.call_tool("delete_agent", {"agent_id": "agent_used"})
        assert res.isError and "Uses it" in res.content[0].text and "confirm_token" not in res.content[0].text

    async def test_a_workflow_is_deleted_the_same_way(self, mcp_app, api_key, seed_orchestration):
        from core.builder_tools import _visible_orchestration

        await seed_orchestration(id="orch_bye", name="Bye")
        async with mcp_session(mcp_app, api_key) as s:
            token = _body(await s.call_tool("delete_orchestration", {"orch_id": "orch_bye"}))["confirm_token"]
            done = await s.call_tool("delete_orchestration", {"orch_id": "orch_bye", "confirm_token": token})
        assert not done.isError, done.content[0].text
        assert await _visible_orchestration("orch_bye") is None

    def test_an_approver_is_offered_no_delete(self):
        assert not {n for n in _names(_as(Role.APPROVER)) if n.startswith("delete_")}

    def test_no_delete_tells_a_client_to_pass_confirm_true(self):
        for tool in server._fixed_tools(_as(Role.BUILDER)):
            if tool.name.startswith("delete_"):
                assert "confirm=true" not in tool.description, tool.name



def test_an_embedder_gives_the_endpoint_its_own_caller(monkeypatch):
    """Cloud hands the endpoint the person's live role (MCP2d): `caller_of` reaches the server it builds."""
    from core.mcp_server import endpoint as endpoint_mod

    built = {}
    monkeypatch.setattr(endpoint_mod, "build_server", lambda *a, **kw: built.update(kw) or object())

    def caller_of(principal):
        return _as(Role.VIEWER)

    endpoint_mod.McpEndpoint(caller_of=caller_of)
    assert built["caller_of"] is caller_of
    endpoint_mod.McpEndpoint()
    assert built["caller_of"] is server.caller_from
