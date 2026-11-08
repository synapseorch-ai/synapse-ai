"""The board the person asks for is the board the build's agents post to (F1214, CAP3; Naveen 10-29: in #4).

A workflow's agents posted to its own board: no product tool gave a workflow a shared board's access. CAP3's stage 0
(10-29) asked for "a chart on a board": the build's poster posted to the workflow's own board, nameless and
unmentioned, while a later turn's card made a shared "Sales Analysis" board nothing would ever post to.

Now the board a plan names is a shared board (made with the build, or the one of that name already here), and the
build gives its workflow that board to work through `give_board_access`: the board's access list, as the Boards
screen's editor saves it, through the same service, with the same rules and the same role. The build's new agents
turn their own board off, so their cards go there. A card never makes a second shared board of a name already here.
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from _fakes.knowledge_env import use_saved_settings
from core.boards.resolve import turn_boards
from core.product_tools import areas, registry
from core.product_tools.caller import Caller
from core.product_tools.errors import ToolRefusal
from core.product_tools.roles import Role
from core.store import boards as store

from tests.unit.test_a_build_is_saved_in_code import (  # noqa: F401 — the build's fixtures and helpers
    CHECK, TELL, WRITER, _call, _plan, _reviewed, _workspace, _yes, workspace)


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings

    await save_settings({"boards_enabled": True})


@pytest.fixture
async def flow():
    from core.store.resources import save_orchestration

    orch = S.make_orchestration(id="orch_sales", name="Weekly sales", entry_step_id="end",
                                steps=[{"id": "end", "name": "End", "type": "end"}])
    await save_orchestration(orch)
    return orch["id"]


async def _give(role: Role, **args):
    areas.ensure_loaded()
    return await registry.invoke("give_board_access", Caller(role=role, via="mcp"), args)


# ── the product tool: the Boards screen's access editor, as a door ──────────────────────────────────────────────
def test_it_is_audited_under_the_screen_s_own_name():
    """The same act, the same audit name, from the Boards screen or from the tool (cloud's PUT /boards/{id}/access
    records `board.access_set`; its audit filters read that name). D3ASM's twin audit, 10-29."""
    areas.ensure_loaded()
    assert registry.get("give_board_access").audit_action == "board.access_set"


class TestGiveBoardAccess:
    async def test_a_builder_gives_a_workflow_a_shared_board_and_the_list_keeps_the_rest(self, boards_on, flow):
        board = await store.create_shared_board("Sales Analysis")
        await store.set_access(board.id, [("agent", "agent_desk", "add")])
        out = await _give(Role.BUILDER, board="Sales Analysis", kind="workflow", id=flow, access="work")
        assert {(a["kind"], a["id"], a["access"]) for a in out["access"]} >= {("workflow", flow, "work")}
        assert {(a.principal_kind, a.principal_id, a.access) for a in await store.list_access(board.id)} == {
            ("agent", "agent_desk", "add"), ("workflow", flow, "work")}

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.APPROVER])
    async def test_below_a_builder_is_refused_as_the_screen_refuses_a_write(self, boards_on, flow, role):
        await store.create_shared_board("Sales Analysis")
        with pytest.raises(ToolRefusal):
            await _give(role, board="Sales Analysis", kind="workflow", id=flow, access="work")

    async def test_a_workflow_not_in_this_workspace_is_refused(self, boards_on):
        await store.create_shared_board("Sales Analysis")
        with pytest.raises(ToolRefusal, match="no workflow"):
            await _give(Role.BUILDER, board="Sales Analysis", kind="workflow", id="orch_elsewhere", access="work")

    @pytest.mark.parametrize("access", ["admin", "own", ""])
    async def test_only_what_the_screen_offers(self, boards_on, flow, access):
        await store.create_shared_board("Sales Analysis")
        with pytest.raises(ToolRefusal):
            await _give(Role.BUILDER, board="Sales Analysis", kind="workflow", id=flow, access=access)

    async def test_a_workflows_own_board_has_no_access_list(self, boards_on, flow):
        own = await store.get_or_create_scoped_board("workflow", flow)
        with pytest.raises(ToolRefusal):
            await _give(Role.BUILDER, board=own.id, kind="workflow", id=flow, access="work")

    async def test_while_boards_are_off_it_says_where_they_are_turned_on(self, monkeypatch, flow):
        use_saved_settings(monkeypatch)
        with pytest.raises(ToolRefusal, match="Settings"):
            await _give(Role.BUILDER, board="Sales Analysis", kind="workflow", id=flow, access="work")


# ── where the cards land in a run ────────────────────────────────────────────────────────────────────────────────
async def test_an_agent_with_its_own_board_off_posts_to_the_board_its_workflow_was_given(boards_on, flow):
    board = await store.create_shared_board("Sales Analysis")
    await store.set_access(board.id, [("workflow", flow, "work")])
    turn = await turn_boards(agent={"boards": {"own": False}}, agent_id="agent_poster", source="orchestration",
                             run_id="run_1", session_id="s1", board_scope={"kind": "workflow", "id": flow},
                             settings={"boards_enabled": True}, tools_override=None, persist_turn=True, visitor=None)
    assert [b.name for b in turn.boards] == ["Sales Analysis"]
    assert turn.pick(None).board_id == board.id, "an unnamed card goes to the board the person asked for"


# ── the builder's agents take the editor's Boards switch ─────────────────────────────────────────────────────────
async def test_create_agent_takes_the_boards_switch_and_refuses_what_the_editor_would(client):
    from core.routes.agents import load_user_agents

    run, _ = await _reviewed(_plan(CHECK))
    made, _ = await _call("create_agent", {"name": "Poster", "description": "Posts", "type": "conversational",
                                           "tools": [], "system_prompt": "Post.", "boards": {"own": False}}, run)
    assert made.get("status") == "created", made
    assert {a["name"]: a for a in await load_user_agents()}["Poster"]["boards"] == {"own": False}
    refused, _ = await _call("create_agent", {"name": "Poster 2", "description": "Posts", "type": "conversational",
                                              "tools": [], "system_prompt": "Post.", "boards": {"own": "no"}},
                             run)
    assert "'boards' must be" in refused["error"]


# ── the build ────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_the_board_a_plan_names_is_made_shared_and_given_to_its_workflow(client, boards_on):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER], boards=[{"name": "Sales Analysis"}]),
                                   prompts={"Warning writer": "You write short, calm delay warnings."})
    assert 'Its cards go on the shared board "Sales Analysis"' in checked["review"], "the card says where they go"
    await _yes(run)
    out, _ = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    agents, flows, _ = await _workspace()
    flow_id = flows["Rotterdam wind warning"]["id"]
    [board] = [b for b in await store.list_boards("shared") if b.name == "Sales Analysis"]
    assert [(a.principal_kind, a.principal_id, a.access) for a in await store.list_access(board.id)] == [
        ("workflow", flow_id, "work")]
    assert agents["Warning writer"]["boards"] == {"own": False}, "its new agent posts there, not to its own"


async def test_a_shared_board_already_here_is_given_never_made_again(client, boards_on):
    """F866's case, now doable: "post it on the Shipment delays board" gives the workflow that board."""
    await store.create_shared_board("Shipment delays")
    post = {**TELL, "does": "Posts the warning on the Shipment delays board"}
    run, checked = await _reviewed(_plan(CHECK, post, agents=[WRITER]),
                                   prompts={"Warning writer": "You write short, calm delay warnings."})
    assert checked["plan"]["boards"] == [{"name": "Shipment delays", "purpose": "", "existing": True}]
    await _yes(run)
    out, _ = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    _, flows, _ = await _workspace()
    shared = [b for b in await store.list_boards("shared") if b.name == "Shipment delays"]
    assert len(shared) == 1, "never made twice"
    assert [a.principal_id for a in await store.list_access(shared[0].id)] == [flows["Rotterdam wind warning"]["id"]]


async def test_a_save_that_stops_takes_back_only_the_access_it_gave(client, boards_on, monkeypatch):
    """8a's review: a stopped save removes the entry it added, never one that was there before."""
    from core.product_tools import registry

    board = await store.create_shared_board("Shipment delays")
    await store.set_access(board.id, [("agent", "agent_desk", "add")])
    real = registry.invoke

    async def refuses_schedules(name, *a, **k):
        if name == "create_schedule":
            raise ToolRefusal("Your plan has no room for another schedule.")
        return await real(name, *a, **k)

    monkeypatch.setattr(registry, "invoke", refuses_schedules)
    post = {**TELL, "does": "Posts the warning on the Shipment delays board"}
    run, _ = await _reviewed(_plan(CHECK, post, agents=[WRITER], schedule={"cron": "0 8 * * *", "says": "daily at 8"}),
                             prompts={"Warning writer": "You write short, calm delay warnings."})
    await _yes(run)
    out, _ = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped", out
    assert [(a.principal_kind, a.principal_id, a.access) for a in await store.list_access(board.id)] == [
        ("agent", "agent_desk", "add")]
    assert [b.name for b in await store.list_boards("shared")] == ["Shipment delays"], "a board already here stays"


async def test_an_entry_the_save_replaced_comes_back_as_it_was(boards_on, flow):
    import json

    from core.native_builder.build_save import _undo

    board = await store.create_shared_board("Shipment delays")
    await store.set_access(board.id, [("agent", "agent_desk", "add"), ("workflow", flow, "add")])
    out = await _give(Role.BUILDER, board="Shipment delays", kind="workflow", id=flow, access="work")
    assert out["was"] == "add"
    await _undo([("board_access", json.dumps({"board": board.id, "kind": "workflow", "id": flow, "was": "add"}),
                  "Shipment delays")], {}, [], "a test")
    assert {(a.principal_kind, a.principal_id, a.access) for a in await store.list_access(board.id)} == {
        ("agent", "agent_desk", "add"), ("workflow", flow, "add")}


async def test_a_plan_names_one_board_at_most(client, boards_on):
    run, _ = await _reviewed(_plan(CHECK))
    sent, _ = await _call("submit_plan", _plan(CHECK, boards=[{"name": "A"}, {"name": "B"}]), run)
    assert "a build has one board" in sent["error"]


# ── a later turn ─────────────────────────────────────────────────────────────────────────────────────────────────
async def test_no_card_makes_a_second_shared_board_of_a_name(client, boards_on):
    from core import builder_actions

    await store.create_shared_board("Sales Analysis")
    with pytest.raises(builder_actions._NotOffered, match="already"):
        await builder_actions._board("board_create", {"name": "sales analysis", "text": "Weekly totals"})
    card = await builder_actions._board("board_create", {"name": "Leads", "text": "New leads"})
    assert "Leads" in str(card)
