"""A Human step's email, date, phone and dropdown fields (F455, CLAIM2, P-7).

The FAQ said forms take email, date, phone and dropdown fields; a Human step
knew text, long text, a number and yes/no, and the engine refused a dropdown's
`options` outright. Naveen's answers (2026-10-12): one choice per dropdown; the
server checks the new types only, so nothing answered today is refused.
"""
from __future__ import annotations

import pytest

from core.orchestration.human_fields import BadAnswer, check_answer, field_type

EMAIL = {"name": "email", "type": "email", "label": "Work email"}
WHEN = {"name": "when", "type": "date", "label": "Start date"}
PHONE = {"name": "phone", "type": "phone", "label": "Phone"}
PICK = {"name": "pick", "type": "choice", "label": "Decision", "options": ["Approve", "Later"]}
COUNT = {"name": "count", "type": "number", "label": "How many"}


class TestTheTypeTable:
    @pytest.mark.parametrize("raw,kind", [
        ("email", "email"), ("date", "date"), ("phone", "phone"), ("choice", "choice"),
        ("select", "choice"), ("dropdown", "choice"), ("options", "choice"), ("enum", "choice"),
        ("tel", "phone"), ("Email", "email"),
        # The spellings that were there before, unchanged.
        ("text", "string"), ("long_text", "textarea"), ("bool", "boolean"), ("int", "number"),
        ("", "string"), (None, "string"),
    ])
    def test_every_spelling_reads_as_its_type(self, raw, kind):
        assert field_type(raw) == kind

    def test_a_type_it_does_not_know_is_none(self):
        assert field_type("colour") is None


class TestTheCheck:
    def test_good_answers_pass_and_a_choice_comes_back_as_written(self):
        answer = {"email": " ana@example.com ", "when": "2026-10-12", "phone": "+91 98765 43210",
                  "pick": "later"}
        assert check_answer([EMAIL, WHEN, PHONE, PICK], answer) == {
            "email": "ana@example.com", "when": "2026-10-12", "phone": "+91 98765 43210",
            "pick": "Later"}

    @pytest.mark.parametrize("field,value,says", [
        (EMAIL, "ana-at-example", "Work email"),
        (WHEN, "12/10/2026", "Start date"),
        (WHEN, "2026-02-30", "Start date"),
        (PHONE, "12", "Phone"),
        (PHONE, "call me", "Phone"),
        (PICK, "maybe", "Approve, Later"),
    ])
    def test_a_bad_answer_is_refused_naming_the_field(self, field, value, says):
        with pytest.raises(BadAnswer) as refused:
            check_answer([field], {field["name"]: value})
        assert says in str(refused.value)
        assert refused.value.field == field["name"]

    def test_a_field_left_blank_is_still_an_answer(self):
        """What a form sends for a field nobody filled in (F232's rule)."""
        assert check_answer([EMAIL, PICK], {"email": "", "pick": None}) == {"email": "", "pick": None}

    def test_the_old_types_are_not_checked(self):
        """Nothing answered today is refused: a number field given words passes as before."""
        assert check_answer([COUNT], {"count": "abc"}) == {"count": "abc"}

    def test_a_text_answer_is_not_checked(self):
        assert check_answer([EMAIL], "approve it") == "approve it"

    def test_a_dropdown_with_no_options_is_not_checked(self):
        assert check_answer([{"name": "pick", "type": "choice"}], {"pick": "x"}) == {"pick": "x"}


class TestTheSchemaHoldsOptions:
    def test_a_step_and_a_run_keep_a_dropdowns_options(self):
        from core.models_orchestration import OrchestrationRun, StepConfig

        step = StepConfig(id="ask", name="Ask", type="human", human_prompt="ok?", human_fields=[PICK])
        assert step.human_fields[0]["options"] == ["Approve", "Later"]
        run = OrchestrationRun(run_id="r", orchestration_id="o", human_fields=[PICK])
        assert run.human_fields[0]["options"] == ["Approve", "Later"]


class TestTheBuilderKnowsDropdowns:
    @staticmethod
    def _issues(fields):
        from core.builder_tools import _validate_orchestration
        return _validate_orchestration({
            "id": "o", "entry_step_id": "ask",
            "state_schema": {"review": {"type": "dict", "default": {}}},
            "steps": [
                {"id": "ask", "name": "Ask", "type": "human", "human_prompt": "ok?",
                 "human_fields": fields, "output_key": "review", "next_step_id": "end1"},
                {"id": "end1", "name": "Done", "type": "end"},
            ],
        })["issues"]

    def test_a_dropdown_with_options_raises_nothing(self):
        assert self._issues([PICK, EMAIL, WHEN, PHONE]) == []

    def test_a_dropdown_with_no_options_says_how_to_mend_it(self):
        [issue] = self._issues([{"name": "pick", "type": "select", "label": "Decision"}])
        assert "'pick'" in issue and '"options"' in issue


# F1434: the planner writes a yes/no field as `yes_no`, which neither table knew, so it was a text box and "yes"
# never met a condition that reads true/false (cap-05's shape). A yes/no answer arrives as true/false wherever it comes
# from — a form, the API, MCP, a channel's reply — and any other text is kept as it came: nothing is refused (F455).

OK = {"name": "ok", "type": "yes_no", "label": "Go ahead?"}


class TestYesNo:
    @pytest.mark.parametrize("raw", ["yes_no", "YES_NO", "yesno", "yes/no", " Yes_No "])
    def test_the_planners_spellings_are_the_yes_no_field(self, raw):
        assert field_type(raw) == "boolean"

    def test_the_browsers_table_reads_them_too(self):
        """`humanFields.ts` is this table's twin (the editor, the run's form, chat's answer form, cloud's run page)."""
        import re
        from pathlib import Path

        ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/orchestration/humanFields.ts").read_text()
        block = re.search(r"const ALIASES: Record<string, HumanFieldType> = \{(.*?)\};", ts, re.S).group(1)
        aliases = dict(re.findall(r"""['"]?([\w/]+)['"]?\s*:\s*'(\w+)'""", block))
        for raw in ("yes_no", "yesno", "yes/no"):
            assert aliases.get(raw) == "boolean", raw

    @pytest.mark.parametrize("said,flag", [
        ("Yes", True), ("yes.", True), (" y ", True), ("TRUE", True), ("1", True), (1, True),
        ("no", False), ("No!", False), ("n", False), ("false", False), ("0", False), (0, False),
    ])
    def test_a_yes_or_no_in_words_is_stored_as_the_flag(self, said, flag):
        assert check_answer([OK], {"ok": said})["ok"] is flag  # `is`: 1 == True would pass as is
        assert check_answer([{"name": "ok", "type": "boolean"}], {"ok": said})["ok"] is flag

    @pytest.mark.parametrize("said", [True, False, "maybe", "yes, but only on Friday", "", None, "on", "off"])
    def test_anything_else_is_kept_as_it_came(self, said):
        assert check_answer([OK], {"ok": said}) == {"ok": said}

    def test_a_text_reply_to_a_lone_yes_no_answers_it(self):
        """A channel's reply, or any door's plain text, to a step that asks one yes/no."""
        assert check_answer([OK], "Yes") == {"ok": True} and check_answer([OK], "Yes")["ok"] is True
        assert check_answer([OK], "no.") == {"ok": False} and check_answer([OK], "no.")["ok"] is False
        assert check_answer([{"name": "approved", "type": "boolean"}], "y") == {"approved": True}

    def test_other_text_and_other_steps_keep_the_text(self):
        assert check_answer([OK], "next tuesday") == "next tuesday"
        assert check_answer([OK, EMAIL], "yes") == "yes"
        assert check_answer([EMAIL], "yes") == "yes"
        assert check_answer([], "yes") == "yes"
