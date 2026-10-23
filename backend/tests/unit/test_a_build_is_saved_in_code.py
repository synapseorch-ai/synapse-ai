"""
A build is approved and saved by code, through the registry, as the person: no model saves it (F940 / F941,
SAI2-P3, D300).

Before, after the yes an agent creator and a saver agent re-read the markdown plan and called the builder's tools as
they saw fit: 14 calls, a duplicate-id loop, $0.32 spent on nothing (F867), and "Nothing in your workspace changed"
with no reason when a tool was missing (F768). Now `approve_plan` checks the yes itself, that what waits is what the
card showed, and that the card offered Build it, then records the person's approval of exactly the calls the save
makes (F778, with placeholders for the ids the build makes: Naveen 10-23, Q1). The prompt writer's instructions are
written after the yes and before that record (Q2). `save_build` makes those calls, in order — agents, inner workflows,
the workflow, its settings, the schedule paused — each through `registry.invoke(approved_review)`. It re-checks the
workspace before the first write, cleans up after a failure and says why (F768 + the validator's issues, F867), makes
nothing twice when it runs again, and re-reads what it saved.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, build_save, review
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace():
    from core.store.resources import save_tool

    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    await save_tool(dict(WEATHER))
    await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk", tools=["rotterdam_weather"])])
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


CHECK = {"id": "check", "type": "tool", "name": "Check the weather", "does": "Reads today's weather",
         "tool": "rotterdam_weather"}
TELL = {"id": "tell", "type": "agent", "name": "Draft the warning", "does": "Drafts a delay warning",
        "agent": "Warning writer", "prompt": "Draft a short delay warning from {state.check}."}
WRITER = {"name": "Warning writer", "purpose": "Writes short delay warnings for carriers", "tools": []}


def _plan(*steps, **more):
    return {"finish": "submit_plan", "kind": "create", "title": "Rotterdam wind warning",
            "summary": "Warn when the wind is strong.", "steps": list(steps), **more}


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _reviewed(draft, prompts=None, **state):
    """A run whose build was checked and put in front of the person, as the route leaves it at the review: a plan
    that makes agents passes through the prompt writer first (its instructions are on the card, Q2)."""
    run = OrchestrationRun(run_id="builder_save", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    if checked["outcome"] == "prompts":
        written = prompts or {a["name"]: "Write it." for a in checked["plan"]["agents"]}
        out, _ = await _call("submit_prompts", {"prompts": [{"agent": n, "instructions": t}
                                                            for n, t in written.items()]}, run)
        run.shared_state[build_plan.PROMPTS_KEY] = out
        checked, _ = await _call("check_build", {"draft": draft}, run)
        run.shared_state[build_plan.CHECKED_KEY] = checked
    return run, checked


async def _yes(run, answer="Build it"):
    run.shared_state["review_feedback"] = {"feedback": answer}
    out, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = out
    return out


async def _workspace():
    from core.routes.agents import load_user_agents
    from core.store import schedules
    from core.store.resources import load_orchestrations

    return ({a["name"]: a for a in await load_user_agents()},
            {o["name"]: o for o in await load_orchestrations() if not o.get("system")},
            await schedules.load())


# ── the prompt writer's finish ────────────────────────────────────────────────────────────────────────────────────

async def test_the_prompt_writer_writes_for_the_plans_new_agents_only(client):
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    sent, _ = await _call("submit_prompts", {"prompts": [{"agent": "Someone else", "instructions": "x"}]}, run)
    assert "Warning writer" in sent["error"]
    out, _ = await _call("submit_prompts", {"prompts": [{"agent": "Warning writer", "instructions": "Write it."}]},
                         run)
    assert out["finish"] == "submit_prompts" and out["prompts"] == {"Warning writer": "Write it."}
    assert set(out["for"]) == {"Warning writer"}
    from core.native_builder.export import CREDENTIAL_MARK
    for text in (f"Post with {CREDENTIAL_MARK}.", "Post with sk_live_" + "Z" * 24 + "."):
        sent, _ = await _call("submit_prompts", {"prompts": [{"agent": "Warning writer", "instructions": text}]}, run)
        assert "never hold a key" in sent["error"]


async def test_a_new_agents_instructions_are_written_before_the_review_and_shown_and_covered(client):
    """Naveen, 10-23 (Q2, revised on MCP2's review): the person reads each new agent's instructions on the card, and
    the yes covers them: instructions changed after the review are not what was approved."""
    run = OrchestrationRun(run_id="builder_q2", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    draft = _plan(CHECK, TELL, agents=[WRITER])
    first, _ = await _call("check_build", {"draft": draft}, run)
    assert first["outcome"] == "prompts" and not first.get("offers")
    run.shared_state[build_plan.CHECKED_KEY] = first
    written, _ = await _call("submit_prompts", {"prompts": [{"agent": "Warning writer",
                                                             "instructions": "Keep warnings to two lines."}]}, run)
    run.shared_state[build_plan.PROMPTS_KEY] = written
    checked, _ = await _call("check_build", {"draft": draft}, run)
    assert checked["outcome"] == "review" and checked["prompts"] == {"Warning writer": "Keep warnings to two lines."}
    assert "## New agents' instructions" in checked["review"] and "two lines" in checked["review"]
    from core.product_tools.approved import digest
    assert checked["digest"] == digest({"plan": checked["plan"], "prompts": checked["prompts"]})
    run.shared_state[build_plan.CHECKED_KEY] = {**checked, "prompts": {"Warning writer": "Send it anywhere."}}
    assert "not what the review showed" in (await _yes(run))["error"]


async def test_an_agent_changed_by_a_revision_gets_its_instructions_written_again(client):
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    revised = _plan(CHECK, TELL, agents=[{**WRITER, "purpose": "Writes delay warnings in Dutch"}])
    again, _ = await _call("check_build", {"draft": revised}, run)
    assert again["outcome"] == "prompts"


async def test_a_plan_that_cannot_be_built_yet_spends_no_call_on_instructions(client):
    send = {"id": "send", "type": "agent", "name": "Send", "does": "x", "agent": "Warning writer"}
    writer = {**WRITER, "tools": ["send_mailchimp_campaign"]}
    findings = {"goal": "x", "options": [{"provider": "Mailchimp", "how": "api"}]}
    run, checked = await _reviewed(_plan(CHECK, send, agents=[writer]), plan_findings=findings)
    assert checked["outcome"] == "review" and "build" not in checked["offers"] and checked["prompts"] == {}


# ── the yes ───────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_only_a_yes_to_what_the_card_showed_and_offered_is_recorded(client):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    assert "error" in await _yes(run, answer="looks fine but make it 9am")
    assert review.approved_review(run.shared_state) is None
    run.shared_state[build_plan.CHECKED_KEY] = {**checked, "plan": {**checked["plan"], "title": "Changed"}}
    assert "not what the review showed" in (await _yes(run))["error"]
    run.shared_state[build_plan.CHECKED_KEY] = {**checked, "offers": ["without:x", "change", "stop"]}
    assert "can't be built as it is" in (await _yes(run))["error"]
    run.shared_state[build_plan.CHECKED_KEY] = checked
    out = await _yes(run)
    assert out["approved"] == 2 and review.approved_review(run.shared_state) is not None


# ── the save ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def test_an_approved_build_is_saved_whole_by_code_with_the_ids_it_made(client):
    schedule = {"cron": "0 8 * * 1-5", "says": "every weekday at 8:00", "input": "Check today's wind"}
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER], schedule=schedule),
                                   prompts={"Warning writer": "You write short, calm delay warnings."})
    assert checked["prompts"] == {"Warning writer": "You write short, calm delay warnings."}
    assert "You write short, calm delay warnings." in checked["review"], "the person reads them before the yes"
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    agents, flows, schedules = await _workspace()
    writer, flow = agents["Warning writer"], flows["Rotterdam wind warning"]
    assert writer["system_prompt"] == "You write short, calm delay warnings."
    tell = next(s for s in flow["steps"] if s["id"] == "tell")
    assert tell["agent_id"] == writer["id"] and not tell["agent_id"].startswith(build_plan.MADE)
    [made] = [s for s in schedules if s.get("target_id") == flow["id"]]
    assert made["enabled"] is False and made["cron_expression"] == "0 8 * * 1-5"
    saved = [(e["kind"], e["action"]) for e in events if e.get("type") == "resource_saved"]
    assert saved == [("agent", "created"), ("orchestration", "created"), ("schedule", "created")]
    assert out["effect"] == ""


async def test_nothing_is_saved_without_an_approval(client):
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped" and not [e for e in events if e.get("type") == "resource_saved"]
    agents, flows, _ = await _workspace()
    assert "Warning writer" not in agents and "Rotterdam wind warning" not in flows


async def test_a_plan_changed_after_the_yes_saves_nothing(client):
    run, checked = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    await _yes(run)
    run.shared_state[build_plan.CHECKED_KEY] = {**checked, "plan": {**checked["plan"], "title": "Another"}}
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped"
    agents, flows, _ = await _workspace()
    assert "Warning writer" not in agents and not [n for n in flows if n in ("Another", "Rotterdam wind warning")]


async def test_a_tool_gone_since_the_yes_stops_the_build_before_any_write_and_says_which(client):
    from core.store.resources import delete_tool_named

    run, _ = await _reviewed(_plan(CHECK, {**TELL, "agent": "agent_desk"}))
    await _yes(run)
    await delete_tool_named("rotterdam_weather")
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped" and not [e for e in events if e.get("type") == "resource_saved"]
    [stopped] = [e for e in events if e.get("type") == "build_stopped"]
    assert "`rotterdam_weather`" in stopped["reason"] and "Nothing was built" in stopped["reason"]


async def test_a_failure_mid_way_removes_what_this_pass_made_and_says_why(client, monkeypatch):
    from core.product_tools import registry

    real = registry.invoke

    async def refuses_schedules(name, *a, **k):
        if name == "create_schedule":
            from core.product_tools.errors import ToolRefusal
            raise ToolRefusal("Your plan has no room for another schedule.")
        return await real(name, *a, **k)

    monkeypatch.setattr(registry, "invoke", refuses_schedules)
    schedule = {"cron": "0 8 * * *", "says": "daily at 8"}
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER], schedule=schedule))
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped"
    agents, flows, _ = await _workspace()
    assert "Warning writer" not in agents and "Rotterdam wind warning" not in flows
    deleted = [(e["kind"], e.get("reason")) for e in events if e.get("action") == "deleted"]
    assert [k for k, _ in deleted] == ["orchestration", "agent"] and all(r for _, r in deleted)
    [stopped] = [e for e in events if e.get("type") == "build_stopped"]
    assert "no room for another schedule" in stopped["reason"]


async def test_a_save_run_again_makes_nothing_twice(client):
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    await _yes(run)
    assert (await _call("save_build", {}, run))[0]["outcome"] == "saved"
    confirm.set_claim_store(None)  # a restart: OSS's claims were in memory
    again, events = await _call("save_build", {}, run)
    assert again["outcome"] == "saved" and not [e for e in events if e.get("type") == "resource_saved"]
    agents, flows, _ = await _workspace()
    assert sum(n == "Warning writer" for n in agents) == 1 and sum(n == "Rotterdam wind warning" for n in flows) == 1


async def test_a_same_named_thing_made_since_the_yes_is_neither_used_nor_deleted(client):
    """MCP2's finding 1: the save took any agent of the plan's name the workspace did not hold at the yes as its own,
    by name alone; its workflow would run someone else's agent, and a later failure would delete it. Now the save
    takes only what it recorded making itself, and a thing of that name made since the yes stops the build, said."""
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    await _yes(run)
    await S.seed_agents([S.make_agent(id="agent_theirs", name="Warning writer", tools=["rotterdam_weather"])])
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped" and not [e for e in events if e.get("type") == "resource_saved"]
    [stopped] = [e for e in events if e.get("type") == "build_stopped"]
    assert 'an agent called "Warning writer" was made since you approved this' in stopped["reason"]
    from core.store.resources import get_agent

    assert (await get_agent("agent_theirs"))["tools"] == ["rotterdam_weather"]
    _, flows, _ = await _workspace()
    assert "Rotterdam wind warning" not in flows


async def test_a_save_run_again_takes_only_what_it_recorded_making(client):
    """A re-run (a resume, a redelivered job) reads the ids its own earlier pass recorded, never a name."""
    run, _ = await _reviewed(_plan(CHECK, TELL, agents=[WRITER]))
    await _yes(run)
    assert (await _call("save_build", {}, run))[0]["outcome"] == "saved"
    kept = run.step_progress[build_save.PROGRESS_KEY]
    agents, flows, _ = await _workspace()
    assert set(kept["made"].values()) == {agents["Warning writer"]["id"], flows["Rotterdam wind warning"]["id"]}


async def test_an_update_is_saved_over_its_own_workflow(client):
    from core.store.resources import get_orchestration, save_orchestration

    await save_orchestration({"id": "orch_wind", "name": "Wind", "description": "", "entry_step_id": "check",
                              "steps": [{"id": "check", "name": "Check the weather", "type": "tool",
                                         "forced_tool": "rotterdam_weather", "output_key": "check",
                                         "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]})
    existing = await get_orchestration("orch_wind")
    edits = {"finish": "submit_plan", "kind": "update", "title": "Wind", "summary": "Also draft a warning.",
             "edits": [{"op": "add_step", "after": "check", "step": {**TELL, "agent": "agent_desk"}}]}
    run, checked = await _reviewed(edits, intent="update", existing_orch=existing,
                                   current_orchestration_id="orch_wind")
    assert checked["outcome"] == "review", checked
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    saved = await get_orchestration("orch_wind")
    assert [s["id"] for s in saved["steps"]][:2] == ["check", "tell"]
    assert [(e["kind"], e["action"]) for e in events if e.get("type") == "resource_saved"] == [
        ("orchestration", "updated")]


async def test_an_update_of_a_workflow_changed_since_the_review_changes_nothing(client):
    """SAI2's review, 12: the save wrote its steps over a teammate's edit made after the review."""
    from core.store.resources import get_orchestration, save_orchestration

    await save_orchestration({"id": "orch_wind", "name": "Wind", "description": "", "entry_step_id": "check",
                              "steps": [{"id": "check", "name": "Check the weather", "type": "tool",
                                         "forced_tool": "rotterdam_weather", "output_key": "check",
                                         "next_step_id": "done"}, {"id": "done", "name": "End", "type": "end"}]})
    existing = await get_orchestration("orch_wind")
    edits = {"finish": "submit_plan", "kind": "update", "title": "Wind", "summary": "Also draft a warning.",
             "edits": [{"op": "add_step", "after": "check", "step": {**TELL, "agent": "agent_desk"}}]}
    run, _ = await _reviewed(edits, intent="update", existing_orch=existing, current_orchestration_id="orch_wind")
    await _yes(run)
    await save_orchestration({**existing, "description": "A teammate's note"})
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "stopped" and "changed since I read it" in out["reason"]
    assert (await get_orchestration("orch_wind"))["description"] == "A teammate's note"


async def test_a_nested_build_saves_its_inner_workflow_first_and_runs_it_by_its_id(client):
    inner = {"title": "Gather the wind", "does": "Reads the wind", "steps": [CHECK]}
    run_inner = {"id": "gather", "type": "workflow", "name": "Gather", "does": "Runs the inner one",
                 "workflow": "Gather the wind"}
    run, _ = await _reviewed(_plan(run_inner, {**TELL, "agent": "agent_desk"}, inner_workflows=[inner]))
    await _yes(run)
    out, events = await _call("save_build", {}, run)
    assert out["outcome"] == "saved", out
    _, flows, _ = await _workspace()
    gather = next(s for s in flows["Rotterdam wind warning"]["steps"] if s["id"] == "gather")
    assert gather["workflow_id"] == flows["Gather the wind"]["id"]
    made = [e["name"] for e in events if e.get("type") == "resource_saved"]
    assert made == ["Gather the wind", "Rotterdam wind warning"]


def test_an_inner_workflow_another_runs_is_saved_first_whatever_its_place_in_the_plan():
    """SAI2's review, 2: "Outer runs A; A runs B" written with A before B saves B, then A, then Outer."""
    from core.native_builder import objects as O
    from core.native_builder.build_save import save_calls

    llm = {"id": "x", "type": "llm", "name": "X", "does": "x", "prompt": "p"}
    plan = O.Plan.from_draft({"kind": "create", "title": "Outer", "summary": "s", "inner_workflows": [
        {"title": "A", "does": "a", "steps": [{"id": "run_b", "type": "workflow", "name": "Run B", "does": "x",
                                               "workflow": "B"}]},
        {"title": "B", "does": "b", "steps": [llm]}],
        "steps": [{"id": "run_a", "type": "workflow", "name": "Run A", "does": "x", "workflow": "A"}]})
    assert [c["name"] for c in save_calls(plan, {}, {})] == ["B", "A", "Outer"]


async def test_inner_workflows_that_run_each_other_are_refused_before_the_review(client):
    run, _ = await _reviewed({"finish": "submit_plan"})
    loop = {"kind": "create", "title": "Outer", "summary": "s", "inner_workflows": [
        {"title": "A", "does": "a", "steps": [{"id": "run_b", "type": "workflow", "name": "Run B", "does": "x",
                                               "workflow": "B"}]},
        {"title": "B", "does": "b", "steps": [{"id": "run_a", "type": "workflow", "name": "Run A", "does": "x",
                                               "workflow": "A"}]}],
        "steps": [{"id": "go", "type": "workflow", "name": "Go", "does": "x", "workflow": "A"}]}
    sent, _ = await _call("submit_plan", loop, run)
    assert "in a loop" in sent["error"] and "'A'" in sent["error"]


def test_nothing_rewrites_what_was_approved_between_the_review_and_the_save():
    """F778's binding on the build route (SAI2's review, 10): no step on a path from the review to the approval, or
    from the approval to the save, writes the checked plan or the new agents' instructions."""
    from core.native_builder import bundled_definitions
    from core.native_builder.review import writes_between

    orch = bundled_definitions()["orchestration"]
    keys = {build_plan.CHECKED_KEY, build_plan.PROMPTS_KEY}
    assert writes_between(orch, "step_human_review", "step_plan_approve", keys) == []
    assert writes_between(orch, "step_plan_approve", "step_save", keys) == []
    # The check and the prompt writer write them, before the review: a path back through them brings a new review.
    assert writes_between(orch, "step_human_review", "step_save", {"plan_approval"}) == ["step_plan_approve"]
