"""
"Build it" right after findings builds what the findings were for (F1064, SAI2-P3, D300; P5's res-02 measure, 10-27).

The research turn ended with findings ("Let an agent book hotel rooms for staff conferences", 3 options); the person
said "build it"; the front door rightly sent it to the build, with the findings in state. But the planner's "The
person's message" was only "build it", so it asked "What should I build?", went 8 rounds on whatever the person then
invented, and ended "Nothing was built" (479k tokens). A bare go-ahead has no request of its own: its request is the
findings' goal. Code writes it (never a planner rule):
- a go-ahead (the review's yes words, or the turn reader's "go on" words) after findings with a goal puts "Build this:
  <goal>…" in the planner's pack, shown under the person's message;
- the person's own words stay where they are (`user_message`, and the conversation);
- a go-ahead that says more ("build it using Outlook") is a request of its own, and stays as written; with no findings
  nothing changes.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401  (the builder's tools load before build_plan, as at boot)
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, seed
from core.orchestration.context import _fill_task_template
from core.orchestration.engine import live
from core.orchestration.state import SharedState

GOAL = "Let an agent book hotel rooms for staff conferences"
FINDINGS = {"status": "reported", "goal": GOAL, "options": [
    {"provider": "Booking.com", "how": "api", "detail": "A partner API"}]}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _pack(message: str, *, findings: dict | None = FINDINGS) -> tuple[dict, dict]:
    state = {builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", "user_message": message,
             "chat_history": "user: find who can book hotel rooms\nassistant: Booking.com has a partner API."}
    if findings is not None:
        state.update(plan_findings=findings, prior_findings=findings)
    run = OrchestrationRun(run_id="builder_go_ahead", orchestration_id="orch_native_builder", shared_state=state)
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("read_plan_context", {}, _Server(), run_id=run.run_id)
    pack = json.loads(raw) if isinstance(raw, str) else raw
    return pack, {**state, "plan_context": pack}


def _planner_input(state: dict) -> str:
    step = next(s for s in seed.bundled_definitions()["orchestration"]["steps"] if s["id"] == "step_plan")
    return _fill_task_template(step["prompt_template"], state, {})


@pytest.mark.parametrize("message", ["build it", "yes", "do it", "go ahead", "Build it!", "go on"])
async def test_a_bare_go_ahead_after_findings_is_asked_to_build_their_goal(message):
    pack, state = await _pack(message)
    assert pack["request"].startswith(f"Build this: {GOAL}.")
    shown = _planner_input(state)
    assert f"Build this: {GOAL}." in shown
    # The person's own words stay theirs.
    assert state["user_message"] == message and f"The person's message:\n{message}" in shown


@pytest.mark.parametrize("message", ["build it using Outlook", "yes, but only for the Berlin office"])
async def test_a_go_ahead_that_says_more_stays_as_written(message):
    pack, state = await _pack(message)
    assert pack["request"] == ""
    assert "Build this:" not in _planner_input(state)


async def test_with_no_findings_a_go_ahead_is_left_alone():
    pack, state = await _pack("build it", findings=None)
    assert pack["request"] == ""
    assert "Build this:" not in _planner_input(state)


async def test_findings_with_no_goal_give_no_request():
    pack, _ = await _pack("build it", findings={"status": "reported", "goal": "", "options": []})
    assert pack["request"] == ""


@pytest.mark.parametrize("message, findings", [("build it", FINDINGS), ("build it using Outlook", FINDINGS),
                                               ("build it", None)])
async def test_the_planner_never_sees_an_unfilled_reference(message, findings):
    _, state = await _pack(message, findings=findings)
    shown = _planner_input(state)
    head = shown[:shown.index("The conversation so far:")]
    assert "{{" not in head and "{state." not in head, head
