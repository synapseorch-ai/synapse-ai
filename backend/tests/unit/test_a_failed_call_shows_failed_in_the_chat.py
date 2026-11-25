"""A chat turn's tool chip says a call that failed, as it says one that was not run (F1751).

An agent's call whose tool said it failed (an HTTP 4xx or 5xx, an MCP error, a script's exit) is marked `failed`, with
its reason, on its `tool_result` event and on the call its turn saves. The chip read only `refused`, so a failed call
sat in "Tools used" like one that worked, under an answer that may say it was sent. Now the chip's label counts the
failed calls and each one says "failed — <why>", live (from its event) and reopened (from its saved call).

Near misses read as before: a call that ran reads with no word beside it, a refused call says "not run", and an event
with neither mark adds neither.
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
MARKS = SRC / "lib/callMarks.ts"


@pytest.fixture(scope="module")
def marks(tmp_path_factory):
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the chip's reading is TypeScript")
    where = tmp_path_factory.mktemp("node-hooks")
    (where / "hooks.mjs").write_text(HOOKS % json.dumps(str(SRC)))
    (where / "register.mjs").write_text(
        "import { register } from 'node:module';\n"
        f"register({json.dumps((where / 'hooks.mjs').as_uri())});\n")

    def run(expression: str):
        program = (f"import * as M from {json.dumps(MARKS.as_uri())};\n"
                   f"console.log(JSON.stringify({expression}));")
        done = subprocess.run([exe, "--experimental-strip-types", "--no-warnings", "--import",
                               (where / "register.mjs").as_uri(), "--input-type=module", "-e", program],
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout)
    return run


FAILED_EVENT = {"type": "tool_result", "tool_name": "send_note", "failed": True, "reason": "HTTP 405: Example Domain",
                "preview": "Tool 'send_note' FAILED: HTTP 405: Example Domain."}
FAILED = {"tool": "send_note", "result": "Tool 'send_note' FAILED: HTTP 405: Example Domain.", "failed": True,
          "reason": "HTTP 405: Example Domain"}
RAN = {"tool": "read_stock", "result": '{"count": 12}'}
REFUSED = {"tool": "wipe_orders", "result": "", "refused": True, "reason": "the person refused it"}


class TestAFailedCall:
    def test_a_live_event_marks_its_call_failed_with_why(self, marks):
        assert marks(f"M.resultOf({json.dumps(FAILED_EVENT)})") == {
            "result": FAILED_EVENT["preview"], "failed": True, "reason": "HTTP 405: Example Domain"}

    def test_the_chip_says_failed_beside_it(self, marks):
        assert marks(f"M.callNote({json.dumps(FAILED)})") == "failed — HTTP 405: Example Domain"

    def test_the_chips_label_counts_it(self, marks):
        assert marks(f"M.tallyOf({json.dumps([FAILED, RAN, REFUSED])})") == {"notRun": 1, "failed": 1}


class TestNearMisses:
    def test_a_call_that_ran_has_no_word(self, marks):
        assert marks(f"M.callNote({json.dumps(RAN)})") is None

    def test_a_refused_call_says_not_run(self, marks):
        assert marks(f"M.callNote({json.dumps(REFUSED)})") == "not run — the person refused it"

    def test_an_event_with_no_mark_adds_none(self, marks):
        assert marks("M.resultOf({preview: 'ok'})") == {"result": "ok"}
