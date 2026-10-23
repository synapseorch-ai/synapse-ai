"""
A build's card lists every approval its run will ask for: who asks, before which tool, and what set it (F964,
SAI2-P3, D300; SAI2's four points, 10-23).

REL2's demo on v2.0 cd75095d: the card said the save is approved "in the Approve Summary human step", and the run
asked a second time, because the Vault Clerk's tool policy held `vault_write` on confirm. P3a's card drew its approvals
from TAG1's tags and the person's choice only. Now `fill` resolves each gate as the engine will
(`tool_policy.decide(tool, resolve_policy(agent, settings))`), for the plan's new agents, the agents it reuses and its
Tool steps:

* a gate is "<who> asks you before `<tool>`, each time", with what set it: this build, the agent's own tool policy or
  the workspace's, and it is asked as well as any Human step;
* a block on a tool the plan names is a blocking issue in the person's words, and Build it is not offered;
* "Let X act without asking" is offered only where pressing it changes what the run asks;
* the gates are part of what the yes covers, and the save stops when one changed since the review.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from _fakes.knowledge_env import use_saved_settings
from core import builder_access, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

WEATHER = {"name": "rotterdam_weather", "description": "Today's weather in Rotterdam.", "tool_type": "http",
           "method": "GET", "url": "https://api.weather.example/v1/rotterdam", "headers": {},
           "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}}
#: No TAG1 tags: undeclared, so it acts (SAI2's point 4).
FILE = {"name": "file_summary", "description": "Files the summary in the vault.", "tool_type": "http",
        "method": "POST", "url": "https://api.files.example/v1/file", "headers": {},
        "inputSchema": {"type": "object", "properties": {}}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    from core.store.resources import save_tool

    use_saved_settings(monkeypatch)
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    await save_tool(dict(WEATHER))
    await save_tool(dict(FILE))
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)


async def _policy(**defaults):
    from core.routes.settings import save_settings

    await save_settings({"tool_policy_defaults": defaults})


async def _clerk(**policy):
    await S.seed_agents([S.make_agent(id="agent_clerk", name="Vault Clerk", tools=["rotterdam_weather", "file_summary"],
                                      tool_policy=policy)])


CHECK = {"id": "check", "type": "tool", "name": "Check the weather", "does": "Reads today's weather",
         "tool": "rotterdam_weather"}
APPROVE = {"id": "approve", "type": "human", "name": "Approve Summary", "does": "The person reads the summary",
           "prompt": "File this summary?"}
FILE_IT = {"id": "file", "type": "agent", "name": "File the summary", "does": "Files the summary",
           "agent": "agent_clerk", "prompt": "File {state.check}."}


def _plan(*steps, **more):
    return {"finish": "submit_plan", "kind": "create", "title": "Weekly summary", "summary": "Files a summary.",
            "steps": list(steps), **more}


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _checked(draft, run=None):
    run = run or OrchestrationRun(run_id="builder_gates", orchestration_id="orch_native_builder",
                                  shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create"})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = draft
    out, _ = await _call("check_build", {"draft": draft}, run)
    if out["outcome"] == "prompts":
        run.shared_state[build_plan.CHECKED_KEY] = out
        written, _ = await _call("submit_prompts", {"prompts": [{"agent": a["name"], "instructions": "File it."}
                                                                for a in out["plan"]["agents"]]}, run)
        run.shared_state[build_plan.PROMPTS_KEY] = written
        out, _ = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = out
    return run, out


def _gates(out):
    return [(g["who"], g["tool"], g["decision"], g["set_by"]) for g in out["plan"]["gates"]]


async def test_rel2s_demo_a_human_step_and_the_agents_own_confirm_are_both_on_the_card(client):
    """The repro: an Approve Summary step, then an agent whose own policy asks before it files."""
    await _clerk(**{"tool:file_summary": "confirm"})
    _, out = await _checked(_plan(CHECK, APPROVE, FILE_IT))
    assert _gates(out) == [("Vault Clerk", "file_summary", "confirm", "agent")]
    assert "Vault Clerk asks you before `file_summary`, each time (its own tool policy)" in out["review"]
    assert "as well as the Human steps" in out["review"]
    assert "file_summary" in out["plan"]["approvals"]["asks_first"]


async def test_a_new_agent_asks_first_because_this_build_says_so_and_the_person_may_lift_it(client):
    clerk = {"name": "Vault Clerk", "purpose": "Files summaries in the vault", "tools": ["file_summary"]}
    _, out = await _checked(_plan(CHECK, APPROVE, {**FILE_IT, "agent": "Vault Clerk"}, agents=[clerk]))
    assert _gates(out) == [("Vault Clerk", "file_summary", "confirm", "build")]
    assert "Vault Clerk asks you before `file_summary`, each time" in out["review"]
    assert "alone:file_summary" in out["offers"]


async def test_a_reused_agents_own_confirm_on_a_read_is_listed_and_cannot_be_lifted_here(client):
    """SAI2's point 2: a press on "Let X act without asking" would change nothing the run asks, so it is not offered,
    and the words are not read as one either."""
    await _clerk(**{"tool:rotterdam_weather": "confirm"})
    tell = {**FILE_IT, "prompt": "Read the weather."}
    run, out = await _checked(_plan(tell))
    assert ("Vault Clerk", "rotterdam_weather", "confirm", "agent") in _gates(out)
    assert "rotterdam_weather" in out["plan"]["approvals"]["asks_first"]
    assert "alone:rotterdam_weather" not in out["offers"]
    run.shared_state["review_feedback"] = {"feedback": "let rotterdam_weather act without asking"}
    answer, _ = await _call("read_plan_answer", {}, run)
    assert answer == {"outcome": "revise"}


async def test_a_reused_agents_category_policy_asks_for_an_untagged_tool(client):
    """SAI2's point 4: a tool with no tags is undeclared, and an `undeclared: confirm` asks before it."""
    await _clerk(undeclared="confirm")
    _, out = await _checked(_plan(FILE_IT))
    assert ("Vault Clerk", "file_summary", "confirm", "agent") in _gates(out)
    assert ("Vault Clerk", "rotterdam_weather", "confirm", "agent") not in _gates(out)


async def test_the_workspaces_default_asks_for_a_reused_agent_and_for_a_tool_step(client):
    await _clerk()
    await _policy(undeclared="confirm")
    post = {"id": "post", "type": "tool", "name": "File it", "does": "Files it", "tool": "file_summary"}
    _, out = await _checked(_plan(APPROVE, FILE_IT, {**post, "id": "post"}))
    gates = _gates(out)
    assert ("Vault Clerk", "file_summary", "confirm", "workspace") in gates
    assert ("File it", "file_summary", "confirm", "workspace") in gates
    assert "(this workspace's tool policy)" in out["review"]
    assert "alone:file_summary" not in out["offers"]


async def test_a_tool_the_person_let_act_alone_still_asks_where_the_workspace_says_so(client):
    """The person's press lifts this build's own confirm only: the workspace's still asks, and the card says so."""
    await _policy(undeclared="confirm")
    clerk = {"name": "Vault Clerk", "purpose": "Files summaries in the vault", "tools": ["file_summary"]}
    run = OrchestrationRun(run_id="builder_gates", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.ANSWER_KEY: {"outcome": "alone", "alone": ["file_summary"]}})
    _, out = await _checked(_plan(APPROVE, {**FILE_IT, "agent": "Vault Clerk"}, agents=[clerk]), run)
    assert _gates(out) == [("Vault Clerk", "file_summary", "confirm", "workspace")]
    assert out["plan"]["approvals"]["acts_without_asking"] == []


async def test_a_blocked_tool_the_plan_uses_is_a_blocking_issue_not_an_ask(client):
    """SAI2's point 1: deny is not ask."""
    await _clerk()
    await _policy(undeclared="deny")
    post = {"id": "post", "type": "tool", "name": "File it", "does": "Files it", "tool": "file_summary"}
    _, out = await _checked(_plan(APPROVE, post))
    assert ("File it", "file_summary", "deny", "workspace") in _gates(out)
    assert "file_summary" not in out["plan"]["approvals"]["asks_first"]
    assert "build" not in out["offers"]
    assert any("this workspace's tool policy blocks `file_summary`" in i for i in out["issues"])


async def test_a_reused_agents_blocked_tool_the_plan_does_not_name_is_said_but_does_not_block(client):
    """A reused agent cannot use what is blocked today either; the build does not change that, so it is said."""
    await _clerk(undeclared="deny")
    _, out = await _checked(_plan({**FILE_IT, "prompt": "Read the weather."}))
    assert ("Vault Clerk", "file_summary", "deny", "agent") in _gates(out)
    assert "build" in out["offers"]
    assert "Vault Clerk can't use `file_summary`: its own tool policy blocks it" in out["review"]


async def test_the_gates_are_covered_by_the_yes_and_a_policy_changed_since_stops_the_save(client):
    """SAI2's point 3: the card promised what the run asks; a policy moved since the review is not what was approved."""
    from core.native_builder.build_plan import reviewed_of
    from core.product_tools.approved import digest

    await _clerk(**{"tool:file_summary": "confirm"})
    run, out = await _checked(_plan(CHECK, APPROVE, FILE_IT))
    assert out["digest"] == digest(reviewed_of(out)) and "gates" in reviewed_of(out)["plan"]
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    await _clerk()  # the Vault Clerk's own confirm is taken off after the yes
    saved, events = await _call("save_build", {}, run)
    assert saved["outcome"] == "stopped" and not [e for e in events if e.get("type") == "resource_saved"]
    [stopped] = [e for e in events if e.get("type") == "build_stopped"]
    assert "Vault Clerk" in stopped["reason"] and "`file_summary`" in stopped["reason"]
    assert "Nothing was built" in stopped["reason"]


def test_the_card_says_each_gate_in_the_reviews_words():
    """`plan.ts` draws the card from the object; its `gateWords` is `render.gate_words`, line for line."""
    from pathlib import Path

    plan_ts = (Path(__file__).resolve().parents[3] / "frontend/src/components/assistant/plan.ts").read_text()
    for words in ("asks you before \\`${gate.tool}\\`, each time${by}", "' (its own tool policy)'",
                  "\" (this workspace's tool policy)\"", "can't use \\`${gate.tool}\\`: ${whose} blocks it",
                  "The step \"${gate.who}\"", "These are asked as well as the Human steps above."):
        assert words in plan_ts, words
