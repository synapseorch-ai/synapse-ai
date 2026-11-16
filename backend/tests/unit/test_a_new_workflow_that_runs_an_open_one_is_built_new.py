"""A new workflow that runs, calls or follows the open one is built new, never planned as a change to it (F1628).

Beside a workflow open on screen, "Make a workflow that runs my 'X' workflow for a sample expense, then shows me what it
decided" was planned as a change to X: the rule that a new request beside an open workflow is new (F1605) stood aside as
soon as the message named the open one, though it named it as what the new workflow runs. Now a message that starts as a
new job and names a workflow after a word that uses it (runs, calls, starts, triggers, uses, after) is a new build, in
English by its words; in another language the quick model's "build" stands. Each way has its near misses: an edit of the
open one that names it stays an edit.
"""
from __future__ import annotations

import pytest

from core.native_builder.classify import override
from core.native_builder.objects import Request

INVENTORY = {"schedule": ["Nightly invoices"], "agent": ["Front desk"], "orchestration": ["Customer Refund Approval"]}
OPEN = {"may_change": True, "may_act": True, "current_orchestration_source": "viewing",
        "current_orchestration_id": "orch_refund", "current_orchestration_name": "Customer Refund Approval"}


def _route(message: str, kind: str):
    return override(Request(kind=kind, needs="workspace"), message=message, state=dict(OPEN), inventory=INVENTORY,
                    connectable=set())


class TestANewWorkflowThatUsesTheOpenOneIsNew:
    @pytest.mark.parametrize("message", [
        "Make a workflow that runs my 'Customer Refund Approval' workflow for a sample refund of 120, then shows me "
        "what it decided.",
        "Build a workflow that calls Customer Refund Approval every Monday with last week's refunds.",
        "Create a workflow that starts after Customer Refund Approval finishes and emails the result to finance.",
    ])
    @pytest.mark.parametrize("kind", ["change", "build"])
    def test_it_is_planned_new(self, message, kind):
        routed = _route(message, kind)
        assert routed.kind == "build" and routed.route == "step_plan_room", (message, kind, routed.route)

    def test_in_another_language_the_quick_models_build_stands(self):
        routed = _route("Haz un flujo que ejecute 'Customer Refund Approval' con un reembolso de prueba de 120 y me "
                        "muestre lo que decidió.", "build")
        assert routed.kind == "build", routed.route


class TestAnEditOfTheOpenOneThatNamesItStaysOne:
    """Near misses: the open workflow named as what to change, never as what a new one runs."""

    @pytest.mark.parametrize("message", ["add an approval step to Customer Refund Approval",
                                         "make Customer Refund Approval run every Monday",
                                         "change Customer Refund Approval's threshold to 200",
                                         "Every Monday run Customer Refund Approval for last week's refunds",
                                         "make Customer Refund Approval start after Nightly invoices"])
    def test_it_stays_a_change(self, message):
        assert _route(message, "change").kind == "change", message
