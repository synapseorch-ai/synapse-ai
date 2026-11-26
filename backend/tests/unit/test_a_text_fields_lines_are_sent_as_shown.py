"""A text field whose starting value has line breaks is drawn as a box of lines, so approved as shown it sends them
(F1759).

A Human step's Text field can start with a value from the run (F1753): a draft's body, written by an earlier step in
paragraphs. Every answer form (the run page and Home's card, the chat's card, Synapse AI's card, the editor's run
panel) draws its fields with `HumanFieldsForm`, which drew a Text field as a one-line input; a browser's one-line input
drops the line breaks of the value it is given, so a draft approved as shown, untouched, was sent as one run-on line.
Only a field typed Long text kept them.

Now a Text field whose starting value has a line break is drawn as a box of lines, as a Long text is
(`humanFieldDrawsLines`), and keeps its breaks shown, sent untouched, and edited. The engine keeps them too: a text
answer is stored as it came, and a field left out takes its starting value as the run recorded it.

Near misses: a Text field with a one-line starting value, or none, is still a one-line input; a Long text is a box of
lines whatever it starts with; an email, a number or a date is never one.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from tests.unit import test_a_form_starts_each_field_at_its_value as forms

ROOT = Path(__file__).resolve().parents[3]
FIELDS = "components/orchestration/humanFields.ts"
FORM = ROOT / "frontend/src/components/orchestration/HumanFieldsForm.tsx"

#: `ts(module, expression)`, the forms' node fixture: the expression's value in node, with `M` the module.
ts = forms.ts

BODY = "Hello Harbor & Pine,\n\nYour order ships on Monday.\nThanks for your patience."
DRAFT = [
    {"name": "approved", "type": "boolean", "label": "Send this email?"},
    {"name": "to", "type": "email", "label": "To", "default": "orders@example.com"},
    {"name": "subject", "type": "text", "label": "Subject", "default": "Ships Monday"},
    {"name": "body", "type": "text", "label": "Body", "default": BODY},
]


def _draws_lines(ts, field: dict) -> bool:
    return ts(FIELDS, f"M.humanFieldDrawsLines({json.dumps(field)})")


class TestTheFormDrawsALineBreakAsALine:
    @pytest.mark.parametrize("field", [
        pytest.param({"name": "body", "type": "text", "default": BODY}, id="text"),
        pytest.param({"name": "body", "type": "string", "default": BODY}, id="string"),
        pytest.param({"name": "body", "default": BODY}, id="no type"),
        pytest.param({"name": "body", "type": "text", "default": "Dear team,\r\nThanks."}, id="a CRLF break"),
    ])
    def test_a_text_field_starting_with_lines_is_a_box_of_lines(self, ts, field):
        assert _draws_lines(ts, field) is True

    @pytest.mark.parametrize("field, lines", [
        pytest.param({"name": "s", "type": "text", "default": "Ships Monday"}, False, id="one line"),
        pytest.param({"name": "s", "type": "text"}, False, id="no starting value"),
        pytest.param({"name": "s", "type": "text", "default": 12}, False, id="a number as its start"),
        pytest.param({"name": "s", "type": "textarea"}, True, id="a long text with nothing"),
        pytest.param({"name": "s", "type": "long_text", "default": "One line"}, True, id="a long text with one line"),
        pytest.param({"name": "s", "type": "email", "default": "a@example.com"}, False, id="an email"),
        pytest.param({"name": "s", "type": "number", "default": 4}, False, id="a number"),
        pytest.param({"name": "s", "type": "date", "default": "2026-12-01"}, False, id="a date"),
    ])
    def test_the_near_misses_keep_their_control(self, ts, field, lines):
        assert _draws_lines(ts, field) is lines

    def test_the_form_draws_a_box_of_lines_by_it(self):
        source = FORM.read_text(encoding="utf-8")
        assert "humanFieldDrawsLines(field)" in source, "the form decides a box of lines by its own test"
        assert not re.search(r"type\s*===\s*'textarea'\s*\?", source), \
            "a Long text's type alone still decides the box: a Text field with lines is drawn on one line"

    def test_untouched_it_sends_the_lines_it_showed(self, ts):
        sent = ts(FIELDS, f"M.humanAnswer({json.dumps(DRAFT)}, M.initialHumanValues({json.dumps(DRAFT)}))")
        assert sent["body"] == BODY
        assert sent["subject"] == "Ships Monday"

    def test_edited_it_sends_the_lines_typed(self, ts):
        edited = "Hello Harbor & Pine,\n\nIt ships on Tuesday now."
        sent = ts(FIELDS, f"M.humanAnswer({json.dumps(DRAFT)}, {{...M.initialHumanValues({json.dumps(DRAFT)}), "
                          f"body: {json.dumps(edited)}}})")
        assert sent["body"] == edited


class TestTheEngineKeepsTheLines:
    def test_a_field_starts_with_its_lines(self):
        from core.orchestration.steps import shown_fields

        [field] = shown_fields([{"name": "body", "type": "text", "default": "{{state.draft.body}}"}],
                               {"draft": {"body": BODY}})
        assert field["default"] == BODY

    def test_left_out_it_takes_its_lines(self):
        from core.orchestration.human_fields import check_answer

        assert check_answer(DRAFT, {"approved": True})["body"] == BODY

    def test_sent_it_keeps_them(self):
        from core.orchestration.human_fields import check_answer

        edited = "Hello Harbor & Pine,\n\nIt ships on Tuesday now.\n"
        assert check_answer(DRAFT, {"approved": True, "body": edited})["body"] == edited
