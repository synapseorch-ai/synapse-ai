"""Only an approval becomes the Approve and Decline buttons; any other Yes / No is a field in the form — F1560 (HUM1;
Naveen 11-12, after F1557).

The run's card (the run page, Home's "Needs you", the editor's setup card) made a step's one Yes / No its two
buttons, the field's label and Decline (M2a), whatever the field asked: a form with a name, an "Urgent?" and a
priority showed "Urgent" and "Decline" and no Send answer, and the Yes / No it asked never showed as a field.

Now the buttons are an approval's: the one Yes / No that is named like one (F1549's `approved`; approve, approval,
accept, confirm, sign off, as a word of its name) or that its author marks so (`"approval": true` on the field, the
editor's "Ask as Approve / Decline"; `false` keeps a field named so in the form). Any other Yes / No is asked in the
form, beside the step's other fields, and sent with Send answer. Two approvals are neither, as two Yes / Nos were.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIELDS_TS = ROOT / "frontend/src/components/orchestration/humanFields.ts"
SRC = ROOT / "frontend/src"


def _approval(fields: list[dict]):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the run's card is TypeScript")
    script = (f"import * as F from {json.dumps(str(FIELDS_TS))};\n"
              f"const got = F.approvalField({json.dumps(fields)});\nconsole.log(JSON.stringify(got ? (got.name ?? got.key) : null));")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


def _yes_no(name: str, **more) -> dict:
    return {"name": name, "type": "boolean", "label": f"{name}?", **more}


COMMENT = {"name": "comment", "type": "textarea", "label": "Comment"}


@pytest.mark.parametrize("name", ["approved", "approve", "approval", "approve_plan", "plan_approved", "is_approved",
                                  "accept", "accepted", "confirm", "confirmed", "sign_off", "signed_off", "Approved",
                                  "approveDraft"])
def test_a_yes_no_named_like_an_approval_is_one(name):
    assert _approval([_yes_no(name), COMMENT]) == name
    assert _approval([_yes_no(name)]) == name


@pytest.mark.parametrize("name", ["urgent", "send_now", "approver_email", "disapproved_count", "unconfirmed_list",
                                  "acceptance_notes", "is_vip"])
def test_any_other_yes_no_is_a_field_in_the_form(name):
    assert _approval([_yes_no(name), COMMENT]) is None
    assert _approval([_yes_no(name)]) is None, "even alone"


def test_its_author_says_so():
    assert _approval([_yes_no("urgent", approval=True), COMMENT]) == "urgent"
    assert _approval([_yes_no("approved", approval=False), COMMENT]) is None


def test_one_approval_among_other_yes_nos_is_the_buttons():
    assert _approval([_yes_no("urgent"), _yes_no("approved"), COMMENT]) == "approved"
    assert _approval([_yes_no("approved"), _yes_no("confirmed")]) is None, "two: neither, as two Yes / Nos were"


def test_a_field_that_is_no_yes_no_never_is():
    assert _approval([{"name": "approved", "type": "textarea"}]) is None
    assert _approval([{"key": "approved", "type": "yes_no", "label": "OK?"}]) == "approved", "a pack's spelling"


def test_the_card_and_the_editor_read_it():
    model = (SRC / "components/run/model.ts").read_text()
    start = model.index("export function decisionField")
    assert "approvalField(" in model[start:start + 600], model[start:start + 600]
    steps = (SRC / "components/orchestration/panel/steps.tsx").read_text()
    human = steps[steps.index("function HumanSection"):]
    human = human[:human.index("\nfunction ", 10)]
    assert "Ask as Approve / Decline" in human and "approval:" in human
    types = (SRC / "types/orchestration.ts").read_text()
    assert types.count("approval?: boolean") >= 2, "the step's fields and the run's"


def test_the_validators_word_says_when_the_buttons_ask_it():
    from core.orchestration.step_answers import repair

    said = repair({"id": "ask", "type": "human", "human_fields": []}, "ask", "approved")
    assert "Approve and Decline" in said and "named like an approval" in said, said
