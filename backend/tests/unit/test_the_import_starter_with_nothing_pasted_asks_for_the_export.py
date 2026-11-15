"""The import starter sent with nothing pasted under it says so and asks for the export; it never rebuilds a workflow
that is already here (F1607).

A browser test of the panel: a tester's turn on prod and T4 sent our
own starter, "Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:", with nothing
under it. With no code, `export.read_export` saw no export and the message went on like any other: the target
resolver could take the open workflow, the conversation's last build or one named by a word in the line itself, and
T4 built "Supplier Invoice Review & Payment Approval 2", a copy of a workflow already there; on OSS (F1600's after-
measure) it built "a general-purpose agent … mirroring a typical OpenAI Agents SDK export".

Now:
- The lead line with nothing under it (`export.lead_only`) is answered in code before anything else
  (`read_turn`'s export_missing): nothing was pasted, and where the export is copied from; no plan, no workflow.
- Whenever the lead line is there (`export.lead_present`), the route resolves no target: "this workflow" is the
  paste's, never one here, and no word of the line names one.
- The lead line with code under it is read as it was (F1550's tests).
- The panel holds back only the exact bare lead line; text that is not code under it goes to the server, whose
  `lead_only` decides.
- The backend's line is the panel's, word for word.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.native_builder import export, read_turn

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
LEAD = "Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:"
PY_EXPORT = '''from agents import Agent, Runner

triage = Agent(name="Triage", instructions="Sort the tickets.")
'''


class TestTheLineAlone:
    @pytest.mark.parametrize("message", [LEAD, LEAD + "\n\n", "  " + LEAD + "  \n"])
    def test_it_is_the_lead_only(self, message):
        assert export.lead_only(message)

    @pytest.mark.parametrize("message", [LEAD + "\n\n" + PY_EXPORT, LEAD + "\n\nA bot that sorts our tickets.",
                                         "Rebuild the weekly report", ""])
    def test_with_anything_under_it_or_another_message_it_is_not(self, message):
        assert not export.lead_only(message)

    @pytest.mark.parametrize("message", [LEAD, LEAD + "\n\nA bot that sorts our tickets.", LEAD + "\n\n" + PY_EXPORT])
    def test_the_line_is_there(self, message):
        assert export.lead_present(message)

    def test_the_backends_line_is_the_panels(self):
        switch = (SRC / "components/assistant/switch.ts").read_text()
        assert f"lead: '{export.IMPORT_LEAD}\\n\\n'," in switch


class TestItIsAnsweredInCode:
    def test_nothing_pasted_is_said_with_where_the_export_comes_from(self):
        turn = read_turn.decide(LEAD, previous={}, facts={})
        assert turn.route == "export_missing" and turn.then == "said"
        assert "nothing" in turn.say.lower() and "Code → Agents SDK" in turn.say

    def test_after_a_build_in_this_conversation_it_is_still_that(self):
        """T4's shape: the conversation had built the invoice workflow; the empty paste is never taken as it."""
        built = {"user_message": "Check each new supplier invoice…", "request": {"kind": "build"},
                 "_run_writes": [{"kind": "orchestration", "action": "created", "id": "orch_inv",
                                  "name": "Supplier Invoice Review & Payment Approval"}]}
        turn = read_turn.decide(LEAD, previous=built, facts={})
        assert turn.route == "export_missing" and "Supplier Invoice" not in turn.say


ROWS = [{"id": "orch_inv", "name": "Supplier Invoice Review & Payment Approval"},
        {"id": "orch_ab", "name": "OpenAI Agent Builder import"}]


class TestNoWorkflowHereIsItsTarget:
    @pytest.mark.parametrize("message", [LEAD, LEAD + "\n\nA bot that sorts our tickets.", LEAD + "\n\n" + PY_EXPORT])
    async def test_the_route_resolves_none(self, monkeypatch, message):
        from core.routes import builder as route
        from core.store import resources

        async def rows():
            return list(ROWS)
        monkeypatch.setattr(resources, "load_orchestrations", rows)
        monkeypatch.setattr("core.builder_tools._is_visible", lambda o: True)
        state = await route._target_state(message, "orch_inv", built_id="orch_inv")
        assert not state.get("current_orchestration_id") and state.get("current_orchestration_source") in ("", None)


class TestThePanel:
    def test_it_holds_back_only_the_bare_line(self):
        switch = (SRC / "components/assistant/switch.ts").read_text()
        panel = (SRC / "components/app/BuilderPanel.tsx").read_text()
        assert re.search(r"export function isBareImportLead\(text: string\): boolean", switch)
        assert "isBareImportLead(text)" in panel
