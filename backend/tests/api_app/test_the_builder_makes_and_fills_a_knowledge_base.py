"""F1200 (BLD1; CAP3's F-c): Synapse AI makes a knowledge base and adds a file the person has, as the Knowledge
screen does, only while Knowledge is on.

It could list bases and add pages or a note to one that existed; a new base was "made on the Knowledge screen", and a
file only by an upload there. Now:

- `create_knowledge_base` makes one through the screen's own path (`bases.make_base`: settings checked, the folder
  made, what it holds queued), on `knowledge/<its name>` unless a folder is named; a name another base has, or a folder
  that overlaps another base's, is refused before any card (its preview checks it);
- `add_knowledge_file` copies a Vault file into a base by an upload's rules (`ingest.check_file`: a type it reads, the
  size per file), never over a file there, and queues it;
- both are cards the person presses (`prepare_action`), both a builder's, both absent while Knowledge is off.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import write_vault
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import dispatch, seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role
from core.storage import get_blob_store

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def assistant_run():
    from core.store import get_store

    await get_store()
    dispatch.reset()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    await SharedState(OrchestrationRun(run_id="builder_kb", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    dispatch.reset()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return client


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_kb")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


async def _bases():
    from core.knowledge import bases
    return {b.name: b for b in await bases.list_bases()}


class TestMakingOne:
    async def test_a_card_then_the_screens_own_base(self, on):
        said, [card] = await _prepare({"action": "knowledge_create", "name": "Price list",
                                       "text": "This month's prices"})
        assert card["title"] == 'Make a knowledge base "Price list"' and "knowledge/price-list" in card["detail"]
        assert "Price list" not in await _bases()  # shown, not done
        result = await _press(on, card)
        kb = (await _bases())["Price list"]
        assert kb.folder == "knowledge/price-list" and kb.status == "active" and kb.description == "This month's prices"
        assert result["folder"] == "knowledge/price-list"

    async def test_a_named_folder_is_used(self, on):
        _, [card] = await _prepare({"action": "knowledge_create", "name": "Contracts", "folder": "legal/contracts"})
        await _press(on, card)
        assert (await _bases())["Contracts"].folder == "legal/contracts"

    async def test_a_taken_name_or_an_overlapping_folder_gets_no_card(self, on):
        _, [card] = await _prepare({"action": "knowledge_create", "name": "Contracts", "folder": "legal"})
        await _press(on, card)
        taken, cards = await _prepare({"action": "knowledge_create", "name": "contracts"})
        assert cards == [] and 'already called "contracts"' in taken["error"]
        overlap, cards = await _prepare({"action": "knowledge_create", "name": "NDAs", "folder": "legal/ndas"})
        assert cards == [] and "overlaps the knowledge base 'Contracts'" in overlap["error"]


class TestAddingAFile:
    async def _base(self, client, name="Price list"):
        _, [card] = await _prepare({"action": "knowledge_create", "name": name})
        await _press(client, card)
        return (await _bases())[name]

    async def test_a_vault_file_is_copied_in_and_queued(self, on):
        kb = await self._base(on)
        write_vault("reports/prices.csv", "item,price\nTea,3\nCake,4\n")
        _, [card] = await _prepare({"action": "knowledge_add_file", "knowledge_base": "Price list",
                                    "path": "reports/prices.csv"})
        assert card["title"] == 'Add "prices.csv" to "Price list"'
        assert get_blob_store().get("vault/knowledge/price-list/prices.csv") is None  # shown, not done
        result = await _press(on, card)
        assert get_blob_store().get("vault/knowledge/price-list/prices.csv") == "item,price\nTea,3\nCake,4\n"
        assert get_blob_store().get("vault/reports/prices.csv") is not None, "the person's file stays where it was"
        assert [d["path"] for d in result["documents"]] == ["knowledge/price-list/prices.csv"]
        assert result["knowledge_base"] == kb.name

    async def test_one_already_in_its_folder_is_queued_where_it_is(self, on):
        await self._base(on)
        write_vault("knowledge/price-list/old.md", "# Old prices")
        _, [card] = await _prepare({"action": "knowledge_add_file", "knowledge_base": "Price list",
                                    "path": "knowledge/price-list/old.md"})
        result = await _press(on, card)
        assert [d["path"] for d in result["documents"]] == ["knowledge/price-list/old.md"]

    @pytest.mark.parametrize("path, data, words", [
        ("bin/tool.exe", b"MZ\x90\x00", "is not a type a knowledge base takes"),
        ("reports/missing.csv", None, "There is no file at 'reports/missing.csv' in the Vault."),
    ])
    async def test_what_an_upload_refuses_gets_no_card(self, on, path, data, words):
        await self._base(on)
        if data is not None:
            get_blob_store().put_bytes(f"vault/{path}", data)
        said, cards = await _prepare({"action": "knowledge_add_file", "knowledge_base": "Price list", "path": path})
        assert cards == [] and words in said["error"]

    async def test_too_big_is_the_uploads_limit(self, on):
        await self._base(on)
        assert (await on.post("/api/settings", json={"knowledge_max_file_mb": 1})).status_code == 200
        write_vault("reports/big.csv", "x" * 1_250_000)
        said, cards = await _prepare({"action": "knowledge_add_file", "knowledge_base": "Price list",
                                      "path": "reports/big.csv"})
        assert cards == [] and "the limit is 1 MB per file" in said["error"]

    async def test_never_over_a_file_the_base_holds(self, on):
        await self._base(on)
        write_vault("knowledge/price-list/prices.csv", "theirs")
        write_vault("reports/prices.csv", "mine")
        said, cards = await _prepare({"action": "knowledge_add_file", "knowledge_base": "Price list",
                                      "path": "reports/prices.csv"})
        assert cards == [] and "already in this knowledge base" in said["error"]
        assert get_blob_store().get("vault/knowledge/price-list/prices.csv") == "theirs"


class TestWhenAndWho:
    async def test_knowledge_off_says_so(self, client):
        for args in ({"action": "knowledge_create", "name": "Price list"},
                     {"action": "knowledge_add_file", "knowledge_base": "Price list", "path": "a.csv"}):
            said, cards = await _prepare(args)
            assert cards == [] and "switched off" in said["error"], args

    def test_they_are_a_builders_acts(self):
        from core.product_tools import areas, registry

        areas.ensure_loaded()
        for name in ("create_knowledge_base", "add_knowledge_file"):
            tool = registry.get(name)
            assert tool.kind == "act" and tool.min_role == Role.BUILDER and tool.scope == "build", name
            assert tool.offered is not None and tool.preview is not None, name

    async def test_the_route_still_makes_one_the_same_way(self, on):
        resp = await on.post("/api/knowledge/bases", json={"name": "Handbook", "folder": "handbook"})
        assert resp.status_code == 201, resp.text
        assert (await _bases())["Handbook"].folder == "handbook"
        again = await on.post("/api/knowledge/bases", json={"name": "Other", "folder": "handbook/inner"})
        assert again.status_code == 422 and "overlaps" in again.json()["detail"]


class TestAskedInWords:
    """The change route (one table of ops, `change_ops`): "make a knowledge base for our prices" is an act whose
    target is what it makes, never looked up among the bases there are (`MAKES_TARGET`), so it is a card, not "no
    knowledge base is called…"; "add prices.csv to it" is a card on the base it names."""

    async def test_make_then_add_are_cards(self, on):
        from unit.test_the_change_route_decides_in_code import _check

        out, [card], _ = await _check([{"op": "knowledge_create", "target": {"kind": "knowledge_base",
                                                                             "name": "Price list"},
                                        "fields": {"text": "This month's prices"}}])
        assert out["outcome"] == "said" and card["title"] == 'Make a knowledge base "Price list"'
        assert "Price list" not in await _bases()  # a card, not done
        _, [made] = await _prepare({"action": "knowledge_create", "name": "Price list"})  # pressed as this person
        await _press(on, made)
        write_vault("reports/prices.csv", "item,price\nTea,3\n")
        out, [card], _ = await _check([{"op": "knowledge_add_file", "target": {"kind": "knowledge_base",
                                                                               "name": "price list"},
                                        "fields": {"path": "reports/prices.csv"}}])
        assert card["title"] == 'Add "prices.csv" to "Price list"'
