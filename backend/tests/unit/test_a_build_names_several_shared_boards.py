"""An ask that names several shared boards already here is built, and the card says where to give it access (F1736).

A build gives or makes one board itself (F1214): the board a plan names is given to its workflow through the board's
access list. A plan that named several shared boards the person already has (Sales, Onboarding, Support) was refused
every time it came back, so the card offered no Build it: a dead end for anyone who names the boards they keep.

Now, where a plan names more than one board, each one already here is the person's to give: the build gives it
nothing, the built agents keep the boards in their instructions, and the card says, in the Boards screen's own words,
which boards to open and what to add under "Who can use this board". At most one new board is still made, and two
new boards are still refused. A board is read from the plan's words only where the words say it is a board, so a
"sales follow-up" or a "support reply" names none.

Once the person gives the workflow "Add cards" on those boards, its agent has the board tools in a run and its card
lands on the board it names.
"""
from __future__ import annotations

import pytest

from _fakes.fake_llm import tool_call
from core.boards import TOOL_NAMES
from core.native_builder import boards_rule
from core.native_builder import objects as O
from core.product_tools.roles import Role
from core.store import boards as store

from tests.unit.test_a_build_is_saved_in_code import (  # noqa: F401 — the build's fixtures and helpers
    CHECK, _call, _plan, _reviewed, _workspace, _yes, workspace)
from tests.unit.test_the_board_asked_for_is_the_one_used import _give, boards_on  # noqa: F401

CLERK = {"name": "Desk clerk", "purpose": "Files each arrival on the board it belongs on", "tools": ["board_add"]}
FILE = {"id": "file", "type": "agent", "name": "Add card", "does": "Adds one task card for the arrival",
        "agent": "Desk clerk", "prompt": "File this arrival as one card: {state.check}."}
PROMPTS = {"Desk clerk": "You file each arrival as one task card, with the customer's details."}
THREE = [{"name": "Sales"}, {"name": "onboarding"}, {"name": "Support"}]
WHERE = 'under "Who can use this board", add "Rotterdam wind warning" with "Add cards"'


@pytest.fixture
async def three_boards(boards_on):  # noqa: F811
    return {n: await store.create_shared_board(n) for n in ("Sales", "Onboarding", "Support")}


def _said(checked: dict) -> str:
    return " ".join([checked.get("say") or "", *(checked.get("notes") or [])])


async def _saved(run) -> dict:
    await _yes(run)
    out, _ = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    return out


async def _access(board) -> list[tuple[str, str, str]]:
    return [(a.principal_kind, a.principal_id, a.access) for a in await store.list_access(board.id)]


# ── several boards already here ──────────────────────────────────────────────────────────────────────────────────
async def test_a_plan_naming_three_shared_boards_here_is_built_and_says_where_to_give_access(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[CLERK], boards=THREE), prompts=PROMPTS)
    assert checked["outcome"] == "review", checked
    assert "build" in checked["offers"], checked["issues"]
    assert not any("a build has one board" in i for i in checked["issues"])
    for said in (checked["say"], " ".join(checked["notes"])):
        assert 'the shared boards "Sales", "Onboarding" and "Support"' in said, said
        assert WHERE in said and "Boards screen" in said, said
        assert ('It works the shared boards "Sales", "Onboarding" and "Support", which are already here. Give it '
                'access to each on the Boards screen: open the board and, under "Who can use this board", add '
                '"Rotterdam wind warning" with "Add cards". Until then, its agents can\'t post there.') in said
    assert [(b["name"], b["existing"], b.get("yours", False)) for b in checked["plan"]["boards"]] == [
        ("Sales", True, True), ("Onboarding", True, True), ("Support", True, True)]
    await _saved(run)
    agents, _, _ = await _workspace()
    for board in three_boards.values():
        assert await _access(board) == [], "the build gives none of them: the person does"
    assert sorted(b.name for b in await store.list_boards("shared")) == ["Onboarding", "Sales", "Support"]
    clerk = agents["Desk clerk"]
    assert clerk["boards"] == {"own": False}, "its cards never land on the workflow's own board instead"
    assert 'the shared boards "Sales", "Onboarding" and "Support"' in clerk["system_prompt"]


async def test_boards_named_as_boards_in_the_plans_words_are_read_the_same(client, three_boards):
    """A plan that names the boards only in its steps (no `boards`): the same card."""
    file = {**FILE, "does": 'Adds one card: the "Sales" board for an enquiry, the Onboarding board for an order, '
                            'board "Support" for a ticket'}
    run, checked = await _reviewed(_plan(CHECK, file, agents=[CLERK]), prompts=PROMPTS)
    assert "build" in checked["offers"], checked["issues"]
    assert [(b["name"], b.get("yours", False)) for b in checked["plan"]["boards"]] == [
        ("Sales", True), ("Onboarding", True), ("Support", True)]
    assert WHERE in _said(checked)


async def test_one_new_board_and_one_here_makes_the_new_one_and_names_the_other(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[CLERK], boards=[{"name": "Escalations"},
                                                                              {"name": "Sales"}]),
                                   prompts=PROMPTS)
    assert "build" in checked["offers"], checked["issues"]
    said = _said(checked)
    assert 'the shared board "Sales", which is already here. Give it access on the Boards screen' in said
    assert WHERE in said
    assert 'Its cards go on the shared board "Escalations" (on the Boards screen), made with it' in said
    await _saved(run)
    _, flows, _ = await _workspace()
    flow_id = flows["Rotterdam wind warning"]["id"]
    [made] = [b for b in await store.list_boards("shared") if b.name == "Escalations"]
    assert await _access(made) == [("workflow", flow_id, "work")], "the one board the build makes, given as today"
    assert await _access(three_boards["Sales"]) == []


async def test_one_board_here_alone_is_given_as_before(client, three_boards):
    run, checked = await _reviewed(_plan(CHECK, FILE, agents=[CLERK], boards=[{"name": "Sales"}]), prompts=PROMPTS)
    assert "Who can use this board" not in _said(checked)
    assert [(b["name"], b.get("yours", False)) for b in checked["plan"]["boards"]] == [("Sales", False)]
    await _saved(run)
    _, flows, _ = await _workspace()
    assert await _access(three_boards["Sales"]) == [("workflow", flows["Rotterdam wind warning"]["id"], "work")]


async def test_two_new_boards_are_still_refused(client, three_boards):
    run, _ = await _reviewed(_plan(CHECK))
    sent, _ = await _call("submit_plan", _plan(CHECK, boards=[{"name": "A"}, {"name": "B"}, {"name": "Sales"}]), run)
    assert "a build has one board" in sent["error"]


# ── words that are not boards (F1633's near misses) ──────────────────────────────────────────────────────────────
async def test_a_sales_follow_up_and_a_support_reply_name_no_board(client, three_boards):
    draft = {**FILE, "id": "draft", "name": "Draft", "does": "Drafts the sales follow-up"}
    reply = {**FILE, "id": "reply", "name": "Reply", "does": "Writes the support reply",
             "prompt": "Write the support reply from {state.draft}."}
    draft["prompt"] = "Draft the sales follow-up from {state.check}."
    run, checked = await _reviewed(_plan(CHECK, draft, reply, agents=[{**CLERK, "tools": []}]), prompts=PROMPTS)
    assert checked["plan"]["boards"] == []
    assert "Who can use this board" not in _said(checked) and "shared board" not in _said(checked)
    await _saved(run)
    agents, _, _ = await _workspace()
    assert "boards" not in agents["Desk clerk"], "its own board is left as it was"
    for board in three_boards.values():
        assert await _access(board) == []


async def test_one_board_named_in_words_is_given_as_before(client, three_boards):
    file = {**FILE, "does": "Adds the enquiry to the Sales board"}
    run, checked = await _reviewed(_plan(CHECK, file, agents=[CLERK]), prompts=PROMPTS)
    assert [(b["name"], b.get("yours", False)) for b in checked["plan"]["boards"]] == [("Sales", False)]
    assert "Who can use this board" not in _said(checked)


async def test_one_board_named_and_another_boards_word_used_loosely_is_as_before(client, three_boards):
    """Two boards' names in the text, one of them only as a word, reads as it did: no board is taken from it."""
    file = {**FILE, "does": "Drafts a support reply and adds the enquiry to the Sales board"}
    run, checked = await _reviewed(_plan(CHECK, file, agents=[CLERK]), prompts=PROMPTS)
    assert checked["plan"]["boards"] == []
    assert "Who can use this board" not in _said(checked)


# ── what the agents are told ─────────────────────────────────────────────────────────────────────────────────────
def test_a_board_agent_is_told_every_board_it_works():
    plan = O.PlanDraft.model_validate(_plan(FILE, agents=[CLERK], boards=[
        {"name": "Sales", "existing": True, "yours": True}, {"name": "Support", "existing": True, "yours": True}]))
    told = boards_rule.with_board_lines(plan, {"Desk clerk": "File it."})["Desk clerk"]
    assert 'You work the shared boards "Sales" and "Support"' in told
    assert "naming the board it belongs on" in told
    one = O.PlanDraft.model_validate(_plan(FILE, agents=[CLERK], boards=[{"name": "Sales"}]))
    assert 'You work the board "Sales".' in boards_rule.with_board_lines(one, {"Desk clerk": "File it."})["Desk clerk"]


# ── once access is given, it works ───────────────────────────────────────────────────────────────────────────────
async def test_once_given_add_cards_its_agent_adds_its_card_to_the_board_it_names(client, three_boards, fake_llm):
    from core.react_engine import run_agent_step

    run, _ = await _reviewed(_plan(CHECK, FILE, agents=[CLERK], boards=THREE), prompts=PROMPTS)
    await _saved(run)
    agents, flows, _ = await _workspace()
    clerk, flow_id = agents["Desk clerk"], flows["Rotterdam wind warning"]["id"]

    async def turn(script):
        fake_llm.script(script)
        server = type("S", (), {"agent_sessions": {}, "memory_store": None, "tool_router": {}})()
        return [ev async for ev in run_agent_step(
            message="A contact form enquiry from Pat Example", agent_id=clerk["id"], session_id="s_front_door",
            server_module=server, agent_override=clerk, max_turns=4, source="orchestration", run_id="run_front_door",
            board_scope={"kind": "workflow", "id": flow_id, "holder": "run_front_door/file"})]

    def offered() -> set[str]:
        return {t["function"]["name"] for t in (fake_llm.calls[-1].get("tools") or [])} & set(TOOL_NAMES)

    await turn(["done"])
    assert offered() == set(), "before the person gives access, no board tool"
    for name in ("Sales", "Onboarding", "Support"):
        await _give(Role.BUILDER, board=name, kind="workflow", id=flow_id, access="add")
    await turn([tool_call("board_add", board="Sales", title="Enquiry from Pat Example"), "done"])
    assert "board_add" in offered()
    on = {n: [c.title for c in await store.list_cards([b.id])] for n, b in three_boards.items()}
    assert on == {"Sales": ["Enquiry from Pat Example"], "Onboarding": [], "Support": []}
    assert await store.find_scoped_board("workflow", flow_id) is None, "nothing went to its own board"


def test_the_card_names_what_the_boards_screen_shows():
    """The heading and the access the card names are the Boards screen's own labels."""
    from pathlib import Path

    boards = Path(__file__).resolve().parents[3] / "frontend/src/components/boards"
    assert f">{boards_rule.ACCESS_LIST}</h4>" in (boards / "AccessList.tsx").read_text()
    labels = (boards / "board.ts").read_text()
    assert all(f"{k}: '{v}'" in labels for k, v in boards_rule.ACCESS_WORDS.items())
