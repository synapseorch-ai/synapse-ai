"""The import starter's words stay true before and after OpenAI Agent Builder closes (F1636).

The import starter is the panel's line for someone moving a workflow out of Agent Builder. Sent with nothing under it,
the reply (`export.EXPORT_MISSING`) and the panel's note (switch.ts's `IMPORT_LEAD_NOTE`) told the person to open the
workflow in Agent Builder and export it: a step nobody can take once Agent Builder is gone. Now both offer the two ways
in that stay true: the workflow's Agents SDK code, an export from Agent Builder or a copy the person saved, or the
workflow described in the person's own words. Neither names a step that needs Agent Builder open, nor a date or a
clock, so neither goes stale. The browser check holds the panel's note word for word.

Near misses: the words still name the Agents SDK and Agent Builder (a saved export is one), and the reply still says
that nothing was pasted under the line, the only time it is said.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.native_builder import export

ROOT = Path(__file__).resolve().parents[3]


def _note() -> str:
    """switch.ts's IMPORT_LEAD_NOTE, as the panel shows it."""
    source = (ROOT / "frontend/src/components/assistant/switch.ts").read_text()
    m = re.search(r"export const IMPORT_LEAD_NOTE\s*=\s*'((?:\\.|[^'\\])*)'\s*;", source)
    assert m, "IMPORT_LEAD_NOTE is one string in switch.ts"
    return m.group(1).replace("\\'", "'")


def _smoke_note() -> str:
    """ui-smoke's F1607_NOTE: the words the browser check holds the panel to."""
    tree = ast.parse((ROOT / "scripts/ui-smoke.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "F1607_NOTE" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("ui-smoke has no F1607_NOTE")


LINES = {"the reply": lambda: export.EXPORT_MISSING, "the panel's note": _note}

#: A step that needs Agent Builder open.
_IN_AGENT_BUILDER = re.compile(r"open the workflow|in openai agent builder,|in agent builder,", re.I)
#: A date or a clock: a month, a year, or words that hold until a day.
_DATED = re.compile(r"\b(?:January|February|March|April|June|July|August|September|October|November|December|May \d)\b"
                    r"|\b20\d\d\b|\b(?:shuts?|shut down|closes|closed|closing|until|before)\b", re.I)


@pytest.mark.parametrize("which", LINES)
class TestBothWaysInThatStayTrue:
    def test_an_agents_sdk_export_from_agent_builder_or_a_saved_copy(self, which):
        said = LINES[which]()
        assert "Agents SDK" in said and "Agent Builder" in said, said
        assert re.search(r"\bsaved\b", said), said

    def test_the_workflow_in_the_persons_own_words(self, which):
        said = LINES[which]()
        assert "your own words" in said, said


@pytest.mark.parametrize("which", LINES)
class TestNothingThatGoesStale:
    def test_no_step_that_needs_agent_builder_open(self, which):
        said = LINES[which]()
        assert not _IN_AGENT_BUILDER.search(said), said

    def test_no_date_or_clock(self, which):
        said = LINES[which]()
        assert not _DATED.search(said), said


class TestTheReplyIsStillTheReply:
    def test_it_says_nothing_was_pasted(self):
        assert export.EXPORT_MISSING.startswith("Nothing was pasted under the line"), export.EXPORT_MISSING


class TestTheBrowserCheckHoldsThePanelsWords:
    def test_ui_smokes_note_is_the_panels(self):
        assert _smoke_note() == _note()
