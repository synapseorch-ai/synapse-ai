"""Synapse AI renames and describes a shared board, says who may use it, and changes a card's words, each on a card
(F1268, REACH2; D340's reach row "boards: rename, describe, change access, edit a card's text"; manager 79, 11-03).

Each is the Boards screen's own service, a builder's, as on its routes (self-hosted's and cloud's PATCH and access
routes ask the same): `update_board` for the name and what it is for, `give_board_access` for one entry of the access
list (`none` takes one off, and an add or a work grant is as before), `update_board_card` for a card's words, written
over the version the card read, so one changed since is said. A workflow's or a schedule's own board is its owner's.
The access list names agents, workflows and schedules only: with none on it the board is still the people's.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_env import use_saved_settings
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def assistant_run(monkeypatch):
    from core.routes.settings import save_settings
    from core.store import get_store

    await get_store()
    use_saved_settings(monkeypatch)
    await save_settings({"boards_enabled": True})
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await _assistant("builder_boards")
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _assistant(run_id, user_id=None):
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": user_id}})).checkpoint()


async def _prepare(args, run_id="builder_boards"):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id=run_id)
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


async def _shared(name="Bugs", description="What breaks"):
    from core.store import boards as store

    return await store.create_shared_board(name, description=description, max_shared=0)


async def test_a_shared_board_is_renamed_and_described_on_a_card(client):
    from core.store import boards as store

    board = await _shared()
    _, [card] = await _prepare({"action": "board_rename", "board": "bugs", "name": "Defects"})
    assert card["rows"] == [{"label": "Name", "before": "Bugs", "after": "Defects"}]
    assert (await store.get_board(board.id)).name == "Bugs"  # shown, not done
    await _press(client, card)
    assert (await store.get_board(board.id)).name == "Defects"

    _, [card] = await _prepare({"action": "board_describe", "board": "Defects", "text": "Bugs found in testing"})
    assert card["rows"] == [{"label": "What it is for", "before": "What breaks", "after": "Bugs found in testing"}]
    await _press(client, card)
    assert (await store.get_board(board.id)).description == "Bugs found in testing"


async def test_a_name_another_board_has_and_a_workflow_s_own_board_are_refused(client):
    from core.store import boards as store

    await _shared("Bugs")
    await _shared("Ideas", "")
    said, cards = await _prepare({"action": "board_rename", "board": "Bugs", "name": "ideas"})
    assert cards == [] and 'called "Ideas" already' in said["error"]

    own = await store.get_or_create_scoped_board("workflow", "orch_digest")
    said, cards = await _prepare({"action": "board_rename", "board": own.id, "name": "Mine"})
    assert cards == [] and "own board" in said["error"]


async def test_who_may_use_a_board_changes_one_entry_and_keeps_the_rest(client, seed_agent, seed_orchestration):
    from core.store import boards as store

    board = await _shared()
    agent = await seed_agent(name="Triage")
    flow = await seed_orchestration(name="Nightly digest")
    await store.set_access(board.id, [("agent", agent["id"], "add"), ("workflow", flow["id"], "work")])

    _, [card] = await _prepare({"action": "board_access", "board": "Bugs", "who": "Triage", "access": "work"})
    assert card["rows"] == [{"label": 'The agent "Triage"', "before": "may add cards", "after": "may work its cards"}]
    await _press(client, card)
    got = {(a.principal_kind, a.principal_id): a.access for a in await store.list_access(board.id)}
    assert got == {("agent", agent["id"]): "work", ("workflow", flow["id"]): "work"}

    _, [card] = await _prepare({"action": "board_access", "board": "Bugs", "who": "Nightly digest", "access": "none"})
    assert card["rows"][0]["after"] == "not on its list" and "people of this workspace keep" in card["detail"]
    await _press(client, card)
    assert [(a.principal_kind, a.principal_id) for a in await store.list_access(board.id)] == [("agent", agent["id"])]


async def test_with_no_one_on_its_list_the_board_is_still_the_people_s(client, seed_agent):
    """79's note: taking the last entry off never orphans a board; its list is for agents, workflows and schedules."""
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller
    from core.store import boards as store

    board = await _shared()
    agent = await seed_agent(name="Triage")
    await store.set_access(board.id, [("agent", agent["id"], "add")])
    _, [card] = await _prepare({"action": "board_access", "board": "Bugs", "who": "Triage", "access": "none"})
    await _press(client, card)
    assert await store.list_access(board.id) == []
    areas.ensure_loaded()
    person = Caller(role=Role.OWNER, via="assistant")
    listed = await registry.invoke("list_boards", person, {})
    assert "Bugs" in json.dumps(listed)
    added = await registry.invoke("add_board_card", person, {"board": board.id, "title": "Still mine"})
    assert added["card"]["title"] == "Still mine"


async def test_an_add_or_a_work_grant_is_as_before(seed_agent):
    """`none` is new; the existing grant is unchanged: one entry set, the rest kept, the same answer."""
    from core.product_tools import areas, registry
    from core.product_tools.caller import Caller

    board = await _shared()
    agent = await seed_agent(name="Triage")
    areas.ensure_loaded()
    out = await registry.invoke("give_board_access", Caller(role=Role.BUILDER, via="mcp"),
                                {"board": board.id, "kind": "agent", "id": agent["id"], "access": "add"})
    assert set(out) == {"board", "id", "access", "was"} and out["was"] is None
    assert [(a["kind"], a["access"]) for a in out["access"]] == [("agent", "add")]


async def test_a_card_s_words_change_over_the_version_it_read(client):
    from core.boards import service
    from core.store import boards as store

    board = await _shared()
    card_row = (await service.add_card(board.id, title="Login 500s", body="Seen twice", by=service.person("Ana"),
                                       max_open=0))["card"]
    _, [card] = await _prepare({"action": "board_card_text", "card_id": card_row["id"], "title": "Login fails",
                                "text": "Seen twice today"})
    assert card["rows"] == [{"label": "Title", "before": "Login 500s", "after": "Login fails"},
                            {"label": "Text", "before": "Seen twice", "after": "Seen twice today"}]
    await _press(client, card)
    after = await store.get_card(card_row["id"])
    assert (after.title, after.body) == ("Login fails", "Seen twice today")

    _, [card] = await _prepare({"action": "board_card_text", "card_id": card_row["id"], "text": "Fixed in 1.4"})
    await service.update_card(card_row["id"], version=after.version, by=service.person("Ana"), title="Changed")
    res = await client.post("/api/assistant/act", json=card["buttons"][0])
    assert res.status_code != 200  # changed since it was read: said, never written over
    assert (await store.get_card(card_row["id"])).body == "Seen twice today"


async def test_one_below_a_builder_gets_no_card(seed_agent):
    """79's note: each act's role is the Boards screen's route's, a builder's; below it, no card."""
    async def viewer(user_id):
        return Role.VIEWER

    hooks.set_role_source(viewer)
    await _shared()
    await _assistant("builder_boards_viewer", user_id="v1")
    builder_access.forget_runs()
    for args in ({"action": "board_rename", "board": "Bugs", "name": "Defects"},
                 {"action": "board_describe", "board": "Bugs", "text": "x"}):
        said, cards = await _prepare(args, run_id="builder_boards_viewer")
        assert cards == [] and "builder" in said["error"], args


def test_the_new_tool_is_a_builder_s_and_the_app_s_own_and_the_census_says_it():
    from core import feature_census
    from core.native_builder import change_ops as C
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    tool = registry.get("update_board")
    assert tool.app_only and tool.min_role == Role.BUILDER and tool.kind == "act"
    assert registry.get("give_board_access").min_role == Role.BUILDER
    assert "update_board" in next(s for s in feature_census.CENSUS if s.id == "boards").tools
    assert any("`access` is add, work or none" in f
               for f in C.issues("board_access", {"kind": "board", "name": "Bugs"}, {"who": "x", "access": "all"}))
