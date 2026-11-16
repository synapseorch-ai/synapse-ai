"""A workflow the person asks to start from a form starts from one, and its card says so (F1616).

Asked for "someone submits an expense on a form (amount, category, a short note)", a plan made its first step a Tool
step running `collect_data`, an agent's way to ask for fields inside a chat. As a workflow's Tool step it returns the
form it would show, never the answers, so the next step read `state.collect_expense.amount`, which nothing wrote, and
every run failed. The card said it starts "When you run it".

Now `collect_data` is never a Tool step: the plan check and the validator send it back with the repair, a `human`
step with `human_fields` first in the workflow, which the card says starts when someone fills it in. And a request
that asks for a form, planned without a form first, is sent back once as a structure finding, then said on the card.
`collect_data` given to an agent is unchanged.
"""
from __future__ import annotations

from core.builder_tools import _validate_orchestration
from core.native_builder import build_plan

ASK = ("Make a workflow where someone submits an expense on a form (amount, category, a short note). If the amount is "
       "over 500, ask me to approve it first; otherwise approve it straight away.")

COLLECT = {"id": "collect_expense", "type": "tool", "name": "Collect Expense", "does": "Collects the expense",
           "tool": "collect_data", "next": "approve"}
FORM = {"id": "submit", "type": "human", "name": "Submit the expense", "does": "Someone enters the expense",
        "prompt": "The expense's amount, category and a short note",
        "config": {"human_fields": [{"name": "amount", "type": "number"}, {"name": "category", "type": "string"},
                                    {"name": "note", "type": "string"}]}, "next": "approve"}
READ = {"id": "read", "type": "llm", "name": "Read the expense", "does": "Reads the expense",
        "prompt": "Read the expense in {{ state.user_input }}.", "next": "approve"}
APPROVE = {"id": "approve", "type": "human", "name": "Approve it", "does": "You approve it",
           "prompt": "Approve this expense?", "next": "end"}


def _plan(*steps: dict) -> dict:
    return {"kind": "create", "title": "Expenses", "summary": "Takes expenses and asks for approval.",
            "steps": list(steps)}


async def _examined(*steps: dict, ask: str = ASK):
    return await build_plan.examine(_plan(*steps), None, ws=build_plan.Workspace(), state={"user_message": ask})


class TestCollectDataIsNeverAToolStep:
    async def test_the_plan_check_sends_it_back_with_the_form_repair(self):
        found = await _examined(COLLECT, APPROVE)
        said = [e for e in found.errors if "collect_data" in e]
        assert len(said) == 1, found.errors
        assert "human" in said[0] and "human_fields" in said[0], said[0]

    def test_the_validator_refuses_it_on_a_saved_workflow(self):
        orch = {"id": "orch_f1616", "name": "Expenses", "entry_step_id": "collect", "steps": [
            {"id": "collect", "name": "Collect", "type": "tool", "forced_tool": "collect_data", "output_key": "collect",
             "next_step_id": "done"},
            {"id": "done", "name": "Done", "type": "end"}]}
        said = [i for i in _validate_orchestration(orch)["issues"] if "collect_data" in i]
        assert len(said) == 1 and "human_fields" in said[0], said


class TestAFormAskStartsFromAForm:
    async def test_a_form_ask_planned_without_a_form_first_is_a_structure_finding(self):
        found = await _examined(READ, APPROVE)
        said = [s for s in found.structure if "form" in s.lower()]
        assert len(said) == 1, found.structure
        assert "Read the expense" in said[0] and "human" in said[0], said[0]

    async def test_a_form_first_is_not_one(self):
        found = await _examined(FORM, APPROVE)
        assert not [s for s in found.structure if "form" in s.lower()], found.structure

    async def test_an_ask_with_no_form_is_not_one(self):
        found = await _examined(READ, APPROVE, ask="Read the expense I paste and ask me to approve it.")
        assert not [s for s in found.structure if "form" in s.lower()], found.structure

    async def test_in_the_form_of_is_no_form(self):
        found = await _examined(READ, APPROVE, ask="Read the expense I paste and give it back in the form of a table.")
        assert not [s for s in found.structure if "form" in s.lower()], found.structure
