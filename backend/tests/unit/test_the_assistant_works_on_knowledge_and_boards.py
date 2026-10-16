"""The assistant's cards reach Knowledge and Boards too (ASSIST1, D291, F684; Naveen: "operations + builds and also
knowledge and boards").

What a person does there, through the same card and the same door as every act (F683): add pages or a note to a
knowledge base, retry or delete a document; add a card to a board, close or reopen one, comment, make a shared board,
delete one. Each is a person's write through the person-facing code the screens use, a builder's act as on the
screens, refused in the feature's own words while it is off. A delete is a danger card. A new knowledge base is made on
the Knowledge screen, where its folder and index are chosen.
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
async def assistant_run():
    from core.store import get_store

    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_kb", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


@pytest.fixture
async def boards_on(monkeypatch):
    use_saved_settings(monkeypatch)
    from core.routes.settings import save_settings
    await save_settings({"boards_enabled": True})


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_kb")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


async def test_a_board_is_made_carded_closed_and_commented_on(client, boards_on):
    from core.store import boards as store

    _, [card] = await _prepare({"action": "board_create", "name": "Bugs", "text": "What breaks"})
    assert card["title"] == 'Make a shared board "Bugs"'
    assert [b for b in await store.list_boards() if b.name == "Bugs"] == []  # shown, not done
    await _press(client, card)
    [board] = [b for b in await store.list_boards() if b.name == "Bugs"]

    _, [card] = await _prepare({"action": "board_add_card", "board": "bugs", "title": "Login 500s"})
    added = (await _press(client, card))["card"]
    assert added["title"] == "Login 500s" and added["by_person"] is True and added["board_id"] == board.id

    _, [card] = await _prepare({"action": "board_comment", "card_id": added["id"], "text": "Seen twice today"})
    await _press(client, card)
    assert [c.comment for c in await store.comments(added["id"])] == ["Seen twice today"]

    _, [card] = await _prepare({"action": "board_update_card", "card_id": added["id"], "status": "done",
                                "result": "Fixed in 1.4"})
    assert card["title"] == 'Close as done: "Login 500s"'
    closed = await _press(client, card)
    assert closed["status"] == "done" and closed["result"] == "Fixed in 1.4"

    raw, _ = await execute_builder_tool_with_events("read_workspace", {"topic": "boards", "name": "Bugs"},
                                                    _Server(), run_id="builder_kb")
    read = json.loads(raw)
    assert read["board"] == "Bugs" and [c["title"] for c in read["recently_closed"]] == ["Login 500s"]

    _, [card] = await _prepare({"action": "board_delete", "board": "Bugs"})
    assert card["danger"] is True
    await _press(client, card)
    assert [b for b in await store.list_boards() if b.name == "Bugs"] == []


async def test_boards_off_says_so(monkeypatch):
    use_saved_settings(monkeypatch)
    said, cards = await _prepare({"action": "board_add_card", "board": "Bugs", "title": "x"})
    assert cards == [] and "switched off" in said["error"]


async def test_a_viewer_gets_no_board_card(boards_on):
    async def source(user_id):
        return Role.VIEWER

    hooks.set_role_source(source)
    await SharedState(OrchestrationRun(run_id="builder_v", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": "v1"}})).checkpoint()
    raw, events = await execute_builder_tool_with_events("prepare_action", {"action": "board_create", "name": "X"},
                                                         _Server(), run_id="builder_v")
    assert not [e for e in events if e.get("type") == "action_needed"]
    assert "builder" in json.loads(raw)["error"]


async def test_knowledge_off_says_so(monkeypatch):
    use_saved_settings(monkeypatch)
    said, cards = await _prepare({"action": "knowledge_add_note", "knowledge_base": "FAQ", "text": "Open 9 to 5"})
    assert cards == [] and "switched off" in said["error"]


@pytest.fixture
def faq(monkeypatch):
    """Knowledge on, one active base "FAQ", and its writes recorded (the ingest itself is Knowledge's own suite's)."""
    import types

    from core.knowledge import bases, ingest, web
    from core.product_tools import knowledge

    kb = types.SimpleNamespace(id="kb_" + "1" * 32, name="FAQ", status="active")
    monkeypatch.setattr(knowledge, "_on", lambda: True)

    async def listed():
        return [kb]

    monkeypatch.setattr(bases, "list_bases", listed)
    wrote: dict = {}

    async def add_urls(base, body):
        wrote["urls"] = (base.id, body.urls, body.kind)
        return types.SimpleNamespace(model_dump=lambda mode=None: {"accepted": len(body.urls)})

    async def put_records(base, body):
        wrote["note"] = (base.id, body.records[0].text, body.records[0].title)
        return []

    monkeypatch.setattr(web, "add_urls", add_urls)
    monkeypatch.setattr(ingest, "put_records", put_records)
    return kb, wrote


async def test_pages_and_a_note_are_added_to_a_knowledge_base_on_a_click(client, faq):
    kb, wrote = faq
    _, [card] = await _prepare({"action": "knowledge_add_pages", "knowledge_base": "faq",
                                "urls": ["https://example.com/hours", "https://example.com/prices"]})
    assert card["title"] == 'Add 2 pages to "FAQ"' and "embedding model" in card["detail"]
    assert "urls" not in wrote
    await _press(client, card)
    assert wrote["urls"] == (kb.id, ["https://example.com/hours", "https://example.com/prices"], "page")

    _, [card] = await _prepare({"action": "knowledge_add_note", "knowledge_base": "FAQ", "title": "Hours",
                                "text": "Open 9 to 5, Monday to Friday."})
    await _press(client, card)
    assert wrote["note"] == (kb.id, "Open 9 to 5, Monday to Friday.", "Hours")


async def test_a_knowledge_base_that_is_not_there_is_not_guessed(faq):
    said, cards = await _prepare({"action": "knowledge_add_note", "knowledge_base": "Contracts", "text": "x"})
    assert cards == [] and "Knowledge screen" in said["error"]


def test_the_new_entries_are_product_tools_with_their_roles():
    from core.product_tools import areas, registry

    areas.ensure_loaded()
    for name in ("add_knowledge_pages", "add_knowledge_note", "retry_knowledge_document", "delete_knowledge_document",
                 "add_board_card", "update_board_card", "comment_on_card", "create_board", "delete_board"):
        tool = registry.get(name)
        assert tool is not None and tool.kind == "act" and tool.min_role == Role.BUILDER, name
    assert registry.get("delete_board").irreversible and registry.get("delete_knowledge_document").irreversible
    for name in ("list_boards", "read_board"):
        assert registry.get(name).kind == "read"
