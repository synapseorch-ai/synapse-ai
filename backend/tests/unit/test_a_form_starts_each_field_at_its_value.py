"""The app's forms start each field at the value the run asked it with, and the step editor keeps that value (F1753).

Every form that answers a Human step (the run page and Home's Needs-you card, the chat's card, the editor's run panel)
starts from `initialHumanValues`, which started every text field at "" and every Yes / No off: approved as shown, a
field the card meant to offer as the draft sent a blank. It now starts a field at its `default`, the value the run
recorded when it asked, so a field nobody touched sends what was shown, and a field the person cleared sends "".

The step editor keeps a field's starting value through every edit of the field (its name, type, label, options or
approval) and through reading the saved step back, whether or not the person touched it; the editor sets it
("Starts with") and clears it, and a cleared one is left out rather than kept as "".
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_a_plan_card_draws_a_step_by_what_it_does import HOOKS

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "frontend/src"
FIELDS = "components/orchestration/humanFields.ts"


@pytest.fixture(scope="module")
def ts(tmp_path_factory):
    """`ts(module, expression)`: the expression's value, with `M` the module (a path under frontend/src)."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the forms are TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(module: str, expression: str):
        program = (f"import * as M from {json.dumps((SRC / module).as_uri())};\n"
                   f"console.log(JSON.stringify({expression}));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


ASKED = [
    {"name": "approved", "type": "boolean", "label": "Send this email?"},
    {"name": "to", "type": "email", "label": "To", "default": "robin@cedarline.example", "required": True},
    {"name": "count", "type": "number", "label": "Bags", "default": 4},
    {"name": "urgent", "type": "boolean", "label": "Urgent?", "default": True},
    {"name": "why", "type": "string", "label": "Why"},
]


class TestTheFormStartsAtTheValue:
    def test_each_field_starts_at_its_value_and_one_with_none_as_before(self, ts):
        assert ts(FIELDS, f"M.initialHumanValues({json.dumps(ASKED)})") == {
            "approved": False, "to": "robin@cedarline.example", "count": "4", "urgent": True, "why": ""}

    def test_untouched_it_sends_what_was_shown_and_cleared_it_sends_blank(self, ts):
        untouched = ts(FIELDS, f"M.humanAnswer({json.dumps(ASKED)}, M.initialHumanValues({json.dumps(ASKED)}))")
        assert untouched == {"approved": False, "to": "robin@cedarline.example", "count": 4, "urgent": True,
                             "why": ""}
        cleared = ts(FIELDS, f"M.humanAnswer({json.dumps(ASKED)}, {{...M.initialHumanValues({json.dumps(ASKED)}), "
                             "to: ''})")
        assert cleared["to"] == ""


STEP_FIELDS = [
    {"name": "approved", "type": "boolean", "label": "Send this email?"},
    {"name": "to", "type": "email", "label": "To", "default": "{{state.draft.to}}"},
]


class TestTheEditorKeepsIt:
    @pytest.mark.parametrize("patch", [{"label": "Recipient"}, {"name": "recipient"}, {"type": "string"},
                                       {"options": ["a"]}, {}])
    def test_through_every_edit_of_the_field(self, ts, patch):
        got = ts(FIELDS, f"M.humanFieldWith({json.dumps(STEP_FIELDS)}, 1, {json.dumps(patch)})")
        assert got[1]["default"] == "{{state.draft.to}}" and got[0] == STEP_FIELDS[0]

    def test_it_is_set_and_a_cleared_one_is_left_out(self, ts):
        set_ = ts(FIELDS, f"M.humanFieldWith({json.dumps(STEP_FIELDS)}, 0, {{default: 'yes'}})")
        assert set_[0]["default"] == "yes"
        cleared = ts(FIELDS, f"M.humanFieldWith({json.dumps(STEP_FIELDS)}, 1, {{default: ''}})")
        assert "default" not in cleared[1]

    def test_through_reading_the_saved_step_back(self, ts):
        doc = {"id": "o", "name": "Welcomes", "steps": [
            {"id": "review", "name": "Review", "type": "human", "human_prompt": "Review it", "human_fields": STEP_FIELDS}]}
        got = ts("components/orchestration/readOrchestration.ts",
                 f"M.readOrchestration({json.dumps(doc)}).orchestration.steps[0].human_fields")
        assert got == STEP_FIELDS

    def test_the_editor_writes_every_field_edit_through_it(self):
        """Each of the editor's field controls goes through `humanFieldWith`, so none rebuilds a field without it."""
        steps = (SRC / "components/orchestration/panel/steps.tsx").read_text()
        start = steps.index("Structured fields")
        block = steps[start:steps.index("Add field", start)]
        assert "next[idx] = {" not in block, "a field rebuilt by hand drops what it does not name"
        assert block.count("humanFieldWith(") >= 6, "name, type, label, approval, options and its starting value"
        assert "Starts with" in block
