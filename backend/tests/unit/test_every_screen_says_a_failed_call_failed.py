"""Every screen that shows an agent's call says a call that failed failed, and never shows the line written for the
agent (F1752).

F1751 marked an agent's failed call `failed`, with its reason, and the chat's tool chip read the mark. The other
screens that show a call did not: a helper's lane and a team member's (they read only `refused`), the agent test panel
(it read the result's first word, which "Tool 'X' FAILED: …" does not match), a phone call's log, a run's log and the
builder's trail. There a failed call read as one that worked, and its result was the line its agent was told ("…tell
the person it failed, and never say it was sent or done"): an instruction to the agent, not words for the person.

Now one reading (`lib/callMarks.ts`) serves them all: the mark says what happened; a call with no mark at all (a
record saved before the mark existed) is read by how the engine's failures used to start ("Error…"), and that only.
A call marked failed shows "failed — why" and never its result; an older failure keeps its own words, its only reason.
The builder's trail reads only the `failed` mark: its own answers and refusals read as they always have.

Near misses: a call not run stays "not run", never failed; a result that only contains the word FAILED, and a marked
call whose words start "Error", read by their marks; a call that may have gone through says its reason like any other.
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

FAILED_LINE = ("Tool 'send_note' FAILED: HTTP 405: Example Domain. It did not do what was asked: tell the person it "
               "failed, and never say it was sent or done.")
MAYBE_LINE = ("Tool 'send_note' FAILED: HTTP 502: Bad gateway. It may or may not have gone through: tell the person "
              "you couldn't confirm it, and never say it was sent or done.")
FAILED_EVENT = {"type": "tool_result", "tool_name": "send_note", "preview": FAILED_LINE, "failed": True,
                "reason": "HTTP 405: Example Domain"}
OLD_ERROR = {"type": "tool_result", "tool_name": "run_sql_query", "preview": "Error: no such table"}
REFUSED_EVENT = {"type": "tool_result", "tool_name": "send_note", "preview": "Error: tool not found", "refused": True,
                 "reason": "tool not found"}
WORDED = {"type": "tool_result", "tool_name": "read_log", "preview": "3 lines; the last says the import FAILED twice"}


@pytest.fixture(scope="module")
def ts(tmp_path_factory):
    """`ts(module, expression)`: the expression's value, with `M` the module (a path under frontend/src)."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not on PATH: the screens' reading is TypeScript")
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


def _j(value) -> str:
    return json.dumps(value)


class TestOneReading:
    def test_the_mark_says_failed(self, ts):
        call = {"tool": "send_note", "result": FAILED_LINE, "failed": True, "reason": "HTTP 405: Example Domain"}
        assert ts("lib/callMarks.ts", f"[M.isFailed({_j(call)}), M.callNote({_j(call)}), M.shownResult({_j(call)})]") \
            == [True, "failed — HTTP 405: Example Domain", ""]

    def test_a_call_with_no_mark_is_read_by_how_failures_used_to_start(self, ts):
        call = {"tool": "run_sql_query", "result": "Error: no such table"}
        assert ts("lib/callMarks.ts", f"[M.isFailed({_j(call)}), M.shownResult({_j(call)}), M.resultLine({_j(call)})]") \
            == [True, "Error: no such table", {"text": "Error: no such table", "marked": True}]

    def test_a_marked_failure_reads_its_why_in_a_trail_never_its_line(self, ts):
        call = {"tool": "send_note", "result": FAILED_LINE, "failed": True, "reason": "HTTP 405: Example Domain"}
        assert ts("lib/callMarks.ts", f"M.resultLine({_j(call)})") == {
            "text": "failed — HTTP 405: Example Domain", "marked": True}

    def test_a_maybe_ran_failure_says_its_reason_like_any_other(self, ts):
        call = {"tool": "send_note", "result": MAYBE_LINE, "failed": True, "reason": "HTTP 502: Bad gateway"}
        assert ts("lib/callMarks.ts", f"[M.callNote({_j(call)}), M.shownResult({_j(call)})]") \
            == ["failed — HTTP 502: Bad gateway", ""]

    def test_a_call_not_run_is_never_failed(self, ts):
        call = {"tool": "send_note", "result": "Error: tool not found", "refused": True, "reason": "tool not found"}
        assert ts("lib/callMarks.ts", f"[M.isFailed({_j(call)}), M.callNote({_j(call)})]") \
            == [False, "not run — tool not found"]

    def test_words_that_only_contain_failed_are_a_result(self, ts):
        call = {"tool": "read_log", "result": WORDED["preview"]}
        assert ts("lib/callMarks.ts", f"[M.isFailed({_j(call)}), M.callNote({_j(call)})]") == [False, None]

    def test_a_call_marked_as_working_is_not_read_by_its_words(self, ts):
        call = {"tool": "read_log", "result": "Error rate this week: 2%", "failed": False}
        assert ts("lib/callMarks.ts", f"M.isFailed({_j(call)})") is False


class TestTheTestPanel:
    def _fold(self, ts, *events):
        steps = " ".join(f"t = M.reduceTest(t, {_j(e)});" for e in events)
        return ts("components/assistant/testChat.ts",
                  f"(() => {{ let t = M.newTurn('t1', 'q'); {steps} return t.calls; }})()")

    def test_a_marked_failure_is_failed_with_why(self, ts):
        calls = self._fold(ts, {"type": "tool_execution", "tool_name": "send_note"}, FAILED_EVENT)
        assert calls[0]["failed"] is True and calls[0]["reason"] == "HTTP 405: Example Domain"

    def test_an_older_error_is_failed_by_its_words(self, ts):
        calls = self._fold(ts, {"type": "tool_execution", "tool_name": "run_sql_query"}, OLD_ERROR)
        assert calls[0]["failed"] is True and "reason" not in calls[0]

    def test_a_call_that_ran_is_not_failed(self, ts):
        calls = self._fold(ts, {"type": "tool_execution", "tool_name": "read_log"}, WORDED)
        assert calls[0]["failed"] is False

    def test_a_refused_call_is_not_run(self, ts):
        calls = self._fold(ts, {"type": "tool_execution", "tool_name": "send_note"}, REFUSED_EVENT)
        assert calls[0].get("refused") is True and not calls[0].get("failed")


class TestALane:
    """A helper's lane and a team member's: one fold, `foldLaneEvent`, and one reading of a saved record."""

    def test_a_live_failure_is_marked_with_why(self, ts):
        lane = {"calls": [{"tool": "send_note", "result": ""}]}
        call = ts("components/assistant/helpers.ts", f"M.foldLaneEvent({_j(lane)}, {_j(FAILED_EVENT)}).calls[0]")
        assert call["failed"] is True and call["reason"] == "HTTP 405: Example Domain"

    def test_a_saved_failure_is_marked_with_why(self, ts):
        record = {"id": "h1", "calls": [{"tool": "send_note", "result": FAILED_LINE, "failed": True,
                                         "reason": "HTTP 405: Example Domain"}]}
        call = ts("components/assistant/helpers.ts", f"M.laneFromRecord({_j(record)}).calls[0]")
        assert call["failed"] is True and call["reason"] == "HTTP 405: Example Domain"

    def test_a_refused_call_stays_not_run(self, ts):
        lane = {"calls": [{"tool": "send_note", "result": ""}]}
        call = ts("components/assistant/helpers.ts", f"M.foldLaneEvent({_j(lane)}, {_j(REFUSED_EVENT)}).calls[0]")
        assert call.get("refused") is True and not call.get("failed")

    def test_a_call_that_ran_has_no_mark(self, ts):
        lane = {"calls": [{"tool": "read_log", "result": ""}]}
        call = ts("components/assistant/helpers.ts", f"M.foldLaneEvent({_j(lane)}, {_j(WORDED)}).calls[0]")
        assert "failed" not in call and "refused" not in call and call["result"] == WORDED["preview"]


class TestAPhoneCallsLog:
    def _tools(self, ts, *calls):
        messages = [{"role": "user", "content": "Is Tuesday's order out?"},
                    {"role": "assistant", "content": "It is.", "calls": list(calls)}]
        return ts("components/phone/phone.ts", f"M.turnsFromMessages({_j(messages)})[0].tools")

    def test_a_saved_failure_is_failed_with_why(self, ts):
        [tool] = self._tools(ts, {"tool": "send_note", "result": FAILED_LINE, "failed": True,
                                  "reason": "HTTP 405: Example Domain"})
        assert tool["failed"] is True and tool["reason"] == "HTTP 405: Example Domain"

    def test_an_older_error_is_failed_by_its_words(self, ts):
        [tool] = self._tools(ts, {"tool": "run_sql_query", "result": "Error: no such table"})
        assert tool["failed"] is True

    def test_a_call_that_ran_and_one_not_run_are_not_failed(self, ts):
        ran, refused = self._tools(ts, {"tool": "read_log", "result": WORDED["preview"]},
                                   {"tool": "send_note", "result": "", "refused": True, "reason": "tool not found"})
        assert ran["failed"] is False and refused["failed"] is False and refused["refused"] is True


class TestTheBuildersTrail:
    """The builder's trail reads only the `failed` mark: its own answers and refusals read as they always have."""

    def _line(self, ts, event):
        state = ts("lib/builderEvents.ts", f"M.applyBuilderEvent(M.emptyBuilderState(), {_j(event)}).messages")
        return [m for m in state if m.get("kind") == "tool_result"][0]["toolPreview"]

    def test_a_marked_failure_reads_its_why_never_its_line(self, ts):
        assert self._line(ts, FAILED_EVENT) == "failed — HTTP 405: Example Domain"

    def test_an_unmarked_error_reads_as_before(self, ts):
        assert self._line(ts, OLD_ERROR) == "Error: no such table"

    def test_a_refusal_reads_as_before(self, ts):
        assert self._line(ts, REFUSED_EVENT) == "Error: tool not found"


SCREENS = {
    "the chat's chip": "app/(app)/page.tsx",
    "a helper's lane": "components/assistant/HelperLane.tsx",
    "a member's lane": "components/team/TeamLanes.tsx",
    "the test panel": "components/assistant/AgentTestPanel.tsx",
    "a phone call's log": "components/phone/CallLog.tsx",
    "a run's log": "components/run/RunLogView.tsx",
}


@pytest.mark.parametrize("screen", sorted(SCREENS))
def test_each_screen_reads_its_calls_through_the_one_reading(screen):
    source = (SRC / SCREENS[screen]).read_text()
    assert "@/lib/callMarks" in source, f"{screen} does not read its calls through lib/callMarks.ts"
    assert "shownResult" in source or "resultLine" in source, f"{screen} may show a failed call's line"
    assert "/^(error|blocked" not in source, f"{screen} reads failures by their words itself"


def test_a_runs_log_keeps_each_results_mark():
    source = (SRC / "components/run/RunLogView.tsx").read_text()
    case = source[source.index('case "tool_result":'):][:600]
    assert "data.failed" in case and "data.refused" in case and "data.reason" in case
