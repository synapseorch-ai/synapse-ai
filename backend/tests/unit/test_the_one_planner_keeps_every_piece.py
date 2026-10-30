"""Merging the two planners kept every piece Synapse AI already had (D340 D0; PLANNER-DESIGN.md §5, "Kept exactly").

Naveen (10-30): "make sure the mcp connection card and tool look ups and everything is available.. i don't want to keep
re doing this again and again". Each row of the spec's kept list is held here by what carries it — a step that is
still in the workflow and still reached, or a tool still registered — so a later change that drops one fails by name.
"""
from __future__ import annotations

import pytest

from core.native_builder.seed import bundled_definitions


@pytest.fixture(scope="module")
def steps():
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


def _reached(steps: dict, target: str) -> bool:
    """Whether any step leads to `target`."""
    keys = ("next_step_id", "if_true_step_id", "if_false_step_id", "switch_default_step_id", "on_exhausted_step_id",
            "true_step_id", "false_step_id")
    for s in steps.values():
        if target in {s.get(k) for k in keys} or target in (s.get("switch_cases") or {}).values():
            return True
    return False


#: (what is kept, the step that carries it, the tool it runs or None).
KEPT_STEPS = [
    ("read_turn and the door", "step_turn", "read_turn"),
    ("a role that may not change is told so", "step_not_yours", None),
    ("the role check before a planner turn", "step_may_change", None),
    ("the effort check (a stronger model offered for a build)", "step_effort", "effort_check"),
    ("plan room before a build", "step_plan_room", None),
    ("rounds caps", "step_plan_rounds", None),
    ("the classify at a waiting plan (continues required)", "step_classify_at_plan", None),
    ("the plan check", "step_check_plan", "check_build"),
    ("the change check, with its guards", "step_check_change", "check_change"),
    ("the provider lookup", "step_plan_lookup", "look_up_providers"),
    ("the plan researcher net", "step_plan_research", None),
    ("the prompt writer", "step_agent_prompts", None),
    ("the plan's review", "step_human_review", None),
    ("the plan's approval, with its digest", "step_plan_approve", "approve_plan"),
    ("the change's approval", "step_manage_review", None),
    ("the change recorded", "step_change_approve", "approve_change"),
    ("the change applied", "step_apply", "apply_change"),
    ("the build's tools made", "step_make_tools", "save_build"),
    ("the Connect card's wait", "step_wait_connect", None),
    ("the Connect card's answer", "step_connect_answer", "read_connect_answer"),
    ("the build saved", "step_save", "save_build"),
    ("the maker", "step_make_new", None),
    ("what was made, checked", "step_check_made", "check_made"),
    ("a question asked", "step_human_clarify", None),
    ("a which-one about a change asked", "step_change_clarify", None),
    ("the answer route's check (F1243)", "step_answer_checked", "check_answer"),
    ("a provider question's own route", "step_research", "look_up_providers"),
    ("the provider researcher", "step_lookup_research", None),
]


@pytest.mark.parametrize("what, sid, tool", KEPT_STEPS, ids=[k[0] for k in KEPT_STEPS])
def test_each_piece_is_still_there_and_still_reached(steps, what, sid, tool):
    assert sid in steps, f"{what}: {sid} is gone"
    if sid != "step_turn":
        assert _reached(steps, sid), f"{what}: nothing leads to {sid}"
    if tool is not None:
        assert steps[sid].get("forced_tool") == tool, f"{what}: {sid} no longer runs {tool}"


KEPT_TOOLS = ["prepare_connection", "look_up_providers", "report_findings", "check_build", "check_change",
              "submit_plan", "submit_change", "ask_person", "cannot", "approve_plan", "approve_change",
              "apply_change", "save_build", "read_plan_context", "read_change_context", "check_answer"]


@pytest.mark.parametrize("name", KEPT_TOOLS)
def test_each_tool_is_still_registered(name):
    import core.builder_tools  # noqa: F401 — first, as the product loads it: the modules below register at import
    import core.builder_connect  # noqa: F401
    import core.builder_research  # noqa: F401
    import core.native_builder.answer_pack  # noqa: F401
    import core.native_builder.build_plan  # noqa: F401
    import core.native_builder.change_apply  # noqa: F401
    import core.native_builder.change_tools  # noqa: F401
    import core.native_builder.lookup  # noqa: F401
    from core.builder_tools import BUILDER_TOOL_NAMES

    assert name in BUILDER_TOOL_NAMES


def test_the_planner_still_sees_the_images_a_person_sends(steps):
    assert steps["step_plan"].get("pass_images") is True


def test_the_planner_still_stops_after_too_many_rounds(steps):
    assert steps["step_plan"].get("on_exhausted_step_id") == "step_plan_rounds"
