"""
An approved change is made through the registry as its person, and each target is read again: a change without
effect is said (F796, SAI2-P2, D308; F778's first caller; F695's host half).

The operator agent used to carry out an approved markdown plan with the builder's tools directly: no registry, so the
audit named things by name (F697), and nothing checked that a write had taken effect. Now `approve_change` checks the
yes itself and records the person's approval of exactly the calls the review showed; `apply_change` makes each
through `registry.invoke(confirm_policy="approved_review")`, so the approval stands in for each call's token and for
nothing else; it reads the target before (one changed since the review is left alone) and after (what did not take
effect is said and not counted done); and the builder's `resource_saved` events reach the run, so `run_writes` says
what changed. Each applied op's before-image goes on the person's card, never a secret's value.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder.review import REVIEW_KEY
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
    held = {"role": Role.OWNER}

    async def source(user_id):
        return held["role"]

    hooks.set_role_source(source)
    audits: list = []

    async def audit(event):
        audits.append(event)

    hooks.set_audit(audit)
    yield {"role": held, "audits": audits}
    hooks.set_role_source(None)
    hooks.set_audit(None)
    builder_access.forget_runs()


async def _run():
    run = OrchestrationRun(run_id="builder_apply", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


async def _checked(run, ops):
    out, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": ops}}, run)
    assert out["outcome"] == "review", out
    run.shared_state["change_checked"] = json.dumps(out)
    return out


async def _agent(**kw):
    from core.store.resources import save_agent
    from tests._fakes.seed import make_agent

    agent = make_agent(**kw)
    await save_agent(agent)
    return agent


async def _get(agent_id):
    from core.store.resources import get_agent

    return await get_agent(agent_id)


def _rename(agent_id, name="Harbor desk"):
    return [{"op": "rename_agent", "target": {"kind": "agent", "id": agent_id}, "fields": {"name": name}}]


async def _approve_and_apply(run, answer="approve"):
    run.shared_state["manage_review"] = {"feedback": answer}
    approved, _ = await _call("approve_change", {}, run)
    applied, events = await _call("apply_change", {}, run)
    return approved, applied, events


async def test_an_approved_rename_is_made_audited_by_id_and_checked(client, fresh):
    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, _rename(agent["id"]))
    approved, applied, events = await _approve_and_apply(run)
    assert approved == {"approved": 1}
    assert applied == {"object": "change_applied", "applied": ["rename_agent"], "failed": [], "effect": ""}
    assert (await _get(agent["id"]))["name"] == "Harbor desk"
    saved = [e for e in events if e.get("type") == "resource_saved"]
    assert [(e["kind"], e["action"], e["id"]) for e in saved] == [("agent", "updated", agent["id"])]
    [audit] = fresh["audits"]
    assert (audit.tool, audit.target_type, audit.target_id, audit.user_id) == (
        "update_agent", "agent", agent["id"], "u1")
    # Confirmed by the person's review, and which one: never said to be a token (MCP2's point 4).
    assert audit.confirmed_by == f"review:{run.shared_state['_approved_review']['digest'][:16]}"


async def test_without_a_yes_nothing_is_approved_and_nothing_is_done(client):
    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, _rename(agent["id"]))
    approved, applied, _ = await _approve_and_apply(run, answer="approve, but call it Desk")
    assert "not approved" in approved["error"] and REVIEW_KEY not in run.shared_state
    assert applied["applied"] == [] and "does not cover" in applied["failed"][0]
    assert (await _get(agent["id"]))["name"] == "Front desk"


async def test_a_change_rewritten_after_the_review_is_not_approved(client):
    agent = await _agent(name="Front desk")
    run = await _run()
    out = await _checked(run, _rename(agent["id"]))
    out["calls"][0][1]["fields"]["name"] = "Something else"
    run.shared_state["change_checked"] = json.dumps(out)
    approved, applied, _ = await _approve_and_apply(run)
    assert "not what the review showed" in approved["error"]
    assert (await _get(agent["id"]))["name"] == "Front desk" and applied["applied"] == []


async def test_a_target_changed_since_the_review_is_left_as_it_is(client):
    from core.store.resources import save_agent

    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, _rename(agent["id"]))
    await save_agent({**await _get(agent["id"]), "name": "Renamed by someone else"})
    _, applied, _ = await _approve_and_apply(run)
    assert applied["applied"] == [] and "changed since you approved this (name)" in applied["failed"][0]
    assert (await _get(agent["id"]))["name"] == "Renamed by someone else"


async def test_a_person_demoted_since_the_yes_is_refused(client, fresh):
    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, _rename(agent["id"]))
    run.shared_state["manage_review"] = {"feedback": "yes"}
    await _call("approve_change", {}, run)
    fresh["role"]["role"] = Role.APPROVER
    builder_access.forget_runs()
    applied, _ = await _call("apply_change", {}, run)
    assert "building and changing things needs the builder role" in applied["error"]
    assert (await _get(agent["id"]))["name"] == "Front desk"


async def test_a_change_without_effect_is_said_and_not_counted_done(client, monkeypatch):
    from core.product_tools import registry

    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, _rename(agent["id"]))
    real = registry.get("update_agent")

    async def does_nothing(caller, args):
        return json.dumps({"status": "updated"})

    monkeypatch.setitem(registry._TOOLS, "update_agent", type(real)(**{**real.__dict__, "handler": does_nothing}))
    _, applied, _ = await _approve_and_apply(run)
    assert applied["applied"] == []
    assert applied["failed"] == ['Not done as asked: after the change, the agent "Front desk"\'s name should read '
                                 '"Harbor desk" but reads "Front desk".']
    assert applied["effect"].startswith(" Not done as asked")


async def test_a_delete_is_made_and_must_leave_nothing(client):
    agent = await _agent(name="Support bot")
    run = await _run()
    await _checked(run, [{"op": "delete_agent", "target": {"kind": "agent", "id": agent["id"]}}])
    _, applied, _ = await _approve_and_apply(run)
    assert applied["applied"] == ["delete_agent"] and await _get(agent["id"]) is None


async def test_a_new_schedule_is_made_paused_and_found_afterwards(client):
    from core.store import schedules
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name="Nightly digest")
    await save_orchestration(flow)
    run = await _run()
    await _checked(run, [{"op": "create_schedule", "target": {"kind": "schedule", "name": "Mondays"},
                          "fields": {"runs": {"kind": "workflow", "id": flow["id"]}, "prompt": "Go",
                                     "cron_expression": "0 9 * * 1"}}])
    _, applied, events = await _approve_and_apply(run)
    assert applied["applied"] == ["create_schedule"], applied
    [made] = [s for s in await schedules.load() if s["name"] == "Mondays"]
    assert made["enabled"] is False and made["cron_expression"] == "0 9 * * 1"
    assert any(e.get("type") == "resource_saved" and e.get("kind") == "schedule" for e in events)


async def test_the_card_keeps_each_before_image_never_a_secret(client):
    from core.boards import assistant

    agent = await _agent(name="Front desk", system_prompt="Use key sk-live-abcdefghijklmnopqrstuvwx1234 for calls.")
    run = await _run()
    await _checked(run, [{"op": "set_agent_instructions", "target": {"kind": "agent", "id": agent["id"]},
                          "fields": {"system_prompt": "Be brief."}},
                         {"op": "rename_agent", "target": {"kind": "agent", "id": agent["id"]},
                          "fields": {"name": "Desk"}}])
    _, applied, _ = await _approve_and_apply(run)
    assert applied["applied"] == ["set_agent_instructions", "rename_agent"]
    card = await assistant.card(run.run_id)
    assert card.stage == "changed" and card.status == "done"
    first, second = card.data["applied"]
    assert first["before"] == {"system_prompt": None, "_not_kept": ["system_prompt"]}
    assert second["before"] == {"name": "Front desk"}
    assert "sk-live" not in json.dumps(card.data)



async def test_two_ops_on_one_field_each_build_on_the_one_before(client):
    """"Give Front desk search_help and take away read_file" as two ops: each is checked against what the one before
    it leaves, so both apply and the second neither undoes the first nor reads as changed since the review (the
    branch review's finding 3)."""
    from core.store.resources import save_tool

    for name in ("read_file", "web_search", "search_help"):
        await save_tool({"name": name, "description": name, "tool_type": "http", "method": "GET",
                         "url": f"https://api.example.com/{name}", "headers": {}, "inputSchema": {"type": "object"}})
    agent = await _agent(name="Front desk", tools=["read_file", "web_search"], tool_policy={})
    run = await _run()
    await _checked(run, [
        {"op": "set_agent_tools", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"add": ["search_help"]}},
        {"op": "set_agent_tools", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"remove": ["read_file"]}},
        {"op": "set_tool_policy", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"open_world": "confirm"}},
        {"op": "set_tool_policy", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"destructive": "deny"}},
    ])
    _, applied, _ = await _approve_and_apply(run)
    assert applied["failed"] == [] and len(applied["applied"]) == 4, applied
    after = await _get(agent["id"])
    assert after["tools"] == ["web_search", "search_help"]
    assert after["tool_policy"] == {"destructive": "deny", "open_world": "confirm"}
