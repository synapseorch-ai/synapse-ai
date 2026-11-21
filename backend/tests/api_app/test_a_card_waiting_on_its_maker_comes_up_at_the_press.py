"""A card that waits on what another card makes comes up as soon as that card is pressed (F1670, A).

Asked for a knowledge base with a note in it, Synapse AI put up "Make a knowledge base" and said the note would come
once the base was made: "press Make it on its card, then ask me for it again". The person pressed, and had to ask
again. Now an op aimed at what the same change makes, and that is a card of its own, is kept on the card that makes it,
on the turn's own record and never in the browser; when that card is pressed and works, its waiting cards are
prepared there and then, for the person who pressed, and come back with the press to be drawn under it.

Near misses: a change that is not a card of its own (a knowledge base's settings) keeps its line to ask again; a card
with nothing waiting brings nothing; a press that is refused brings nothing; a waiting card that can't be prepared now
is said in words.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access, builder_actions
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import dispatch, seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks

RUN = "builder_f1670a"
BASE = "IRS Tax Forms"
NOTE = "Due each year by January 31."

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def on(client):
    from core.store import get_store

    await get_store()
    dispatch.reset()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    yield client
    hooks.set_role_source(None)
    builder_access.forget_runs()
    dispatch.reset()


async def _check(ops) -> tuple[dict, list[dict]]:
    run = OrchestrationRun(run_id=RUN, orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None},
                                         "user_message": f"Make a knowledge base {BASE} with a note: {NOTE}"})
    await SharedState(run).checkpoint()
    with live(run):
        raw, events = await execute_builder_tool_with_events(
            "check_change", {"draft": {"finish": "submit_change", "ops": ops}}, _Server(), run_id=RUN)
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


def _base_with_note():
    return [{"op": "knowledge_create", "target": {"name": BASE}, "fields": {"text": "IRS forms"}},
            {"op": "knowledge_add_note", "target": {"name": BASE}, "fields": {"title": "Deadline", "text": NOTE}}]


async def _press(client, button) -> tuple[int, dict]:
    res = await client.post("/api/assistant/act", json={**button, "run_id": RUN})
    return res.status_code, res.json()


class TestTheChange:
    async def test_the_waiting_card_is_said_as_coming_up_at_the_press(self, on):
        out, cards = await _check(_base_with_note())
        assert [c["title"] for c in cards] == [f'Make a knowledge base "{BASE}"'], [c.get("title") for c in cards]
        said = json.dumps(out, ensure_ascii=False)
        assert "comes up as its own card" in said and "ask me for it again" not in said, said

    async def test_a_change_that_is_no_card_keeps_its_line_to_ask_again(self, on):
        ops = [_base_with_note()[0], {"op": "knowledge_settings", "target": {"name": BASE},
                                      "fields": {"description": "Every IRS form we file"}}]
        out, _ = await _check(ops)
        assert "ask me for it again" in json.dumps(out, ensure_ascii=False)


class TestThePress:
    async def test_pressing_make_it_brings_the_note_s_card_which_adds_the_note(self, on):
        _, [card] = await _check(_base_with_note())
        status, body = await _press(on, card["buttons"][0])
        assert status == 200, body
        [nxt] = body.get("cards") or [None]
        assert nxt is not None and BASE in nxt["title"] and "note" in nxt["title"].lower(), body
        assert nxt["buttons"][0]["token"] and nxt["buttons"][0]["arguments"], nxt
        status, done = await _press(on, nxt["buttons"][0])
        assert status == 200 and done.get("cards") == [], done

    async def test_a_refused_press_brings_nothing(self, on):
        _, [card] = await _check(_base_with_note())
        assert (await _press(on, card["buttons"][0]))[0] == 200
        status, again = await _press(on, card["buttons"][0])
        assert status == 409 and "cards" not in again, again

    async def test_a_waiting_card_that_can_t_be_prepared_now_is_said(self, on, monkeypatch):
        _, [card] = await _check(_base_with_note())

        async def refuse(action, args):
            raise builder_actions._NotOffered("Knowledge is busy indexing; ask again in a minute.")

        monkeypatch.setattr(builder_actions, "_offer", refuse)
        status, body = await _press(on, card["buttons"][0])
        assert status == 200 and body.get("cards") == [], body
        assert any("Knowledge is busy indexing" in s for s in body.get("said") or []), body
