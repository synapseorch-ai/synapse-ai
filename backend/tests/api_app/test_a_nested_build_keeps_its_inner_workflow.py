"""The AI builder keeps the inner workflow of a nested build it made (F429, S1).

C2 (D200) asked the builder for a two-level vendor review. The saver built the
inner "Vendor check", deployed it, built the outer "Vendor review" calling its
workflow agent and validated only the outer one; `finish_build` then deleted the
inner one, which still carried the run's mark. The outer workflow failed its
first run with "Sub-orchestration … not found", and the answer said "ready to
use". This drives the whole builder — plan, approval, the saver's tool calls,
`step_finish_build`, the summary — the way the panel does, with a scripted model.
"""
from __future__ import annotations

import itertools

import pytest

from core.native_builder import seed_native_builder
from test_builder_routes import route, stream, tool_call


@pytest.fixture
async def builder():
    await seed_native_builder()


@pytest.fixture
def ids():
    """Ids the script can name before the saver makes them (orch_t1, agent_t2, orch_t3),
    through the store's minting, as an embedder installs it (F431)."""
    from core.store.ids import set_id_minter

    counter = itertools.count(1)
    set_id_minter(lambda prefix: f"{prefix}_t{next(counter)}")
    yield
    set_id_minter(None)


INNER, INNER_AGENT, OUTER = "orch_t1", "agent_t2", "orch_t3"

SAVER = [
    tool_call("create_orchestration_skeleton", name="Vendor check", entry_step_id="step_check"),
    tool_call("add_multiple_steps", orch_id=INNER, steps=[
        {"step_id": "step_check", "name": "Check", "type": "llm", "prompt_template": "Check {state.user_input}",
         "output_key": "check", "next_step_id": "step_end"},
        {"step_id": "step_end", "name": "End", "type": "end"},
    ]),
    tool_call("deploy_orchestration", orch_id=INNER),
    tool_call("create_orchestration_skeleton", name="Vendor review", entry_step_id="step_call"),
    tool_call("add_multiple_steps", orch_id=OUTER, steps=[
        {"step_id": "step_call", "name": "Check a vendor", "type": "agent", "agent_id": INNER_AGENT,
         "prompt_template": "Acme", "output_key": "report", "next_step_id": "step_end"},
        {"step_id": "step_end", "name": "End", "type": "end"},
    ]),
    tool_call("validate_orchestration", orch_id=OUTER),
    "DONE",
]


async def test_the_inner_workflow_survives_the_build_and_its_parent_finds_it(client, builder, ids, fake_llm):
    from core.store.resources import get_orchestration

    fake_llm.script([
        route("create"),                                         # step_intent
        "Requirements: review three vendors, each with an inner check.",  # step_understand
        route("draft"),                                          # step_clarify_route
        "## Plan\nVendor review calls Vendor check for each vendor.",     # step_draft_plan
        tool_call("check_plan"),                                 # step_check_plan's arguments (F492)
    ])
    events = await stream(client, "/api/builder/chat", {"message": "build a vendor review with an inner check"})
    run_id = next(e for e in events if e["type"] == "human_input_required")["run_id"]

    fake_llm.reset()
    fake_llm.script([
        route("no"),                      # step_agents_needed
        route("create"),                  # step_save_route
        *SAVER,                           # step_save_create
        tool_call("finish_build"),        # step_finish_build's arguments (the summary asks no model: F490)
    ])
    after = await stream(client, "/api/builder/resume", {"run_id": run_id, "response": {"feedback": "approve"}})

    steps = [e.get("step_name") for e in after if e["type"] == "step_start"]
    assert "Finish Build" in steps, steps
    assert not [e for e in after if e.get("type") == "orchestration_error"], after

    # Both workflows are there, finished, and the inner one's agent still runs it.
    inner, outer = await get_orchestration(INNER), await get_orchestration(OUTER)
    assert inner is not None and outer is not None
    assert "building_run" not in inner and "building_run" not in outer

    done = next(e for e in after if e["type"] == "orchestration_complete")
    cleanup = str(done["final_state"].get("build_cleanup"))
    assert "'removed': []" in cleanup or '"removed": []' in cleanup, cleanup

    # The summary is the run's own record (F490): both workflows built, nothing removed.
    summary = done["final_state"].get("summary") or ""
    assert summary.startswith("Built the workflows ") and "Removed" not in summary, summary

    # And the outer workflow's first run reaches the inner one — C2's failed with
    # "Sub-orchestration 'orch_209xwc1' … not found".
    fake_llm.reset()
    fake_llm.set_default("Acme checks out.")
    ran = await stream(client, f"/api/orchestrations/{OUTER}/run", {"message": "Acme"})
    assert not [e for e in ran if "not found" in str(e.get("error", ""))], ran
    finished = next(e for e in ran if e["type"] == "orchestration_complete")
    assert finished["status"] == "completed", finished
    # The inner workflow ran: its step's execution memory records the call. Read from the run's
    # own state — the stream no longer carries the engine's state keys (F854).
    from core.orchestration.state import SharedState

    kept = (await SharedState.restore(finished["run_id"])).run.shared_state
    assert "sub_orchestration" in str(kept)
