"""The builder builds only on an explicit yes (F754, BWA1, D274).

BWA1's can't-do rescore (2026-10-17): at the plan's review the person answered its open questions — services, prices,
opening hours — and never said "approve"; `step_approval_route`, an evaluator on the quick role, read that as
approval, and the builder made an agent and a workflow. Every build's and every manage change's approval was a model
reading free text. Naveen: approval is decided in code. Only an explicit yes approves (what the review asks for, and
what the card's Build it / Go ahead sends); anything else goes back to the drafter with what the person said.
"""
from __future__ import annotations

import pytest

from core.native_builder import bundled_definitions
from core.orchestration.steps import _eval_state_expr

GATES = {
    # gate: (the human step's output key, approved →, anything else →)
    "step_approval_route": ("review_feedback", "step_agents_needed", "step_draft_plan"),
    "step_manage_route": ("manage_review", "step_after_yes", "step_change"),
}

YES = ["approve", "Approve", " approve ", "approved", "ok", "OK!", "okay", "yes", "Yes.", "looks good",
       "Looks good!", "go ahead", "Go ahead.", "build it", "lgtm"]
NOT_YES = [
    "- Services: Check-ups ($80), Cleaning ($120). Opening hours: Mon-Fri 8am-5pm.",   # the answers that built
    "approve, but add a reminder step", "yes but use Slack instead", "no", "not yet", "change the time to 9am",
    "", None,
]


def _steps() -> dict:
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


@pytest.mark.parametrize("gate", sorted(GATES))
def test_each_gate_is_decided_in_code_not_by_a_model(gate):
    step = _steps()[gate]
    key, approved, revise = GATES[gate]
    assert step["type"] == "if_else"
    assert (step["if_true_step_id"], step["if_false_step_id"]) == (approved, revise)
    assert key in step["input_keys"]


@pytest.mark.parametrize("gate", sorted(GATES))
@pytest.mark.parametrize("answer", YES)
def test_an_explicit_yes_approves(gate, answer):
    step, (key, approved, _) = _steps()[gate], GATES[gate]
    value, error = _eval_state_expr(step["if_condition"], {key: {"feedback": answer}}, default=False)
    assert error is None and value is True, answer


@pytest.mark.parametrize("gate", sorted(GATES))
@pytest.mark.parametrize("answer", NOT_YES)
def test_anything_else_goes_back_to_the_drafter(gate, answer):
    step, (key, _, revise) = _steps()[gate], GATES[gate]
    state = {key: {} if answer is None else {"feedback": answer}}
    value, error = _eval_state_expr(step["if_condition"], state, default=False)
    assert error is None and value is False, answer


def test_the_review_asks_for_the_words_the_gate_takes():
    """The review's own sentence names what approves; each must approve, or the person is told to say a word that
    redrafts."""
    import re

    step = _steps()["step_human_review"]
    named = re.findall(r"\*\*(\w+)\*\*|'([^']+)'", step["human_prompt"].split("---")[0])
    words = [a or b for a, b in named]
    assert "approve" in words
    gate = _steps()["step_approval_route"]
    for word in words:
        value, _ = _eval_state_expr(gate["if_condition"], {"review_feedback": {"feedback": word}}, default=False)
        assert value is True, word


def test_the_card_sends_a_word_the_gate_takes():
    from pathlib import Path

    card = Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/cards.tsx"
    if not card.exists():
        pytest.skip("no frontend in this checkout")
    import re

    sent = re.search(r"export const APPROVE_WORD = '([^']+)';", card.read_text()).group(1)
    gate = _steps()["step_approval_route"]
    value, _ = _eval_state_expr(gate["if_condition"], {"review_feedback": {"feedback": sent}}, default=False)
    assert value is True


@pytest.mark.parametrize("answer", YES + NOT_YES)
def test_the_change_route_s_own_check_of_the_yes_is_the_gate_s(answer):
    """`approve_change` checks the yes again rather than trusting the branch it is on (SAI2-P2): it must read every
    answer exactly as the gate does."""
    from core.native_builder.review import APPROVAL_WORDS, is_yes

    gate = {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}["step_manage_route"]
    took, error = _eval_state_expr(gate["if_condition"], {"manage_review": {"feedback": answer}}, default=False)
    assert error is None and is_yes(answer) is bool(took)
    words = gate["if_condition"].split(" in (", 1)[1].rstrip(")")
    assert words == ", ".join(f'"{w}"' for w in APPROVAL_WORDS)
