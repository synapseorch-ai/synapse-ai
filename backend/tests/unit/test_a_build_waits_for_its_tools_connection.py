"""
A build that made tools from a read API waits for the person to connect them before it saves the rest, and never
leaves a made tool behind when it does not save (F946, SAI2-P3, D300; P3B-HANDOFF.md, SAI2's addition 2).

After the yes, `step_make_tools` makes the tools and puts up one Connect card per provider (F945). `step_wait_connect`
waits for the person; `read_connect_answer` reads what they said, in code:
- the card's own words ("Saved the Authorization for Mailchimp.") with every tool connected → the save;
- "build without Mailchimp" → its tools removed, and back to the check with it left out (a new review, a new yes);
- "stop" → its tools removed, said, and the build ends;
- anything else → asked again, a few times, then as stop.
A run cancelled or expired while it waits (New conversation, the parked-run sweep) removes them too.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save, bundled_definitions
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role
from core.store.resources import load_tools, save_tool

from test_a_build_makes_the_tools_it_found import ASK, FINDINGS, SEND


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def owner():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def role(user_id):
        return Role.OWNER

    hooks.set_role_source(role)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _steps():
    return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}


def test_the_route_makes_the_tools_waits_for_them_and_then_saves():
    steps = _steps()
    assert steps["step_approve_route"]["switch_cases"]["ok"] == "step_make_tools"
    make = steps["step_make_tools"]
    assert (make["forced_tool"], make["tool_arguments"]) == ("save_build", {"only": "tools"})
    route = steps[make["next_step_id"]]
    assert route["type"] == "switch"
    assert set(route["switch_cases"].values()) >= {"step_wait_connect", "step_save"}
    wait = steps["step_wait_connect"]
    assert wait["type"] == "human" and wait["next_step_id"] == "step_connect_answer"
    # Asked again past its cap, it gives up as stop does: its tools removed (never left behind), then said.
    assert wait["on_exhausted_step_id"] == "step_connect_give_up" and wait["max_iterations"] == 5
    give_up = steps["step_connect_give_up"]
    assert (give_up["forced_tool"], give_up["tool_arguments"]) == ("read_connect_answer", {"give_up": True})
    assert give_up["next_step_id"] == "step_connect_stopped"
    answer = steps["step_connect_answer"]
    assert answer["forced_tool"] == "read_connect_answer" and answer["output_key"] == "plan_answer"
    cases = steps[answer["next_step_id"]]["switch_cases"]
    assert cases == {"connected": "step_save", "without": "step_check_plan", "stop": "step_connect_stopped",
                     "again": "step_wait_connect"}
    assert build_plan.MAKES_HTTP_TOOLS, "Build it is offered for a read API's tools now that the wait exists"


async def _made():
    """A run past the yes whose tools are made, as `step_make_tools` leaves it."""
    run = OrchestrationRun(run_id="builder_wait", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: FINDINGS})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Newsletter", "summary": "Sends it.",
             "steps": [ASK, SEND]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    made, events = await _call("save_build", {"only": "tools"}, run)
    assert made["outcome"] == "tools_made", made
    return run, events


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _answer(run, words):
    run.shared_state["connect_answer"] = {"feedback": words}
    return await _call("read_connect_answer", {}, run)


async def _made_tool():
    return next((t for t in await load_tools() if t["name"] == "mailchimp_send_campaign"), None)


async def test_the_card_goes_up_when_the_tools_are_made(client):
    _, events = await _made()
    [card] = [e for e in events if e.get("type") == "connection_needed"]
    assert card["tool"]["tools"] == ["mailchimp_send_campaign"] and card["tool"]["name"] == "Mailchimp"
    assert (card["tool"]["header"], card["tool"]["scheme"]) == ("Authorization", "Bearer")
    assert card["tool"]["host_params"] == [{"name": "dc", "says": "the part of your API key after the dash"}]


async def test_the_cards_words_with_every_tool_connected_go_on_to_the_save(client):
    from core.routes.tools import connect_made_tools

    run, _ = await _made()
    await connect_made_tools({"tools": ["mailchimp_send_campaign"], "header": "Authorization", "scheme": "Bearer",
                              "key": "k-us6", "host_params": {"dc": "us6"}, "provider": "Mailchimp"})
    out, _ = await _answer(run, "Saved the Authorization for Mailchimp.")
    assert out["outcome"] == "connected"


async def test_words_that_say_connected_while_a_tool_is_not_are_asked_again(client):
    run, _ = await _made()
    out, _ = await _answer(run, "Saved the Authorization for Mailchimp.")
    assert out["outcome"] == "again" and "not connected yet" in out["say"]


async def test_build_without_removes_its_tools_and_goes_back_to_the_check(client):
    run, _ = await _made()
    out, events = await _answer(run, "build without Mailchimp")
    assert out == {**out, "outcome": "without", "without": ["mailchimp_send_campaign"]}
    assert await _made_tool() is None and "I removed the Mailchimp tools I'd made" in out["say"]
    assert any(e.get("action") == "deleted" and e.get("name") == "mailchimp_send_campaign" for e in events)


async def test_stop_removes_its_tools_and_says_so(client):
    run, _ = await _made()
    out, _ = await _answer(run, "stop")
    assert out["outcome"] == "stop" and await _made_tool() is None
    assert "I removed the Mailchimp tools I'd made" in out["say"]


async def test_anything_else_is_asked_again(client):
    run, _ = await _made()
    out, _ = await _answer(run, "what's the weather in Rotterdam?")
    assert out["outcome"] == "again" and await _made_tool() is not None


@pytest.mark.parametrize("ends", ["cancelled", "expired"])
async def test_a_run_ended_while_it_waits_removes_its_tools(client, ends):
    """SAI2's addition 2: New conversation and the parked-run sweep both cancel the run."""
    from core.orchestration.cancel import cancel_run

    run, _ = await _made()
    run.status = "paused"
    await SharedState(run).checkpoint()
    await cancel_run(run.run_id, reason=ends)
    assert await _made_tool() is None


async def test_a_saved_builds_tools_are_never_removed_by_a_later_cancel(client):
    from core.orchestration.cancel import cancel_run
    from core.routes.tools import connect_made_tools

    run, _ = await _made()
    await connect_made_tools({"tools": ["mailchimp_send_campaign"], "header": "Authorization", "scheme": "Bearer",
                              "key": "k-us6", "host_params": {"dc": "us6"}, "provider": "Mailchimp"})
    saved, _ = await _call("save_build", {}, run)
    assert saved["outcome"] == "saved", saved
    await cancel_run(run.run_id)
    assert await _made_tool() is not None


async def test_giving_up_after_asking_removes_its_tools(client):
    run, _ = await _made()
    out, _ = await _call("read_connect_answer", {"give_up": True}, run)
    assert out["outcome"] == "stop" and await _made_tool() is None
