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


def _asked_for(run, ops) -> None:
    """The person asked for the renames the test proposes (F960: a new name must be their words), unless the test says
    what they asked itself."""
    names = [o["fields"]["name"] for o in ops or []
             if o.get("op", "").startswith("rename_") and (o.get("fields") or {}).get("name")]
    if names and "user_message" not in run.shared_state:
        run.shared_state["user_message"] = " and ".join(f'rename it to "{n}"' for n in names)


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw), events


async def _checked(run, ops):
    _asked_for(run, ops)
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


async def test_an_agent_s_skills_are_named_as_they_are_kept(client):
    """`attach_skills` keeps a skill's name lower-case with hyphens: "Customer FAQ" is `customer-faq`. The check reads
    the names the same way, so the change is made and found made, a remove in another case removes, and a skill that
    does not exist is said before the review, not refused after the yes (the branch review's finding 5)."""
    from core import skills

    await skills.save_library([{"name": n, "description": n, "body": n} for n in ("triage", "customer-faq")])
    agent = await _agent(name="Front desk", skills=["triage"])
    run = await _run()
    await _checked(run, [{"op": "set_agent_skills", "target": {"kind": "agent", "id": agent["id"]},
                          "fields": {"add": ["Customer FAQ"], "remove": ["TRIAGE"]}}])
    _, applied, _ = await _approve_and_apply(run)
    assert applied["failed"] == [] and applied["applied"] == ["set_agent_skills"], applied
    assert (await _get(agent["id"]))["skills"] == ["customer-faq"]

    unknown, _ = await _call("check_change", {"draft": {"finish": "submit_change", "ops": [
        {"op": "set_agent_skills", "target": {"kind": "agent", "id": agent["id"]},
         "fields": {"add": ["Refund rules"]}}]}}, await _run())
    assert unknown["outcome"] == "said" and unknown["say"] == 'No skill is called "refund-rules".'


async def test_an_apply_run_twice_makes_nothing_twice_and_says_it_was_done(client):
    """A redelivered job or a resumed step runs `apply_change` again for one approval (MCP2's finding 1): a new
    schedule is made once, and the second pass reports it done, found by its effect check, not made again or
    refused as "changed since"."""
    from core.store import schedules
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name="Nightly digest")
    await save_orchestration(flow)
    agent = await _agent(name="Front desk")
    run = await _run()
    await _checked(run, [
        {"op": "create_schedule", "target": {"kind": "schedule", "name": "Mondays"},
         "fields": {"runs": {"kind": "workflow", "id": flow["id"]}, "prompt": "Go", "cron_expression": "0 9 * * 1"}},
        {"op": "rename_agent", "target": {"kind": "agent", "id": agent["id"]}, "fields": {"name": "Desk"}}])
    _, first, _ = await _approve_and_apply(run)
    assert first["applied"] == ["create_schedule", "rename_agent"], first
    again, _ = await _call("apply_change", {}, run)
    assert again["applied"] == ["create_schedule", "rename_agent"] and again["failed"] == [], again
    assert [s["name"] for s in await schedules.load()].count("Mondays") == 1


async def _new_schedule_checked(name="Mondays"):
    from core.store.resources import save_orchestration
    from tests._fakes.seed import make_orchestration

    flow = make_orchestration(name="Nightly digest")
    await save_orchestration(flow)
    run = await _run()
    await _checked(run, [{"op": "create_schedule", "target": {"kind": "schedule", "name": name},
                          "fields": {"runs": {"kind": "workflow", "id": flow["id"]}, "prompt": "Go",
                                     "cron_expression": "0 9 * * 1"}}])
    return run, flow


async def test_a_create_run_again_after_a_restart_is_made_once(client):
    """MCP2's re-check, D: OSS keeps its spent-once claims in memory, per process. A run failed by a restart and
    resumed by the person, or a job redelivered to another worker (scale mode), runs `apply_change` again with no
    claim left: a delete or a write finds its target as it leaves it, but a create would be made a second time. A
    create finds what the earlier pass made, by name, and is done; the claim store is a second guard, not the only
    one."""
    from core.store import schedules

    run, _ = await _new_schedule_checked()
    _, first, _ = await _approve_and_apply(run)
    assert first["applied"] == ["create_schedule"], first
    confirm.set_claim_store(None)  # what a restart or another worker leaves
    again, _ = await _call("apply_change", {}, run)
    assert again["applied"] == ["create_schedule"] and again["failed"] == [], again
    assert [s["name"] for s in await schedules.load()].count("Mondays") == 1


async def test_a_name_taken_since_the_yes_is_not_made_again_under_it(client):
    """Someone else made a schedule with the approved name, set otherwise, after the review: nothing is made, and it is
    said; the other schedule is left as it is."""
    from core.store import schedules

    run, flow = await _new_schedule_checked()
    await schedules.save({"id": "sched_theirs", "name": "Mondays", "enabled": True, "target_type": "orchestration",
                          "target_id": flow["id"], "prompt": "Other", "schedule_type": "cron",
                          "cron_expression": "0 7 * * 1"})
    _, applied, _ = await _approve_and_apply(run)
    assert applied["applied"] == [], applied
    assert applied["failed"] == ['Not done: a schedule called "Mondays" was made since you approved this, so I made '
                                 'no second one. Ask again to change it.'], applied
    assert [s["id"] for s in await schedules.load() if s["name"] == "Mondays"] == ["sched_theirs"]
    assert (await schedules.get("sched_theirs"))["cron_expression"] == "0 7 * * 1"


@pytest.mark.parametrize("kind, writes, words, stage", [
    ("skill", [{"kind": "skill", "action": "created", "id": "polite-replies", "name": "polite-replies"}], "",
     "changed"),
    ("tool", [{"kind": "tool", "action": "created", "id": "weather_forecast", "name": "weather_forecast"}],
     'It was made as the HTTP tool "weather_forecast" (you approved "Weather Forecast Tool").', "set_aside"),
    ("agent", [{"kind": "skill", "action": "created", "id": "billing", "name": "billing"}],
     'The agent "Weather Forecast Tool" you approved was not made; made instead: the skill "billing".', "set_aside"),
    ("agent", [], 'The agent "Weather Forecast Tool" you approved was not made.', "set_aside"),
    # F1198: a Python tool is a tool once saved.
    ("python_tool", [{"kind": "tool", "action": "created", "id": "Weather Forecast Tool",
                      "name": "Weather Forecast Tool"}], "", "changed"),
    ("python_tool", [], 'The Python tool "Weather Forecast Tool" you approved was not made.', "set_aside"),
])
async def test_what_the_maker_made_is_held_to_what_was_approved(client, kind, writes, words, stage):
    """MCP2's note 5: the maker is a model calling create tools after the yes, outside the approval's digest. A code
    step reads what the run made (its own writes) and says plainly when it is not the approved thing: another name
    (batch 2b: "Weather Forecast Tool" approved, `weather_forecast` saved), another kind, or nothing. The card is
    `changed` only when the approved thing was made (MCP2's re-check, E: another name read as made as approved); what
    was made is kept on it either way."""
    from core.boards import assistant
    from core.orchestration.run_writes import WRITES_KEY

    name = "Polite replies" if kind == "skill" else "Weather Forecast Tool"
    run = await _run()
    run.shared_state["change_checked"] = json.dumps({"object": "change", "outcome": "create", "ops": [
        {"op": "create", "kind": "create", "status": "ready", "target": {"kind": kind, "name": name}}]})
    run.shared_state[WRITES_KEY] = writes
    got, _ = await _call("check_made", {}, run)
    assert got["note"] == (" " + words if words else "")
    card = await assistant.card(run.run_id)
    assert card.stage == stage
    assert card.data["made"] == [{"kind": w["kind"], "id": w["id"], "name": w["name"]} for w in writes]


async def test_the_same_change_approved_again_is_made_again(client):
    """MCP2's re-check, A: the spent-once key held nothing unique to the yes, so a second approval of an identical
    change (set it back, then set it again; undo, then redo) found its call already spent, and the person read "Not
    done as asked" for a change they had just approved. Each yes is its own record, and is spent once."""
    agent = await _agent(name="Front desk")
    for name in ("Desk", "Front desk", "Desk"):
        run = await _run()
        await _checked(run, _rename(agent["id"], name))
        _, applied, _ = await _approve_and_apply(run)
        assert applied["applied"] == ["rename_agent"] and applied["failed"] == [], (name, applied)
        assert (await _get(agent["id"]))["name"] == name
