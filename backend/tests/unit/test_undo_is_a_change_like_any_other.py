"""
"Undo that" is a change like any other: code builds its inverse from the card the change left, and it goes through
the same check, review, yes and apply (F797, SAI2-P2, D308; BWA1's review point 5).

The model says only which change (`submit_change(undo_of=<its card>)`); it never writes the inverse ops. A value the
card never kept (it held a credential) is not restored and the review says so; a deleted thing comes back only as a
new copy, with what does not come back said on the review (Naveen, 10-22); a name something took since stops the
re-create; and the person can undo only their own change.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def fresh():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


_N = {"n": 0}


async def _run(user_id="u1"):
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_undo_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": user_id}})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft, user_id="u1", answer="approve"):
    """One whole change: check, review answered, approve, apply. Returns (run, check, apply)."""
    run = await _run(user_id)
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": answer}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


async def _agent(**kw):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(**kw)
    await save_agent(agent)
    return agent


async def _tools(*names):
    """Tools that exist here, so F489 lets an agent hold them."""
    from core.store.resources import save_tool

    for name in names:
        await save_tool({"name": name, "description": name, "tool_type": "http", "method": "GET",
                         "url": f"https://api.example.com/{name}", "headers": {}, "inputSchema": {"type": "object"}})


async def _get(agent_id):
    from core.store.resources import get_agent

    return await get_agent(agent_id)


async def test_a_rename_is_undone_through_its_own_review(client):
    agent = await _agent(name="Front desk")
    first, _, done = await _change({"ops": [{"op": "rename_agent", "target": {"kind": "agent", "id": agent["id"]},
                                             "fields": {"name": "Harbor desk"}}]})
    assert done["applied"] == ["rename_agent"]
    _, checked, undone = await _change({"undo_of": first.run_id})
    assert checked["review"] == '- Rename the agent "Harbor desk" — to "Front desk"; undo: its old name'
    assert undone["applied"] == ["rename_agent"] and (await _get(agent["id"]))["name"] == "Front desk"


async def test_the_person_is_told_which_change_the_context_holds(client):
    agent = await _agent(name="Front desk")
    first, _, _ = await _change({"ops": [{"op": "set_agent_model", "target": {"kind": "agent", "id": agent["id"]},
                                          "fields": {"model": "claude-haiku-4-5"}, "says": "use haiku"}]})
    context = await _call("read_change_context", {}, await _run())
    assert context["last_change"]["key"] == first.run_id
    assert context["last_change"]["ops"] == [{"op": "set_agent_model", "target": "Front desk", "says": "use haiku"}]
    assert await _call("read_change_context", {}, await _run(user_id="u2")) == {"last_change": None,
                                                                                "waiting_pick": None}


async def test_a_schedule_s_time_goes_back(client):
    from core.store import schedules

    await schedules.save({"id": "sched_n", "name": "Every night at 2", "enabled": True, "target_type":
                          "orchestration", "target_id": "orch_x", "prompt": "Go", "schedule_type": "cron",
                          "cron_expression": "0 2 * * *"})
    first, _, _ = await _change({"ops": [{"op": "set_schedule", "target": {"kind": "schedule", "id": "sched_n"},
                                          "fields": {"cron_expression": "0 3 * * *"}}]})
    assert (await schedules.get("sched_n"))["cron_expression"] == "0 3 * * *"
    await _change({"undo_of": first.run_id})
    assert (await schedules.get("sched_n"))["cron_expression"] == "0 2 * * *"


async def test_tools_and_a_tool_policy_go_back_exactly(client):
    await _tools("read_file", "web_search", "search_help")
    agent = await _agent(name="Front desk", tools=["read_file", "web_search"], tool_policy={"destructive": "confirm"})
    first, _, done = await _change({"ops": [
        {"op": "set_agent_tools", "target": {"kind": "agent", "id": agent["id"]},
         "fields": {"add": ["search_help"], "remove": ["read_file"]}},
        {"op": "set_tool_policy", "target": {"kind": "agent", "id": agent["id"]},
         "fields": {"open_world": "confirm", "destructive": "inherit"}}]})
    assert done["applied"] == ["set_agent_tools", "set_tool_policy"], done
    changed = await _get(agent["id"])
    assert changed["tools"] == ["web_search", "search_help"] and changed["tool_policy"] == {"open_world": "confirm"}
    await _change({"undo_of": first.run_id})
    back = await _get(agent["id"])
    assert sorted(back["tools"]) == ["read_file", "web_search"]
    assert back["tool_policy"] == {"destructive": "confirm"}


async def test_a_deleted_agent_comes_back_as_a_new_copy_and_the_review_says_so(client):
    from core.store.resources import load_agents

    await _tools("web_search")
    agent = await _agent(name="Support bot", tools=["web_search"])
    first, _, done = await _change({"ops": [{"op": "delete_agent", "target": {"kind": "agent", "id": agent["id"]}}]})
    assert done["applied"] == ["delete_agent"] and await _get(agent["id"]) is None
    _, checked, undone = await _change({"undo_of": first.run_id})
    assert 'Bring back the agent "Support bot"' in checked["review"] and "without its memory" in checked["review"]
    assert undone["applied"] == ["restore_agent"], undone
    [back] = [a for a in await load_agents() if a.get("name") == "Support bot"]
    assert back["id"] != agent["id"] and back["tools"] == ["web_search"]


async def test_a_deleted_schedule_comes_back_paused(client):
    from core.store import schedules
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name="Nightly digest")
    await save_orchestration(flow)
    await schedules.save({"id": "sched_n", "name": "Every night at 2", "enabled": True, "target_type":
                          "orchestration", "target_id": flow["id"], "prompt": "Go", "schedule_type": "cron",
                          "cron_expression": "0 2 * * *"})
    first, _, _ = await _change({"ops": [{"op": "delete_schedule", "target": {"kind": "schedule", "id": "sched_n"}}]})
    _, checked, undone = await _change({"undo_of": first.run_id})
    assert "paused" in checked["review"] and undone["applied"] == ["create_schedule"]
    [back] = [s for s in await schedules.load() if s["name"] == "Every night at 2"]
    assert back["enabled"] is False and back["cron_expression"] == "0 2 * * *" and back["id"] != "sched_n"


async def test_a_name_taken_since_stops_the_re_create(client):
    agent = await _agent(name="Support bot")
    first, _, _ = await _change({"ops": [{"op": "delete_agent", "target": {"kind": "agent", "id": agent["id"]}}]})
    await _agent(name="Support bot")
    _, checked, _ = await _change({"undo_of": first.run_id})
    assert checked["outcome"] == "said" and "can't bring the old one back under its name" in checked["say"]


async def test_instructions_that_held_a_key_are_not_put_back_and_that_is_said(client):
    agent = await _agent(name="Front desk", system_prompt="Use key sk-live-abcdefghijklmnopqrstuvwx1234 for calls.")
    first, _, _ = await _change({"ops": [{"op": "set_agent_instructions", "target": {"kind": "agent",
                                                                                     "id": agent["id"]},
                                          "fields": {"system_prompt": "Be brief."}}]})
    _, checked, _ = await _change({"undo_of": first.run_id})
    assert checked["outcome"] == "said" and "held a key" in checked["say"]
    assert (await _get(agent["id"]))["system_prompt"] == "Be brief."


async def test_only_the_person_s_own_change_can_be_undone(client):
    agent = await _agent(name="Front desk")
    first, _, _ = await _change({"ops": [{"op": "rename_agent", "target": {"kind": "agent", "id": agent["id"]},
                                          "fields": {"name": "Desk"}}]})
    _, checked, _ = await _change({"undo_of": first.run_id}, user_id="u2")
    assert checked["outcome"] == "said" and "can't find a change of yours" in checked["say"]
    assert (await _get(agent["id"]))["name"] == "Desk"


async def test_an_undo_waits_for_its_own_yes(client):
    agent = await _agent(name="Front desk")
    first, _, _ = await _change({"ops": [{"op": "rename_agent", "target": {"kind": "agent", "id": agent["id"]},
                                          "fields": {"name": "Desk"}}]})
    _, checked, undone = await _change({"undo_of": first.run_id}, answer="hmm, not yet")
    assert checked["outcome"] == "review" and undone["applied"] == []
    assert (await _get(agent["id"]))["name"] == "Desk"


async def test_a_deleted_workflow_comes_back_as_a_new_copy(client):
    from core.store.resources import load_orchestrations, save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name="Nightly digest", max_run_credits=3)
    await save_orchestration(flow)
    first, _, done = await _change({"ops": [{"op": "delete_workflow", "target": {"kind": "workflow",
                                                                                 "id": flow["id"]}}]})
    assert done["applied"] == ["delete_workflow"]
    _, checked, undone = await _change({"undo_of": first.run_id})
    assert "without its run history" in checked["review"] and undone["applied"] == ["restore_workflow"], undone
    [back] = [o for o in await load_orchestrations() if o.get("name") == "Nightly digest"]
    assert back["id"] != flow["id"] and [s["id"] for s in back["steps"]] == ["step_1"]
    assert back["max_run_credits"] == 3


async def test_a_deleted_http_tool_comes_back_without_its_headers(client):
    from core.store.resources import load_tools, save_tool

    await save_tool({"name": "mailchimp_lists", "description": "Lists", "tool_type": "http", "method": "GET",
                     "url": "https://us1.api.mailchimp.com/3.0/lists",
                     "headers": {"Authorization": "Bearer sk-live-abcdefghijklmnopqrstuvwx1234"},
                     "inputSchema": {"type": "object", "properties": {}}})
    first, _, done = await _change({"ops": [{"op": "delete_tool", "target": {"kind": "tool",
                                                                             "name": "mailchimp_lists"}}]})
    assert done["applied"] == ["delete_tool"]
    _, checked, undone = await _change({"undo_of": first.run_id})
    assert "enter its key again" in checked["review"] and undone["applied"] == ["restore_tool"], undone
    [back] = [t for t in await load_tools() if t["name"] == "mailchimp_lists"]
    assert back["url"] == "https://us1.api.mailchimp.com/3.0/lists" and not back.get("headers")


async def test_a_deleted_tool_comes_back_in_its_own_group(client):
    """A tool's `group` is its group's id (`grp_support`); `create_http_tool` takes a group's name. The undo brings it
    back into the group it was in, not a new one named after the old id (the branch review's finding 8)."""
    from core.routes.tools import _load_groups, _upsert_group
    from core.store.resources import load_tools, save_tool

    await _upsert_group({"id": "grp_support", "name": "Support", "description": ""})
    await save_tool({"name": "ticket_lookup", "description": "Tickets", "tool_type": "http", "method": "GET",
                     "url": "https://api.example.com/tickets", "headers": {}, "group": "grp_support",
                     "inputSchema": {"type": "object", "properties": {}}})
    first, _, _ = await _change({"ops": [{"op": "delete_tool", "target": {"kind": "tool", "name": "ticket_lookup"}}]})
    _, _, undone = await _change({"undo_of": first.run_id})
    assert undone["applied"] == ["restore_tool"], undone
    [back] = [t for t in await load_tools() if t["name"] == "ticket_lookup"]
    assert back["group"] == "grp_support"
    assert [g["id"] for g in await _load_groups()] == ["grp_support"]


async def test_a_tool_whose_address_held_a_key_is_said_to_be_so(client):
    from core.store.resources import save_tool

    await save_tool({"name": "leaky", "description": "Leaky", "tool_type": "http", "method": "GET",
                     "url": "https://api.example.com/x?api_key=sk-live-abcdefghijklmnopqrstuvwx1234", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}})
    first, _, _ = await _change({"ops": [{"op": "delete_tool", "target": {"kind": "tool", "name": "leaky"}}]})
    _, checked, _ = await _change({"undo_of": first.run_id})
    assert checked["outcome"] == "said" and "address held a key" in checked["say"]
