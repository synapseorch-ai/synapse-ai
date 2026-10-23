"""The AI builder keeps the inner workflow of a nested build it made (F429, S1).

C2 (D200) asked the builder for a two-level vendor review. The saver built the
inner "Vendor check", deployed it, built the outer "Vendor review" calling its
workflow agent and validated only the outer one; `finish_build` then deleted the
inner one, which still carried the run's mark. The outer workflow failed its
first run with "Sub-orchestration … not found", and the answer said "ready to
use". This drives the whole builder — plan, approval, the save, the summary — the
way the panel does, with a scripted model (since SAI2-P3: the planner's Plan, and
a save in code that makes the inner workflow first).
"""
from __future__ import annotations

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.fixture
async def builder():
    await seed_native_builder()


#: Since SAI2-P3 the planner writes the nested build as one Plan: the inner workflow, saved first, run by a
#: Workflow step (E3) in the outer one; the save is code.
NESTED = {"kind": "create", "title": "Vendor review", "summary": "Review a vendor with an inner check.",
          "inner_workflows": [{"title": "Vendor check", "does": "Checks one vendor", "steps": [
              {"id": "step_check", "type": "llm", "name": "Check", "does": "Checks the vendor",
               "prompt": "Check {state.user_input}"}]}],
          "steps": [{"id": "step_call", "type": "workflow", "name": "Check a vendor", "does": "Runs the check",
                     "workflow": "Vendor check", "prompt": "Acme"}]}


async def test_the_inner_workflow_survives_the_build_and_its_parent_finds_it(client, builder, fake_llm):
    from core.store.resources import load_orchestrations

    fake_llm.script([
        route("create"),                                         # step_intent
        tool_call("submit_plan", **NESTED),                      # step_plan (the planner's finish)
    ])
    events = await stream(client, "/api/builder/chat", {"message": "build a vendor review with an inner check"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    after = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "approve"}})
    assert not fake_llm.calls, "the save asks no model"

    steps = [e.get("orch_step_id") for e in after if e["type"] == "step_start"]
    assert "step_save" in steps, steps
    assert not [e for e in after if e.get("type") == "orchestration_error"], after

    # Both workflows are there, the inner one made first, and the outer one runs it by its id.
    made = {o["name"]: o for o in await load_orchestrations() if not o.get("system")}
    inner, outer = made["Vendor check"], made["Vendor review"]
    call = next(s for s in outer["steps"] if s["id"] == "step_call")
    assert call["type"] == "workflow" and call["workflow_id"] == inner["id"]
    saved = [e["name"] for e in after if e.get("type") == "resource_saved"]
    assert saved == ["Vendor check", "Vendor review"], "the inner workflow is made first"

    done = next(e for e in after if e["type"] == "orchestration_complete")
    # The summary is the run's own record (F490): both workflows built, nothing removed.
    summary = done["final_state"].get("summary") or ""
    assert summary.startswith("Built the workflows ") and "Removed" not in summary, summary

    # And the outer workflow's first run reaches the inner one — C2's failed with
    # "Sub-orchestration 'orch_209xwc1' … not found".
    fake_llm.reset()
    fake_llm.set_default("Acme checks out.")
    ran = await stream(client, f"/api/orchestrations/{outer['id']}/run", {"message": "Acme"})
    assert not [e for e in ran if "not found" in str(e.get("error", ""))], ran
    finished = next(e for e in ran if e["type"] == "orchestration_complete")
    assert finished["status"] == "completed", finished
    assert fake_llm.calls, "the inner workflow's step ran"
