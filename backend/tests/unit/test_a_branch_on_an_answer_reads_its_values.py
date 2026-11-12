"""An If/Else branches on a Human step's Yes / No or One of from a dropdown, and a condition that cannot follow the answer
is said — F1558 (HUM1; Naveen 11-12: "if I set yes or no in the human step and add if else for its output, can I use the
drop-down value in the if else — and when I try it's not coming properly").

The If/Else's condition was a box to type in, with chips that add `state.<key>`. Nothing offered a step's fields or the
values they hold, and each way a person reached for one read something the answer never is:

- the chip writes `state.review`, the whole answer — a mapping of its fields, never empty — so the If/Else took True on
  Approve and on Decline alike (and `state.review == True`, False on both);
- a Yes / No holds true or false (F1434, F1552), so `state.review.approved == 'Yes'` was never true;
- a One of holds its option as written (F455), so `state.review.decision == 'yes'` against "Yes" was never true.

The run said nothing: each is a working condition that never follows the answer.

Now the If/Else has "Branch on an answer": every Yes / No and One of field a step asks (a Human step's fields) or answers
(a model step's answer fields, F1553), and the values it holds; picking both writes the condition, which routes each
value its way, and a condition written so is read back into the two dropdowns. And a condition that compares a Yes / No
with a word, compares a One of with a word that is not one of its options, or decides on such an answer whole is warned,
on the server (`validate_orchestration`) and in the editor alike, with the fix. A warning: the run is unchanged.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from _fakes import seed as S

from core.builder_tools import _validate_orchestration

ROOT = Path(__file__).resolve().parents[3]
FIELDS_TS = ROOT / "frontend/src/components/orchestration/humanFields.ts"

YES_NO = {"name": "approved", "type": "boolean", "label": "Approve the plan?"}
ONE_OF = {"name": "decision", "type": "choice", "label": "Decision", "options": ["Yes", "No", "Later"]}
COMMENT = {"name": "comment", "type": "textarea", "label": "Comment"}
REVIEW = {"id": "review", "name": "Review", "type": "human", "human_prompt": "Review the plan", "output_key": "review",
          "human_fields": [YES_NO, ONE_OF, COMMENT]}


def _gate(condition: str, gid: str = "gate") -> dict:
    return {"id": gid, "name": "Gate", "type": "if_else", "if_condition": condition, "if_true_step_id": "yes",
            "if_false_step_id": "no"}


def _orch(*steps: dict, orch_id: str = "orch_f1558") -> dict:
    tail = [{"id": "yes", "name": "YES", "type": "print", "print_content": "yes", "output_key": "o", "next_step_id": None},
            {"id": "no", "name": "NO", "type": "print", "print_content": "no", "output_key": "o", "next_step_id": None}]
    steps = [dict(s) for s in steps]
    for here, after in zip(steps, steps[1:]):
        if here.get("type") != "if_else":
            here.setdefault("next_step_id", after["id"])
    return S.make_orchestration(id=orch_id, entry_step_id=steps[0]["id"], steps=[*steps, *tail])


def _warned(orch: dict) -> list[str]:
    return [w for w in _validate_orchestration(orch).get("warnings") or [] if "never" in w and "Step 'gate'" in w]


async def _arm(orch: dict, answer) -> str:
    from test_a_condition_reads_a_field_its_step_writes import _answered

    return await _answered(orch, answer)


class TestWhatNaveenMet:
    """The engine as it is: each condition runs, and none of them follows the answer."""

    @pytest.mark.parametrize("condition, arms", [
        ("state.review", ("true", "true")),                       # the chip: the whole answer
        ("state.review == True", ("false", "false")),
        ("state.review.approved == 'Yes'", ("false", "false")),   # a Yes / No holds true or false
    ])
    async def test_approve_and_decline_go_one_way(self, condition, arms):
        name = re.sub(r"\W", "", condition).lower()
        got = []
        for flag in (True, False):
            orch = _orch({**REVIEW, "id": "review"}, _gate(condition), orch_id=f"orch_f1558_m_{name}_{flag}")
            got.append(await _arm(orch, {"approved": flag, "decision": "Yes", "comment": ""}))
        assert tuple(got) == arms

    async def test_a_one_of_is_its_option_as_written(self):
        orch = _orch(REVIEW, _gate("state.review.decision == 'yes'"), orch_id="orch_f1558_m_case")
        assert await _arm(orch, {"approved": True, "decision": "yes", "comment": ""}) == "false", "stored as 'Yes'"


class TestTheServerSaysSo:
    def test_a_yes_no_compared_with_a_word(self):
        [said] = _warned(_orch(REVIEW, _gate("state.review.approved == 'Yes'")))
        assert said.startswith("Step 'gate' compares state.review.approved, a yes/no, with 'Yes': it holds true or "
                               "false, so the comparison never follows the answer — compare it with True or False"), said
        assert said.endswith("It runs as it is today."), said

    def test_a_one_of_compared_with_a_word_it_is_not(self):
        [said] = _warned(_orch(REVIEW, _gate('state.review.decision != "yes"')))
        assert said.startswith("Step 'gate' compares state.review.decision, one of 'Yes', 'No', 'Later', with 'yes': "
                               "the answer is the option as written, so the comparison never follows it — use 'Yes'"), said
        [other] = _warned(_orch(REVIEW, _gate("state.review.decision == 'Maybe'")))
        assert "never follows it — compare it with one of them" in other, other

    @pytest.mark.parametrize("condition", ["state.review", "not state.review", "state.review == True",
                                           "state['review'] != False", "{{ state.review }}"])
    def test_the_whole_answer_decided_on(self, condition):
        [said] = _warned(_orch(REVIEW, _gate(condition)))
        assert said.startswith("Step 'gate' decides on state.review whole, human step 'review''s answer: a mapping of "
                               "its fields, never empty and never True itself, so this never follows it — read one of "
                               "its fields, e.g. state.review.approved"), said

    def test_a_model_steps_declared_answer_too(self):
        classify = {"id": "classify", "name": "Classify", "type": "llm", "prompt_template": "Is it urgent?",
                    "output_key": "classify", "output_fields": [{"name": "urgent", "type": "boolean"},
                                                                {"name": "team", "type": "choice",
                                                                 "options": ["Billing", "Tech"]}]}
        assert len(_warned(_orch(classify, _gate("state.classify.urgent == 'true'")))) == 1
        assert len(_warned(_orch(classify, _gate("state.classify.team == 'billing'")))) == 1
        assert len(_warned(_orch(classify, _gate("state.classify")))) == 1

    @pytest.mark.parametrize("condition", [
        "state.review.approved", "state.review.approved == True", "not state.review.approved",
        "state.review.approved != False", "state.review.decision == 'Yes'", "'Later' == state.review.decision",
        "state.review.comment == 'ok'", "len(state.review) > 0", "state.review.approved and state.review.comment",
        "state.user_input == 'go'",
    ])
    def test_a_condition_that_follows_the_answer_is_not_warned(self, condition):
        assert _warned(_orch(REVIEW, _gate(condition))) == []

    def test_an_answer_with_no_fields_is_left_alone(self):
        """A Human step with no fields answers the person's words (F1549's to say), whose truth is that they typed."""
        bare = {**REVIEW, "human_fields": []}
        assert _warned(_orch(bare, _gate("state.review"))) == []
        assert _warned(_orch(bare, _gate("state.review == 'yes'"))) == []


# ── The editor ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _node(script: str):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH: the editor's If/Else picker is TypeScript")
    run = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e",
                          f"import * as F from {json.dumps(str(FIELDS_TS))};\nconsole.log(JSON.stringify({script}));"],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


def _choices(steps: list[dict], self_id: str = "gate") -> list[dict]:
    return _node(f"F.branchFields({json.dumps(steps)}, {json.dumps(self_id)})")


def _condition(choice: dict, literal: str) -> str:
    return _node(f"F.branchCondition({json.dumps(choice)}, {json.dumps(literal)})")


class TestTheIfElsePicker:
    def test_it_offers_each_yes_no_and_one_of_with_their_values(self):
        choices = _choices([REVIEW, _gate("")])
        assert [(c["key"], c["field"], c["kind"]) for c in choices] == [("review", "approved", "boolean"),
                                                                       ("review", "decision", "choice")]
        approved, decision = choices
        assert approved["label"] == "Review › Approve the plan?" and decision["label"] == "Review › Decision"
        assert [(v["label"], v["value"]) for v in approved["values"]] == [("Yes", "True"), ("No", "False")]
        assert [(v["label"], v["value"]) for v in decision["values"]] == [("Yes", '"Yes"'), ("No", '"No"'),
                                                                         ("Later", '"Later"')]

    def test_a_model_steps_answer_fields_and_a_packs_key_too(self):
        classify = {"id": "classify", "name": "Classify", "type": "llm", "output_key": "classify",
                    "output_fields": [{"name": "urgent", "type": "boolean"}, {"name": "why", "type": "string"}]}
        packed = {"id": "pr", "name": "PR review", "type": "human",
                  "human_fields": [{"key": "pr_decision", "label": "Decision", "type": "select",
                                    "options": ["approve", "request_changes"]}]}
        got = [(c["key"], c["field"]) for c in _choices([classify, packed, _gate("")])]
        assert got == [("classify", "urgent"), ("human_response", "pr_decision")]

    def test_it_writes_the_condition_and_reads_it_back(self):
        approved, decision = _choices([REVIEW, _gate("")])
        assert _condition(approved, "True") == "state.review.approved == True"
        assert _condition(approved, "False") == "state.review.approved == False"
        assert _condition(decision, '"Later"') == 'state.review.decision == "Later"'
        for written, field, literal in [("state.review.approved == True", "approved", "True"),
                                        ("  state.review.decision=='No' ", "decision", '"No"'),
                                        ('state.review.decision == "Later"', "decision", '"Later"')]:
            picked = _node(f"F.branchPicked({json.dumps(written)}, F.branchFields({json.dumps([REVIEW])}))")
            assert (picked["field"]["field"], picked["literal"]) == (field, literal), written
        assert _node(f"F.branchPicked('state.review.approved', F.branchFields({json.dumps([REVIEW])}))") is None

    @pytest.mark.parametrize("pick, answer, arm", [
        ((0, "True"), {"approved": True}, "true"), ((0, "True"), {"approved": False}, "false"),
        ((0, "False"), {"approved": False}, "true"), ((0, "False"), {"approved": True}, "false"),
        ((1, '"Later"'), {"decision": "later"}, "true"), ((1, '"Later"'), {"decision": "Yes"}, "false"),
    ])
    async def test_what_it_writes_routes_each_value_its_way(self, pick, answer, arm):
        condition = _condition(_choices([REVIEW, _gate("")])[pick[0]], pick[1])
        assert _warned(_orch(REVIEW, _gate(condition))) == []
        name = re.sub(r"\W", "", f"{pick}{answer}").lower()
        orch = _orch(REVIEW, _gate(condition), orch_id=f"orch_f1558_pick_{name}")
        assert await _arm(orch, {"approved": False, "decision": "No", "comment": "", **answer}) == arm


def _server_verdicts(orch: dict) -> set[tuple]:
    out = set()
    for said in _warned(orch):
        if m := re.match(r"Step '(\w+)' compares state\.(\w+)\.(\w+), a yes/no, with '(.*?)':", said):
            out.add((m[1], "yes_no_word", m[2], m[3], m[4]))
        elif m := re.match(r"Step '(\w+)' compares state\.(\w+)\.(\w+), one of .*?, with '(.*?)':", said):
            out.add((m[1], "not_an_option", m[2], m[3], m[4]))
        elif m := re.match(r"Step '(\w+)' decides on state\.(\w+) whole", said):
            out.add((m[1], "whole", m[2], None, None))
    return out


EDITOR_CASES = ["state.review.approved == 'Yes'", 'state.review.approved != "no"', "'yes' == state.review.approved",
                "state.review.decision == 'yes'", "state.review.decision == 'Yes'", "state.review", "not state.review",
                "state.review == True", "state['review'] != False", "{{ state.review }}", "state.review.approved",
                "state.review.comment == 'ok'", "len(state.review) > 0", "state.review.approved == True"]


class TestTheEditorSaysWhatTheServerSays:
    @pytest.mark.parametrize("condition", EDITOR_CASES)
    def test_the_same_verdicts(self, condition):
        orch = _orch(REVIEW, _gate(condition))
        editor = _node(f"F.answerValueChecks({json.dumps(orch['steps'])})")
        got = {(v["stepId"], v["kind"], v["key"], v.get("field"), v.get("word")) for v in editor}
        assert got == _server_verdicts(orch), condition
        for v in editor:
            assert v["message"] and "It runs as it is today" in v["message"], v

    def test_each_is_wired(self):
        src = ROOT / "frontend/src/components/orchestration"
        validate = (src / "validate.ts").read_text()
        start = validate.index("answerValueChecks(orch.steps")
        assert "warning(" in validate[start:start + 300], validate[start:start + 300]
        steps = (src / "panel/steps.tsx").read_text()
        section = steps[steps.index("function IfElseSection"):steps.index("function SwitchSection")]
        assert "<AnswerBranch" in section, "the If/Else offers the picker"
        picker = steps[steps.index("function AnswerBranch"):]
        assert "branchFields(" in picker and "branchCondition(" in picker and "branchPicked(" in picker
