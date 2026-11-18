"""A board is made with its first cards, and a knowledge base with its pages, on one press (F1670).

A change that made a knowledge base for the W-9 form's page put up a card that made an empty base, and a board with
three cards a card that made an empty board: the pages and the cards were ops of their own, aimed at what was not made
yet. Now `create_knowledge_base` takes the pages it is filled from and `create_board` its first cards: the card says
them, and one press makes the base and adds its pages, or the board and its cards. Pages it can't take are refused
before the card; neither is fetched or made while the card is only shown.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import dispatch, seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks

W9 = "https://www.irs.gov/pub/irs-pdf/fw9.pdf"


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
    await SharedState(OrchestrationRun(run_id="builder_parts", orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": None}})).checkpoint()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    dispatch.reset()


async def _prepare(args):
    raw, events = await execute_builder_tool_with_events("prepare_action", args, _Server(), run_id="builder_parts")
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


async def _press(client, card, i=0):
    res = await client.post("/api/assistant/act", json=card["buttons"][i])
    assert res.status_code == 200, res.text
    return res.json()["result"]


@pytest.mark.usefixtures("knowledge_store")
class TestABaseWithItsPages:
    @pytest.fixture
    async def on(self, client, monkeypatch):
        from core.knowledge import web

        assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
        seams.set_ingest_dispatcher(RecordingDispatcher())
        fetched: dict = {}

        async def add_urls(base, body):
            fetched["urls"] = (base.name, body.urls, body.kind)
            return type("Accepted", (), {"model_dump": lambda self, mode=None: {"accepted": len(body.urls)}})()

        monkeypatch.setattr(web, "add_urls", add_urls)
        return client, fetched

    async def test_one_press_makes_it_and_adds_its_pages(self, on):
        from core.knowledge import bases

        client, fetched = on
        _, [card] = await _prepare({"action": "knowledge_create", "name": "IRS Tax Forms", "urls": [W9]})
        assert W9 in card["detail"], card["detail"]
        assert "urls" not in fetched and not [b for b in await bases.list_bases() if b.name == "IRS Tax Forms"]
        await _press(client, card)
        assert [b.name for b in await bases.list_bases()] == ["IRS Tax Forms"]
        assert fetched["urls"] == ("IRS Tax Forms", [W9], "page"), fetched

    async def test_pages_it_cant_take_get_no_card(self, on):
        said, cards = await _prepare({"action": "knowledge_create", "name": "Bad", "urls": ["ftp://example.com/x"]})
        assert cards == [] and "can't be added" in json.dumps(said), said


class TestABoardWithItsCards:
    @pytest.fixture
    async def on(self, client):
        assert (await client.post("/api/settings", json={"boards_enabled": True})).status_code == 200
        return client

    async def test_one_press_makes_it_and_adds_its_cards(self, on):
        from core.store import boards as store

        cards_asked = [{"title": "Book the venue"}, {"title": "Send the invites", "text": "To the whole team"}]
        _, [card] = await _prepare({"action": "board_create", "name": "Launch checklist", "cards": cards_asked})
        assert "Book the venue" in card["detail"] and "Send the invites" in card["detail"], card["detail"]
        assert not [b for b in await store.list_boards() if b.name == "Launch checklist"]
        await _press(on, card)
        [board] = [b for b in await store.list_boards() if b.name == "Launch checklist"]
        titles = [c.title for c in await store.list_cards([board.id])]
        assert sorted(titles) == ["Book the venue", "Send the invites"], titles
