"""A change that makes a board or a knowledge base makes it with its parts, and says a feature that is off once
(F1670, F1698).

Asked for a knowledge base of the W-9 form's page, Synapse AI's card read "Make a knowledge base 'IRS Tax Forms'" with
no page on it, and the page's own op was refused "No knowledge_base is called 'IRS Tax Forms'": an op aimed at what
the same change makes was looked for among what is there already (F1670). Asked for a board with three cards, it put
up "Turn on Boards" four times while Boards were off, and once they were on, refused each card "No board is called
'Launch checklist'" (F1698).

Now a board's first cards and a base's pages go on the card that makes it, which says them; any other op on what the
change makes is said once, as coming once it is made; a feature that is off is said once, with one switch; and a
part of the ask that is not done never ends "..".
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

W9 = "https://www.irs.gov/pub/irs-pdf/fw9.pdf"
BOARD = "Launch checklist"
CARDS = ["Book the venue", "Send the invites", "Order the food"]


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


async def _check(ops, message: str):
    run = OrchestrationRun(run_id="builder_f1670", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "user_message": message})
    await SharedState(run).checkpoint()
    with live(run):
        raw, events = await execute_builder_tool_with_events(
            "check_change", {"draft": {"finish": "submit_change", "ops": ops}}, _Server(), run_id=run.run_id)
    return json.loads(raw), [e for e in events if e.get("type") == "action_needed"]


def _board_ops():
    return [{"op": "board_create", "target": {"name": BOARD}, "fields": {"text": "What the launch needs"}},
            *({"op": "board_add_card", "target": {"name": BOARD}, "fields": {"title": t}} for t in CARDS)]


def _said(out) -> str:
    return json.dumps(out, ensure_ascii=False)


class TestABoardWithItsCards:
    async def test_boards_off_puts_up_one_switch_and_says_it_once(self, monkeypatch):
        from _fakes.knowledge_env import use_saved_settings

        use_saved_settings(monkeypatch)
        out, cards = await _check(_board_ops(), f'Make a board "{BOARD}" with three cards')
        switches = [c for c in cards if c.get("title") == "Turn on Boards"]
        assert len(switches) == 1, [c.get("title") for c in cards]
        assert str(out.get("say") or "").count("Boards are switched off") == 1, out.get("say")

    async def test_its_cards_go_on_the_card_that_makes_it(self, monkeypatch):
        from _fakes.knowledge_env import use_saved_settings
        from core.routes.settings import save_settings

        use_saved_settings(monkeypatch)
        await save_settings({"boards_enabled": True})
        out, cards = await _check(_board_ops(), f'Make a board "{BOARD}" with three cards')
        assert [c["title"] for c in cards] == [f'Make a shared board "{BOARD}"'], [c.get("title") for c in cards]
        assert all(t in cards[0]["detail"] for t in CARDS), cards[0]["detail"]
        assert cards[0]["buttons"][0]["arguments"].get("cards") == [{"title": t} for t in CARDS], cards[0]["buttons"]
        said = _said(out)
        assert "No board is called" not in said and "list_boards" not in said, said


@pytest.fixture
def knowledge_on(monkeypatch):
    """Knowledge on with no base yet; nothing is fetched."""
    from core.knowledge import bases
    from core.product_tools import knowledge

    monkeypatch.setattr(knowledge, "_on", lambda: True)

    async def listed():
        return []

    monkeypatch.setattr(bases, "list_bases", listed)


class TestABaseWithItsPages:
    async def test_its_pages_go_on_the_card_that_makes_it(self, knowledge_on):
        ops = [{"op": "knowledge_create", "target": {"name": "IRS Tax Forms"}, "fields": {"text": "IRS forms"}},
               {"op": "knowledge_add_pages", "target": {"name": "IRS Tax Forms"}, "fields": {"urls": [W9]}}]
        out, cards = await _check(ops, f"Make a knowledge base from {W9} and tell me who fills it in")
        assert [c["title"] for c in cards] == ['Make a knowledge base "IRS Tax Forms"'], [c.get("title") for c in cards]
        assert W9 in cards[0]["detail"], cards[0]["detail"]
        assert cards[0]["buttons"][0]["arguments"].get("urls") == [W9], cards[0]["buttons"]
        said = _said(out)
        assert "No knowledge_base is called" not in said and "knowledge_base is" not in said, said

    async def test_another_op_on_it_is_said_as_coming_once_it_is_made(self, knowledge_on):
        ops = [{"op": "knowledge_create", "target": {"name": "IRS Tax Forms"}, "fields": {}},
               {"op": "knowledge_add_note", "target": {"name": "IRS Tax Forms"}, "fields": {"text": "Due each year"}}]
        out, cards = await _check(ops, "Make a knowledge base IRS Tax Forms with a note: due each year")
        said = _said(out)
        assert 'once \\"IRS Tax Forms\\" is made' in said or "once “IRS Tax Forms” is made" in said, said
        assert "No knowledge" not in said, said


def test_a_part_not_done_never_ends_twice():
    from core.native_builder.change_tools import not_done

    said = not_done([{"part": "Tell me who has to fill it in", "why_not": "It is answered from the form once made."}])
    assert said.endswith("once made.") and not said.endswith(".."), said
