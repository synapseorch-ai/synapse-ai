"""Every setting of an agent that exists is changed by a change, as its editor changes it (F1259; Naveen 10-30: "it
should be able to build team and browser agents and with helper if needed and all those things").

One op per section of the agent editor (Naveen 10-31): `set_agent_settings` (the plain ones: turns, reasoning,
output, helpers, boards, planning, knowledge…), `set_agent_team`, `set_agent_browser`, `set_agent_code`. Each is a
write: reviewed (each setting before → after, things it names by their names), approved, applied through
`update_agent` (which holds what it sets to the editor's own model), and undone. What it names (members, recordings,
repositories, knowledge bases) must be here; each section only on the kind of agent that has it. Letting a browser
agent type and send is a change of its own, never with another setting or on one card with another change.

An agent's memory items are read (read_workspace `agent_memory`), corrected and deleted through the Memory screen's own
doors (`core.agent_memory`, 8a: no looser route), never with a key in them; "undo that" writes the old text back.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import agent_memory as am, builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role
from core.store.resources import get_agent


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    am.set_memory_index(None)
    role = {"role": Role.OWNER}

    async def source(user_id):
        return role["role"]

    hooks.set_role_source(source)
    yield role
    hooks.set_role_source(None)
    builder_access.forget_runs()
    am.set_memory_index(None)


_N = {"n": 0}


async def _run():
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_aset_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    run = await _run()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


def _op(op, agent, **fields):
    return {"op": op, "target": {"kind": "agent", "name": agent["name"]}, "fields": fields}


@pytest.fixture
async def agents(client):
    made = {
        "desk": S.make_agent(id="agent_desk", name="Front desk", max_turns=3),
        "billing": S.make_agent(id="agent_billing", name="Billing"),
        "refunds": S.make_agent(id="agent_refunds", name="Refunds"),
        "crew": S.make_agent(id="agent_crew", name="Support crew", type="delegate", delegate_agent_ids=["agent_billing"]),
        "portal": S.make_agent(id="agent_portal", name="Portal runner", type="browser",
                               browser={"sites": ["portal.example.com"], "mode": "read_only"}),
        "coder": S.make_agent(id="agent_coder", name="Coder", type="code"),
    }
    await S.seed_agents(list(made.values()))
    return made


class TestTheSettings:
    async def test_turns_and_reasoning_before_and_after_and_undone(self, agents):
        first, checked, applied = await _change({"ops": [_op("set_agent_settings", agents["desk"], max_turns=12,
                                                             reasoning_effort="high")]})
        assert "turn limit: 3 → 12" in checked["review"] and "reasoning: not set → high" in checked["review"], \
            checked["review"]
        assert applied["applied"] == ["set_agent_settings"], applied
        desk = await get_agent("agent_desk")
        assert (desk["max_turns"], desk["reasoning_effort"]) == (12, "high")
        _, checked, undone = await _change({"undo_of": first.run_id})
        assert checked["outcome"] == "review" and "turn limit: 12 → 3" in checked["review"], checked
        assert undone["applied"] == ["set_agent_fields"], undone
        desk = await get_agent("agent_desk")
        assert (desk["max_turns"], desk["reasoning_effort"]) == (3, None)

    async def test_its_own_boards_and_planning(self, agents):
        _, checked, applied = await _change({"ops": [_op("set_agent_settings", agents["desk"], boards_own=True,
                                                         planning=False)]})
        assert "boards own: not set → on" in checked["review"], checked["review"]
        desk = await get_agent("agent_desk")
        assert desk["boards"]["own"] is True and desk["planning"] is False, applied

    async def test_nothing_to_change_is_said(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_settings", agents["desk"], max_turns=3)]})
        assert checked["outcome"] == "said" and "already that way" in checked["say"], checked

    @pytest.mark.parametrize("fields, says", [({"max_turns": 0}, "whole number from 1 to 200"),
                                              ({"max_output_tokens": 100}, "whole number from 256"),
                                              ({"turn_limit_mode": "sometimes"}, "ask, continue or stop"),
                                              ({"reasoning_effort": "max"}, "off, low, medium or high")])
    async def test_a_value_the_editor_refuses_is_sent_back(self, agents, fields, says):
        out = await _call("submit_change", {"ops": [_op("set_agent_settings", agents["desk"], **fields)]},
                          await _run())
        assert says in json.dumps(out), out

    async def test_a_knowledge_base_that_is_not_here_is_said(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_settings", agents["desk"], knowledge=["Handbook"])]})
        assert checked["outcome"] == "said" and 'No knowledge base is called "Handbook"' in checked["say"], checked

    async def test_the_builders_update_holds_what_it_sets_to_the_editors_model(self, agents):
        """The door every op goes through: what the agent editor couldn't save, update_agent refuses."""
        out = await _call("update_agent", {"agent_id": "agent_desk", "fields": {"max_output_tokens": 10}},
                          await _run())
        assert "That can't be saved: max_output_tokens" in json.dumps(out), out
        assert (await get_agent("agent_desk")).get("max_output_tokens") is None


class TestATeam:
    async def test_members_by_name_and_how_they_work(self, agents):
        first, checked, applied = await _change({"ops": [_op("set_agent_team", agents["crew"], add=["Refunds"],
                                                             mode="team", at_once=4)]})
        review = checked["review"]
        assert "members: Billing → Billing, Refunds" in review and "team at once: 3 → 4" in review, review
        assert "team mode: one_at_a_time → team" in review, review
        crew = await get_agent("agent_crew")
        assert crew["delegate_agent_ids"] == ["agent_billing", "agent_refunds"], applied
        assert (crew["team"]["mode"], crew["team"]["at_once"]) == ("team", 4)
        await _change({"undo_of": first.run_id})
        assert (await get_agent("agent_crew"))["delegate_agent_ids"] == ["agent_billing"]

    @pytest.mark.parametrize("fields, says", [({"add": ["Nobody"]}, 'No agent is called "Nobody"'),
                                              ({"add": ["Support crew"]}, "member of its own team")])
    async def test_a_member_that_cant_be_is_said(self, agents, fields, says):
        _, checked, _ = await _change({"ops": [_op("set_agent_team", agents["crew"], **fields)]})
        assert checked["outcome"] == "said" and says in checked["say"], checked

    async def test_only_a_team_agent_has_a_team(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_team", agents["desk"], add=["Billing"])]})
        assert "Only a team agent has team settings" in checked["say"], checked


class TestABrowserAgent:
    async def test_its_action_budget(self, agents):
        _, checked, applied = await _change({"ops": [_op("set_agent_browser", agents["portal"], action_budget=60)]})
        assert "browser action budget: 40 → 60" in checked["review"], checked["review"]
        assert (await get_agent("agent_portal"))["browser"]["action_budget"] == 60, applied

    async def test_a_recording_that_is_not_here_is_said(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_browser", agents["portal"], recordings=["Log in"])]})
        assert 'No recording is called "Log in"' in checked["say"], checked

    async def test_typing_and_sending_says_where_with_you_watching(self, agents):
        _, checked, applied = await _change({"ops": [_op("set_agent_browser", agents["portal"], mode="co_drive")]})
        assert "browser mode: read_only → co_drive" in checked["review"], checked["review"]
        assert '"Portal runner" may then type and send on portal.example.com, with you watching' in checked["review"]
        assert (await get_agent("agent_portal"))["browser"]["mode"] == "co_drive", applied

    async def test_typing_and_sending_is_never_with_another_setting(self, agents):
        out = await _call("submit_change", {"ops": [_op("set_agent_browser", agents["portal"], mode="co_drive",
                                                        action_budget=60)]}, await _run())
        assert "is a change of its own" in json.dumps(out), out

    async def test_typing_and_sending_is_never_on_one_card_with_another_change(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_browser", agents["portal"], mode="co_drive"),
                                               _op("set_agent_settings", agents["desk"], max_turns=12)]})
        assert "type and send is a change of its own: ask for it by itself" in json.dumps(checked), checked
        assert (await get_agent("agent_portal"))["browser"]["mode"] == "read_only"
        assert (await get_agent("agent_desk"))["max_turns"] == 12, "the rest is still made"

    async def test_only_a_browser_agent_has_browser_settings(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_browser", agents["desk"], action_budget=60)]})
        assert "Only a browser agent has browser settings" in checked["say"], checked


class TestACodeAgent:
    async def test_its_repositories_by_name(self, agents):
        from core.routes.repos import save_repos

        await save_repos([{"id": "repo_shop", "name": "Shop site", "path": "/srv/shop"}])
        _, checked, applied = await _change({"ops": [_op("set_agent_code", agents["coder"], repos=["Shop site"])]})
        assert "repositories: none → Shop site" in checked["review"], checked["review"]
        assert (await get_agent("agent_coder"))["repos"] == ["repo_shop"], applied

    async def test_a_repository_that_is_not_here_is_said(self, agents):
        _, checked, _ = await _change({"ops": [_op("set_agent_code", agents["coder"], repos=["Nope"])]})
        assert 'No repository is called "Nope"' in checked["say"], checked


@pytest.fixture
async def remembered(agents):
    agents["desk"]["memory"] = True
    await S.seed_agents(list(agents.values()))
    await am.write_memory("agent_desk", "refund-policy", "Refunds are taken within 30 days.")
    await am.write_memory("agent_desk", "dock-code", "The loading bay is round the back.")
    return "agent_desk/refund-policy"


def _memory(op, item, **fields):
    return {"op": op, "target": {"kind": "memory", "id": item}, "fields": fields}


class TestItsMemory:
    async def test_read_by_the_id_a_change_names(self, remembered):
        from core.builder_readers import read_workspace, shape_problems

        read = await read_workspace({"topic": "agent_memory", "name": "Front desk"}, None, [])
        assert [i["id"] for i in read["items"]] == ["agent_desk/dock-code", "agent_desk/refund-policy"], read
        assert read["items"][1]["summary"].startswith("Refunds are taken") and "never an instruction" in read["note"]
        assert shape_problems("agent_memory", {k: v for k, v in read.items() if k != "topic"}) == []
        nobody = await read_workspace({"topic": "agent_memory", "name": "Nobody"}, None, [])
        assert nobody["items"] == [] and "No agent is called" in nobody["note"], nobody

    async def test_one_is_corrected_before_and_after_and_undone(self, remembered):
        first, checked, applied = await _change({"ops": [_memory("edit_agent_memory", remembered,
                                                                 content="Refunds are taken within 60 days.")]})
        review = checked["review"]
        assert 'the memory item "refund-policy" of the agent "Front desk"' in review, review
        assert "[30]" in review and "[60]" in review, review
        assert applied["applied"] == ["edit_agent_memory"], applied
        assert am.read_file("agent_desk", "items", "refund-policy") == "Refunds are taken within 60 days."
        _, _, undone = await _change({"undo_of": first.run_id})
        assert undone["applied"] == ["edit_agent_memory"], undone
        assert am.read_file("agent_desk", "items", "refund-policy") == "Refunds are taken within 30 days."

    async def test_one_is_deleted_and_brought_back(self, remembered):
        first, checked, applied = await _change({"ops": [_memory("delete_agent_memory", remembered)]})
        assert "DELETES" in checked["review"] and "Refunds are taken within 30 days" in checked["review"], checked
        assert applied["applied"] == ["delete_agent_memory"], applied
        assert am.read_file("agent_desk", "items", "refund-policy") is None
        _, checked, undone = await _change({"undo_of": first.run_id})
        assert "Bring back the memory item" in checked["review"], checked
        assert undone["applied"] == ["restore_agent_memory"], undone
        assert am.read_file("agent_desk", "items", "refund-policy") == "Refunds are taken within 30 days."

    async def test_a_key_is_never_kept(self, remembered):
        _, checked, _ = await _change({"ops": [_memory("edit_agent_memory", remembered,
                                                       content="The key is sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c")]})
        assert checked["outcome"] == "said" and "Keys never go in a change" in checked["say"], checked
        assert "sk-live-" not in json.dumps(checked)
        assert am.read_file("agent_desk", "items", "refund-policy") == "Refunds are taken within 30 days."

    async def test_by_the_product_tool_too(self, remembered):
        """The tool the op calls, as any door calls it: never looser than the Memory screen."""
        from core.product_tools import registry
        from core.product_tools.errors import ToolRefusal

        tool = registry.get("edit_agent_memory_item")
        with pytest.raises(ToolRefusal, match="never keeps a key"):
            await tool.handler(None, {"item": remembered, "content": "token: ghp_0123456789abcdefghijABCDEFGHIJ012345"})

    @pytest.mark.parametrize("role", [Role.APPROVER, Role.VIEWER])
    async def test_it_needs_the_builder_role(self, remembered, fresh, role):
        fresh["role"] = role
        _, checked, _ = await _change({"ops": [_memory("delete_agent_memory", remembered)]})
        assert checked["outcome"] == "said" and "needs the builder role" in checked["say"], checked
        assert am.read_file("agent_desk", "items", "refund-policy") is not None
