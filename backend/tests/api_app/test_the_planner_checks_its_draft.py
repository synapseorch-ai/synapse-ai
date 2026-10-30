"""D340 D3 (BLD1; TRI1's spec §3.6, §4): the planner checks its own draft, and a fetch is a Tool step.

`check_draft` is one more planner read: examine's own rules on a draft (the parts, `_rules`, the save rules dry), the
census's imitation findings (GUIDE1's `imitation.findings`) and the structure code can tell, so a miss is fixed in the
planner's own turn. It writes nothing to the run, shows no card and looks nothing up; submitting still runs everything.

Structure, where code can tell (`structure.issues`): a new agent whose one tool only reads (a page read, or a GET tool
here that says it only reads) needs no model, so its step is a Tool step. It goes back to the planner once, then is
said on the card: never a reason a plan cannot be built. An agent already here is the person's, and left alone; a plan
with no agents at all is a plan like any other.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def desk(client):
    from core.store.resources import save_tool

    builder_access.forget_runs()
    for name, method, read in (("read_orders", "GET", True), ("send_digest", "POST", False)):
        await save_tool({"name": name, "description": f"{name} on the shop's API.", "tool_type": "http",
                         "method": method, "url": f"https://api.shop.example/v1/{name}", "headers": {},
                         "inputSchema": {"type": "object", "properties": {}},
                         "annotations": {"readOnlyHint": read}})
    await S.seed_agents([S.make_agent(id="agent_reader", name="Order reader", tools=["read_orders"])])
    yield
    builder_access.forget_runs()


async def _run() -> OrchestrationRun:
    run = OrchestrationRun(run_id="builder_d3", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": "Each morning, read the orders and send a digest."})
    await SharedState(run).checkpoint()
    return run


async def _call(run, tool: str, args: dict):
    from core.orchestration.engine import live

    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


READ = {"id": "read", "type": "agent", "name": "Read the orders", "does": "Reads the orders",
        "agent": "Order fetcher", "prompt": "Read today's orders.", "next": "digest"}
DIGEST = {"id": "digest", "type": "llm", "name": "Write the digest", "does": "Writes the digest",
          "prompt": "Write a short digest of {{ state.read }}.", "next": "end"}
FETCHER = {"name": "Order fetcher", "purpose": "Reads the orders", "tools": ["read_orders"]}


def _plan(steps, agents=(), **more) -> dict:
    return {"kind": "create", "title": "Order digest", "summary": "Reads the orders and sends a digest.",
            "steps": list(steps), "agents": list(agents), **more}


class TestTheCheck:
    async def test_it_is_a_read_and_writes_nothing_to_the_run(self, client):
        assert builder_access.KINDS["check_draft"] == "read"
        run = await _run()
        before = dict(run.shared_state)
        out, events = await _call(run, "check_draft", {"draft": _plan([READ, DIGEST], [FETCHER])})
        assert out["structure"] and run.shared_state == before, "no sendback, no checked plan"
        assert build_plan.CHECKED_KEY not in run.shared_state
        assert not [e for e in events if e.get("type") in ("connection_needed", "action_needed")], "no card"

    async def test_it_says_what_submitting_would_send_back(self, client):
        run = await _run()
        out, _ = await _call(run, "check_draft", {"draft": _plan([{**DIGEST, "next": "nowhere"}])})
        assert out["ok"] is False and any("nowhere" in e for e in out["errors"])

    async def test_a_plan_with_no_agents_is_a_plan_like_any_other(self, client):
        run = await _run()
        tool_read = {"id": "read", "type": "tool", "name": "Read the orders", "does": "Reads the orders",
                     "tool": "read_orders", "next": "digest"}
        out, _ = await _call(run, "check_draft", {"draft": _plan([tool_read, DIGEST])})
        assert out["ok"] is True and out["errors"] == [] and out["structure"] == [], out


class TestAFetchIsAToolStep:
    async def test_a_new_agent_that_only_reads_is_said_with_the_tool_step_instead(self, client):
        run = await _run()
        out, _ = await _call(run, "check_draft", {"draft": _plan([READ, DIGEST], [FETCHER])})
        assert out["structure"] == [
            "structure: step read: \"Order fetcher\" only reads with `read_orders`, which needs no model: make it a "
            "Tool step calling `read_orders`, and have the next step read its output as {{ state.read }}"]

    @pytest.mark.parametrize("plan", [
        # The person's own agent, already here: theirs, left alone.
        _plan([{**READ, "agent": "agent_reader"}, DIGEST], reuse_agents=["agent_reader"]),
        # Two tools, or a tool that acts: judgement, or more than a read.
        _plan([READ, DIGEST], [{**FETCHER, "tools": ["read_orders", "send_digest"]}]),
        _plan([{**READ, "agent": "Digest sender"}, DIGEST], [{"name": "Digest sender", "purpose": "Sends",
                                                             "tools": ["send_digest"]}]),
    ])
    async def test_an_agent_already_here_or_one_that_does_more_is_left_alone(self, client, plan):
        run = await _run()
        out, _ = await _call(run, "check_draft", {"draft": plan})
        assert out["structure"] == [], out

    async def test_it_goes_back_once_then_the_card_says_it(self, client):
        run = await _run()
        draft = _plan([READ, DIGEST], [FETCHER])
        first, _ = await _call(run, "submit_plan", draft)
        assert first["error"].startswith("Before this goes to the person, a better shape (said once")
        again, _ = await _call(run, "submit_plan", draft)
        assert again.get("finish") == "submit_plan", "sent as it is: the planner's to keep"
        # A later pass of the same build (a question round's re-plan) never sends it back again (TRI1).
        run.shared_state.pop(build_plan._SENDBACK_KEY, None)
        third, _ = await _call(run, "submit_plan", draft)
        assert third.get("finish") == "submit_plan"
        with __import__("core.orchestration.engine", fromlist=["live"]).live(run):
            found = await build_plan.examine(draft, _Server(), state=run.shared_state)
        assert any(n.startswith("step read: \"Order fetcher\" only reads") for n in found.notes)
        assert found.errors == [], "never a reason a plan cannot be built"


class TestAScreenRebuiltByHand:
    async def test_an_eval_made_by_hand_is_said(self, client):
        await S.seed_orchestrations([S.make_orchestration(id="orch_posts", name="Post generator")])
        run = await _run()
        draft = _plan([{"id": "gen", "type": "workflow", "name": "Run it", "does": "Runs it", "workflow": "orch_posts",
                        "next": "judge"},
                       {"id": "judge", "type": "llm", "name": "Judge", "does": "Scores it",
                        "prompt": "Score {{state.gen}} from 1 to 10.", "next": "end"}])
        out, _ = await _call(run, "check_draft", {"draft": draft})
        assert out["ok"] is False and out["imitations"][0].startswith('That is an eval of "Post generator"')
