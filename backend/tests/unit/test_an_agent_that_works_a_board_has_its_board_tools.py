"""An agent that works the board a plan names has the board tools, as the run gives them (F1738).

A run gives a workflow step's agent the board tools from the boards its workflow may use, whatever its own tools list
says. The planner is told to name them in that agent's tools; when it does not, every reader of the list says the
agent has nothing: the card's "has no tools: it … fetches or changes nothing", its step's "with no tools", and the
saved instructions, which then never say which board to work.

Now, in a plan that names a board while Boards are on, code names them for it: an agent with no board tool whose step
works the board (its words say board or card, or name one of the plan's boards, or it is the plan's only agent step)
is given `board_add` where a board is the person's to give (they choose its access), and all five where the build
gives or makes the board ("Read, claim and update"). An agent the planner gave a board tool keeps its list as
written. A step that does something else, a plan with no board, and Boards off read as before.
"""
from __future__ import annotations

import pytest

from core.boards import TOOL_NAMES

from tests.unit.test_a_build_is_saved_in_code import (  # noqa: F401 — the build's fixtures and helpers
    CHECK, _call, _plan, _reviewed, _workspace, _yes, workspace)
from tests.unit.test_a_build_names_several_shared_boards import (  # noqa: F401
    CLERK, FILE, PROMPTS, THREE, _saved, three_boards)
from tests.unit.test_the_board_asked_for_is_the_one_used import boards_on  # noqa: F401

BARE = {**CLERK, "tools": []}
WELCOME = {"name": "Welcome writer", "purpose": "Writes welcome emails", "tools": []}
DRAFT = {"id": "welcome", "type": "agent", "name": "Draft welcome", "does": "Drafts the welcome email",
         "agent": "Welcome writer", "prompt": "Draft the welcome email from {state.file}."}
TWO_PROMPTS = {**PROMPTS, "Welcome writer": "You write short welcome emails."}
NO_TOOLS = "has no tools"


def _agent(checked: dict, name: str) -> dict:
    return {a["name"]: a for a in checked["plan"]["agents"]}[name]


# ── the agent that works the board ───────────────────────────────────────────────────────────────────────────────
async def test_on_boards_the_person_gives_it_adds_cards_and_its_card_and_instructions_say_so(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[BARE], boards=THREE), prompts=PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == ["board_add"]
    assert not [o for o in checked["plan"]["off"] if NO_TOOLS in o], checked["plan"]["off"]
    step = checked["plan"]["step_words"]["file"]
    assert "with tools: adds cards to a board" in step and "with no tools" not in step, step
    assert 'with "Add cards"' in checked["say"], "the access the person gives is still Add cards"
    await _saved(run)
    told = (await _workspace())[0]["Desk clerk"]["system_prompt"]
    assert "## Your board" in told and 'the shared boards "Sales", "Onboarding" and "Support"' in told, told
    assert "Add a card for each thing you do (board_add)." in told


async def test_on_a_board_the_build_gives_it_works_it_with_all_five(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[BARE], boards=[{"name": "Sales"}]), prompts=PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == list(TOOL_NAMES)
    assert not [o for o in checked["plan"]["off"] if NO_TOOLS in o]
    step = checked["plan"]["step_words"]["file"]
    assert step.count("reads and updates a board's cards") == 1, "each tool's words said once"


async def test_a_step_that_names_a_plan_board_works_it(client, three_boards):
    file = {**FILE, "name": "File it", "does": "Files the enquiry under Sales"}
    run, checked = await _reviewed(_plan(CHECK, file, DRAFT, agents=[BARE, WELCOME], boards=THREE),
                                   prompts=TWO_PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == ["board_add"]


async def test_the_plans_only_agent_step_works_its_board(client, three_boards):
    file = {**FILE, "name": "Handle it", "does": "Handles what arrived", "prompt": "Handle this: {state.check}."}
    run, checked = await _reviewed(_plan(CHECK, file, agents=[BARE], boards=THREE), prompts=PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == ["board_add"]


async def test_tools_the_planner_named_are_kept_as_written(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[{**CLERK, "tools": ["board_add"]}],
                                         boards=[{"name": "Sales"}]), prompts=PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == ["board_add"]


# ── as before (F1633's near misses) ──────────────────────────────────────────────────────────────────────────────
async def test_a_second_step_that_drafts_an_email_still_has_no_tools(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, DRAFT, agents=[BARE, WELCOME], boards=THREE),
                                   prompts=TWO_PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == ["board_add"]
    assert _agent(checked, "Welcome writer")["tools"] == []
    assert any(o.startswith('"Welcome writer" has no tools') for o in checked["plan"]["off"]), checked["plan"]["off"]


async def test_its_own_board_alone_is_as_before(client, boards_on):  # noqa: F811
    file = {**FILE, "does": "Writes a short note on the arrival", "prompt": "Write a note on {state.check}."}
    run, checked = await _reviewed(_plan(CHECK, file, agents=[BARE]), prompts=PROMPTS)
    assert checked["plan"]["boards"] == []
    assert _agent(checked, "Desk clerk")["tools"] == []
    assert any(o.startswith('"Desk clerk" has no tools') for o in checked["plan"]["off"])


async def test_while_boards_are_off_it_is_as_before(client):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[BARE], boards=[{"name": "Sales"}]), prompts=PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == []
    assert any(o.startswith('"Desk clerk" has no tools') for o in checked["plan"]["off"])


@pytest.mark.parametrize("does", [
    "Charges the customer's card",
    "Writes a thank-you card",
    "Reads the scanned business card",
    "Sends a gift card",
    "Drafts the sales follow-up",
    "Prepares notes for the board meeting",
    "Reports to the board of directors",
], ids=["charges-card", "thank-you-card", "business-card", "gift-card", "sales-follow-up", "board-meeting",
        "board-of-directors"])
async def test_a_card_or_a_board_in_another_sense_is_no_board_work(client, three_boards, does):
    """In a plan that works its boards, a step whose card or board is another kind keeps a true "has no tools"."""
    step = {"id": "file", "type": "agent", "name": "Do it", "does": does, "agent": "Desk clerk",
            "prompt": f"{does}, from {{state.check}}."}
    run, checked = await _reviewed(_plan(CHECK, step, DRAFT, agents=[BARE, WELCOME], boards=[{"name": "Sales"}]),
                                   prompts=TWO_PROMPTS)
    assert _agent(checked, "Desk clerk")["tools"] == []
    assert any(o.startswith('"Desk clerk" has no tools') for o in checked["plan"]["off"]), checked["plan"]["off"]
