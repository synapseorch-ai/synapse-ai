"""An agent's board tools, and the access its card asks for, follow what its step does to each board (F1746).

On boards already here that the person gives access to, every agent the plan gave no board tool was given `board_add`
only, and the card asked for "Add cards" on every board. A team lead whose step claims the Onboarding card, closes it
and adds one to Deliveries then had no tool to claim or close, and a person following the card gave access it could
not work with. Now a step's words are read clause by clause: a clause with a work verb (claim, close, update, move,
read, pick up, finish, mark) whose object is board work (a board, a task card, one of the plan's boards by name)
works a board, and its agent gets all five tools; one that only adds gets `board_add`. The card names the access each
board needs: "Read, claim and update" where a clause naming it works it, "Add cards" where its clauses only add, and
today's one sentence when every board needs the same.

Near misses read as before: an agent that only adds, a list the planner wrote, "reads the enquiry", "close the deal",
a board the build gives, and boards that all need "Add cards".
"""
from __future__ import annotations

import pytest

from core.boards import TOOL_NAMES
from core.native_builder import boards_rule
from core.native_builder import objects as O
from core.store import boards as store

from tests.unit.test_a_build_is_saved_in_code import (  # noqa: F401 — the build's fixtures and helpers
    CHECK, _plan, _reviewed, workspace)
from tests.unit.test_the_board_asked_for_is_the_one_used import boards_on  # noqa: F401

SHARED = ["Onboarding", "Deliveries", "Sales", "Support"]
LEAD = {"name": "Onboarding lead", "purpose": "Runs each new customer's onboarding", "tools": []}
TAKE = {"id": "take", "type": "agent", "name": "Take the onboarding", "agent": "Onboarding lead",
        "does": "Claim the Onboarding card, close it, and add one to Deliveries",
        "prompt": "Claim the Onboarding card for {{ state.check }}, close it when done, and add one to Deliveries."}


def _plan_of(does: str, boards: list[str], tools: list[str] | None = None, prompt: str | None = None) -> O.PlanDraft:
    plan = O.PlanDraft.model_validate({
        "kind": "create", "title": "Team onboarding", "summary": "Works each new customer's onboarding.",
        "agents": [{**LEAD, "tools": list(tools or [])}],
        "steps": [{"id": "take", "type": "agent", "name": "Do it", "agent": "Onboarding lead", "does": does,
                   "prompt": prompt}],
        "boards": [{"name": n} for n in boards]})
    boards_rule.settle(plan, SHARED)
    boards_rule.with_tools(plan, True)
    return plan


def _tools(plan: O.PlanDraft) -> list[str]:
    return list(plan.agents[0].tools)


class TestTheLeadWhoClaimsAndCloses:
    def test_it_gets_all_five_and_each_board_its_own_access(self):
        plan = _plan_of(TAKE["does"], ["Onboarding", "Deliveries"])
        assert _tools(plan) == list(TOOL_NAMES)
        assert boards_rule.board_access(plan) == {"Onboarding": "work", "Deliveries": "add"}, \
            "'close it' names no board: Onboarding's access is its claim's, and Deliveries only gets a card"
        [line] = boards_rule.give_line(plan)
        assert ('add "Team onboarding" with "Read, claim and update" on "Onboarding" and "Add cards" on '
                '"Deliveries". Until then') in line

    def test_where_one_of_its_texts_ends_and_the_next_begins_is_a_clause_break(self):
        """`does` ending "… add one to Deliveries" and a prompt starting "Claim the Onboarding card" are two clauses,
        never "add one to Deliveries Claim the Onboarding card", which would work Deliveries."""
        plan = _plan_of("Take each new customer's card and add one to Deliveries", ["Onboarding", "Deliveries"],
                        prompt="Claim the Onboarding card, then close it when done.")
        assert boards_rule.board_access(plan) == {"Onboarding": "work", "Deliveries": "add"}

    def test_picking_up_and_updating_a_boards_cards_works_it(self):
        plan = _plan_of("Pick up each card on the Support board and update it when done", ["Support", "Sales"])
        assert _tools(plan) == list(TOOL_NAMES)
        assert boards_rule.board_access(plan)["Support"] == "work"

    async def test_through_the_check_the_card_says_both(self, client, boards_on):  # noqa: F811
        for name in ("Onboarding", "Deliveries"):
            await store.create_shared_board(name)
        run, checked = await _reviewed(_plan(CHECK, TAKE, agents=[LEAD],
                                             boards=[{"name": "Onboarding"}, {"name": "Deliveries"}]),
                                       prompts={"Onboarding lead": "You run each new customer's onboarding."})
        agent = {a["name"]: a for a in checked["plan"]["agents"]}["Onboarding lead"]
        assert agent["tools"] == list(TOOL_NAMES)
        said = " ".join([checked.get("say") or "", *(checked.get("notes") or [])])
        assert '"Read, claim and update" on "Onboarding" and "Add cards" on "Deliveries"' in said, said


class TestNearMisses:
    @pytest.mark.parametrize("does", [
        "Adds one task card for the arrival",
        "Reads the enquiry and adds a card to Sales",
        "Close the deal and add a card to Sales",
    ])
    def test_an_agent_that_only_adds_gets_board_add_and_add_cards(self, does):
        plan = _plan_of(does, ["Sales", "Support"])
        assert _tools(plan) == ["board_add"]
        assert set(boards_rule.board_access(plan).values()) == {"add"}
        [line] = boards_rule.give_line(plan)
        assert 'add "Team onboarding" with "Add cards". Until then' in line, "today's sentence, word for word"

    def test_a_list_the_planner_wrote_is_kept_and_the_card_follows_it(self):
        plan = _plan_of(TAKE["does"], ["Onboarding", "Deliveries"], tools=["board_add"])
        assert _tools(plan) == ["board_add"]
        assert set(boards_rule.board_access(plan).values()) == {"add"}

    def test_a_board_the_build_gives_still_gets_all_five(self):
        plan = _plan_of("Adds one task card for the arrival", ["Sales"])
        assert not plan.boards[0].yours and _tools(plan) == list(TOOL_NAMES)
        assert boards_rule.give_line(plan) == []
