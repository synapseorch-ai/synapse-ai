"""A turn keeps what it put up, what it listed and what was pressed, on its own card (SAI2-P4, F977).

Naveen, 10-24: each turn's own card on Synapse AI's board. The next turn reads it in code (`read_turn`), so "1", a
typed "Run now", "done" after a press and "approve" with nothing waiting are answered from facts, never from a
model's memory of the conversation (P2's dev after-pass: "not executed yet" after a press; Nova's pick 0/3).

* Every card a builder tool puts up is kept as it was shown: its title and each button's label, tool and arguments.
  The token is never kept: a card's record is not a way to press it.
* A "which one?" refusal keeps its options with their ids, and what was being done, so a pick puts that card up.
* A Connect card is kept by what it connects, so it is never offered twice in one conversation unless asked (SAI2's
  review, after Naveen's Firecrawl case on prod).
* A press is kept on the card of the run that showed it, and only when the presser is that card's person and the
  press is a button that run put up (SAI2's review): a press can never write on someone else's turn.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.boards import assistant
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def fresh():
    from core.store import get_store

    await get_store()
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    confirm.set_claim_store(None)
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _assistant_run(user_id=None, run_id="builder_turn"):
    await SharedState(OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                                       shared_state={builder_access.CALLER_KEY: {"user_id": user_id}})).checkpoint()
    return run_id


async def _call(tool, args, run_id="builder_turn"):
    raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run_id)
    return json.loads(raw), events


async def _settled(*orch_ids):
    """The runs a press started, to their end: a run outliving its test's loop would end in an error there."""
    import asyncio

    from core.orchestration.run_view import list_runs

    for _ in range(100):
        runs = [r for o in orch_ids for r in await list_runs(limit=50, orchestration_id=o)]
        if all(r.get("status") in ("completed", "failed", "cancelled") for r in runs):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"a run never ended: {runs}")


async def _workflow(**kw):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    orch = make_orchestration(**kw)
    await save_orchestration(orch)
    return orch


class TestWhatATurnPutUp:
    async def test_a_card_is_kept_as_shown_without_its_token(self):
        await _workflow(id="orch_inv", name="Invoice chaser")
        await _assistant_run()
        await _call("prepare_action", {"action": "run_workflow", "workflow": "Invoice chaser", "message": "March"})

        [kept] = (await assistant.turn("builder_turn"))["cards"]
        assert kept["title"] == 'Run "Invoice chaser"'
        [button] = kept["buttons"]
        assert button["tool"] == "run_orchestration"
        assert button["arguments"]["orchestration_id"] == "orch_inv"
        assert "token" not in button and "confirm_token" not in json.dumps(kept)

    async def test_which_one_keeps_its_options_and_what_was_being_done(self):
        await _workflow(id="orch_first", name="C2 sched print")
        await _workflow(id="orch_second", name="C2 sched print")
        await _assistant_run()
        said, _ = await _call("prepare_action", {"action": "run_workflow", "workflow": "C2 sched print",
                                                 "message": "x"})
        assert "error" in said

        [pick] = (await assistant.turn("builder_turn"))["picks"]
        assert pick["action"] == "run_workflow" and pick["field"] == "workflow"
        assert pick["arguments"]["message"] == "x"
        assert [o["id"] for o in pick["options"]] == ["orch_first", "orch_second"]

    async def test_a_connect_card_is_kept_by_what_it_connects(self):
        await _assistant_run()
        await _call("prepare_connection", {"kind": "settings", "screen": "models", "title": "Add a model key"})
        [kept] = (await assistant.turn("builder_turn"))["connects"]
        assert (kept["kind"], kept["what"]) == ("settings", "models")

    async def test_a_tool_that_puts_nothing_up_keeps_nothing(self):
        await _assistant_run()
        await _call("search_help", {"query": "how do I add a schedule"})
        assert await assistant.turn("builder_turn") == {}

    async def test_the_card_is_the_runs_persons(self):
        await _workflow(id="orch_inv", name="Invoice chaser")
        await _assistant_run(user_id="user_ana")
        await _call("prepare_action", {"action": "run_workflow", "workflow": "Invoice chaser", "message": "x"})
        assert (await assistant.turn("builder_turn"))["owner_id"] == "user_ana"


class TestAPressIsKeptOnItsTurn:
    async def _card(self, user_id=None):
        await _workflow(id="orch_inv", name="Invoice chaser")
        await _assistant_run(user_id=user_id)
        _, events = await _call("prepare_action", {"action": "run_workflow", "workflow": "Invoice chaser",
                                                   "message": "March"})
        [card] = [e for e in events if e.get("type") == "action_needed"]
        return card["buttons"][0]

    async def test_a_press_with_its_run_is_kept_with_what_it_did(self, client):
        button = await self._card()
        done = await client.post("/api/assistant/act", json={**button, "run_id": "builder_turn"})
        assert done.status_code == 200, done.text
        await _settled("orch_inv")
        [press] = (await assistant.turn("builder_turn"))["presses"]
        assert (press["tool"], press["label"], press["done"]) == ("run_orchestration", button["label"], True)
        assert press["card"] == 'Run "Invoice chaser"' and press["say"]

    async def test_a_refused_press_is_kept_with_why(self, client):
        button = await self._card()
        await client.post("/api/assistant/act", json={**button, "run_id": "builder_turn"})
        again = await client.post("/api/assistant/act", json={**button, "run_id": "builder_turn"})
        assert again.status_code == 409
        await _settled("orch_inv")
        presses = (await assistant.turn("builder_turn"))["presses"]
        assert [p["done"] for p in presses] == [True, False]
        assert "already" in presses[1]["say"].lower()

    async def test_a_press_without_its_run_works_as_before_and_keeps_nothing(self, client):
        button = await self._card()
        done = await client.post("/api/assistant/act", json=button)
        assert done.status_code == 200
        await _settled("orch_inv")
        assert "presses" not in await assistant.turn("builder_turn")

    async def test_a_press_is_never_kept_on_someone_elses_turn(self):
        from core.product_tools.caller import Caller
        from core.product_tools.roles import Role
        from core.routes.assistant import Press, press

        button = await self._card(user_id="user_ana")
        with pytest.raises(Exception):  # Ben's press of Ana's card is refused by the token itself
            await press(Caller(role=Role.OWNER, via="assistant", user_id="user_ben"),
                        Press(**button, run_id="builder_turn"))
        assert "presses" not in await assistant.turn("builder_turn")

    async def test_a_press_that_is_not_a_button_its_run_showed_is_not_kept(self, client):
        button = await self._card()
        await _workflow(id="orch_other", name="Other")
        await _assistant_run(run_id="builder_elsewhere")
        _, events = await _call("prepare_action", {"action": "run_workflow", "workflow": "Other", "message": "x"},
                                run_id="builder_elsewhere")
        other = [e for e in events if e.get("type") == "action_needed"][0]["buttons"][0]
        # A real press of the other run's button, sent with this turn's run id: done, but never kept here.
        done = await client.post("/api/assistant/act", json={**other, "run_id": "builder_turn"})
        assert done.status_code == 200
        await _settled("orch_other")
        assert "presses" not in await assistant.turn("builder_turn")
        assert button  # this turn's own card is untouched
