"""A new workflow that runs, calls or follows a workflow the message names is built new, open or not (F1673).

F1628 built new "Make a workflow that runs my 'X' workflow …" beside X open on screen, where the quick model read it as
a change to X. With nothing open, and X only named, the quick model's "change" stood, so the plan changed X (a sample
note in its form, a step to show what it decided) where the person asked for a new workflow that runs it: something
they own was changed when they asked for something new. Now F1628's words rule reads every workflow the message names:
a message that starts as a whole new job and names a workflow after a word that uses it (runs, calls, starts, triggers,
uses, after), after the new one is asked for, is a new build, planned from nothing, whatever is open.

Near misses: an edit of a named workflow stays a change; a message naming two workflows, one to change and one to run
("change X so it runs Y afterwards"), stays a change of X; the open case is F1628's as it was; another language keeps
the quick model's word; "every Monday run my X" schedules X and is not new.
"""
from __future__ import annotations

import pytest

from core.native_builder.classify import override
from core.native_builder.objects import Request

INVENTORY = {"schedule": [], "agent": [], "orchestration": ["Expense Submission & Approval", "Weekly Report"]}
P6 = ("Make a workflow that runs my 'Expense Submission & Approval' workflow for a sample expense of 120 for Office "
      "supplies, then shows me what it decided.")
NOTHING_OPEN = {"may_change": True, "may_act": True}
WEEKLY_OPEN = {**NOTHING_OPEN, "current_orchestration_source": "viewing", "current_orchestration_id": "orch_weekly",
               "current_orchestration_name": "Weekly Report"}
EXPENSE_OPEN = {**NOTHING_OPEN, "current_orchestration_source": "viewing", "current_orchestration_id": "orch_expense",
                "current_orchestration_name": "Expense Submission & Approval"}


def _route(message: str, kind: str, state: dict):
    return override(Request(kind=kind, needs="workspace"), message=message, state=dict(state), inventory=INVENTORY,
                    connectable=set())


class TestANewWorkflowThatUsesANamedOneIsNew:
    @pytest.mark.parametrize("message", [
        P6,
        "Create a new workflow that calls my 'Weekly Report' workflow and emails nobody: just show me its result.",
        "Build an automation that starts after the 'Weekly Report' workflow and sums up what it wrote.",
    ])
    def test_with_nothing_open(self, message):
        routed = _route(message, "change", NOTHING_OPEN)
        assert routed.kind == "build" and routed.overridden_by == "new_using_named", routed

    def test_with_another_workflow_open_it_is_planned_from_nothing(self):
        routed = _route(P6, "change", WEEKLY_OPEN)
        built_fresh = _route(P6, "build", NOTHING_OPEN)
        assert routed.kind == "build" and routed.route == built_fresh.route, (routed, built_fresh)


class TestNearMisses:
    @pytest.mark.parametrize("message", [
        "Change my 'Expense Submission & Approval' workflow to ask for a receipt.",
        "Change 'Expense Submission & Approval' so it runs 'Weekly Report' afterwards.",
        "Every Monday run my 'Expense Submission & Approval' workflow.",
        "Crea un flujo de trabajo que ejecute mi flujo 'Expense Submission & Approval'.",
    ])
    def test_these_keep_the_quick_models_change(self, message):
        assert _route(message, "change", NOTHING_OPEN).kind == "change"

    def test_the_open_case_is_f1628s_as_it_was(self):
        routed = _route(P6, "change", EXPENSE_OPEN)
        assert routed.kind == "build" and routed.overridden_by == "new_using_open", routed
