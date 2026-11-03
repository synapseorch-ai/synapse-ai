"""An agent that works a site the request names is a browser agent with that site (F1301, CAP3's cap-05 A2; 79 11-03).

cap-05's A2 plan (63dfb505) gave two plain agents the in-app browser's tools, one to look an order up and one to fill
the refund form, on the portal the person named (http://portal.localhost:9411). A plain agent has no sites, so its
first keystroke there was the person's turn ("The agent wants to type on portal.localhost"), and the run stalled on
it. A browser agent (`type: browser`) with that site in `browser.sites` types there without a turn: read_only when it
only looks, co_drive when it types, presses or sends. Code tells the shape from the plan alone: a new agent of type
agent holding the browser's tools, on steps whose words name an http(s) site. A # Structure finding (D340 D3), said
once and then on the card, never a reason a plan cannot be built, never a rewrite.

And the safety the run kept (79's (a), the F1289 rule): a step before the approval whose agent can press, type or send
in the browser without asking goes back to the planner (BWA1's decision 4), and with the confirm policy it does not.
This pin is green on the base: CAP3's red there was its scorer counting the browser's read-only tools.
"""
from __future__ import annotations

import types

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.browser import client as browser_client
from core.browser.gate import ALLOWED
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, objects as O, structure
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.tool_router import ToolRouter

PORTAL = "http://portal.localhost:9411"
LOOKS = ["browser_navigate", "browser_snapshot", "browser_find"]
WORKS = [*LOOKS, "browser_type", "browser_click"]


def _step(sid: str, agent: str, prompt: str, nxt: str = "end") -> dict:
    return {"id": sid, "type": "agent", "name": sid.replace("_", " ").title(), "does": "Works the portal",
            "agent": agent, "prompt": prompt, "next": nxt}


def _agent(name: str, tools: list[str], **more) -> dict:
    return {"name": name, "purpose": "Works the refunds portal", "tools": tools, **more}


def _plan(steps: list[dict], agents: list[dict]) -> O.Plan:
    return O.Plan.from_draft({"kind": "create", "title": "Refunds", "summary": "Refunds an order on the portal.",
                              "steps": steps, "agents": agents})


CHECK = _step("check_order", "Portal Order Checker", f"Navigate to {PORTAL} and look up order {{{{ state.order }}}}.")


def _found(found: list[str]) -> list[str]:
    return [f for f in found if "browser agent" in f]


class TestTheShape:
    async def test_cap_05s_checker_is_said_to_be_a_browser_agent_on_the_portal(self, client):
        said = _found(await structure.issues(_plan([CHECK], [_agent("Portal Order Checker", WORKS)]),
                                             build_plan.Workspace()))
        assert said == [
            'structure: agent "Portal Order Checker" works portal.localhost with the browser\'s tools: make it a '
            'browser agent (type browser) with browser.sites ["portal.localhost"] and browser.mode co_drive, so it '
            'types there without stopping for the person; read_only when it only looks'], said

    async def test_one_that_only_looks_is_read_only(self, client):
        said = _found(await structure.issues(_plan([CHECK], [_agent("Portal Order Checker", LOOKS)]),
                                             build_plan.Workspace()))
        assert said and "browser.mode read_only" in said[0], said

    @pytest.mark.parametrize("steps,agents", [
        # No site named: any public site, the person's to allow.
        ([_step("look", "Looker", "Look the order up on the portal.")], [_agent("Looker", WORKS)]),
        # A browser agent already, with its site.
        ([CHECK], [_agent("Portal Order Checker", [], type="browser",
                          browser={"sites": ["portal.localhost"], "mode": "co_drive"})]),
        # No browser tools.
        ([CHECK], [_agent("Portal Order Checker", ["scrape_url"])]),
    ])
    async def test_otherwise_it_is_not_said(self, client, steps, agents):
        assert _found(await structure.issues(_plan(steps, agents), build_plan.Workspace())) == []


# ── the safety (79's (a)): a send the browser can make before the approval asks first ──────────────────────────
class _Server:
    """The in-app browser registered as the server registers it (`server._register_browser`), its tools tagged as
    the component vouches (`gate.ALLOWED`)."""
    agent_sessions = {browser_client.SERVER_NAME: object()}
    tool_router = ToolRouter()
    _session_tools = {browser_client.SERVER_NAME: [
        types.SimpleNamespace(name=n, description=n, inputSchema={"type": "object", "properties": {}},
                              annotations={"readOnlyHint": bool(t.get("read_only")),
                                           "openWorldHint": bool(t.get("open_world"))})
        for n, t in ALLOWED.items()]}


for _name in ALLOWED:
    _Server.tool_router.register(browser_client.SERVER_NAME, _name)


async def _examine(draft: dict):
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_f1301", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": f"Check the order on {PORTAL}, ask me, then refund it."})
    await SharedState(run).checkpoint()
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


APPROVE = {"id": "approve", "type": "human", "name": "Approve the refund", "does": "The person approves",
           "prompt": "Refund it?", "next": "end"}


def _draft(**policy) -> dict:
    return {"kind": "create", "title": "Refunds", "summary": "Refunds an order on the portal.",
            "steps": [{**CHECK, "next": "approve"}, APPROVE],
            "agents": [_agent("Portal Order Checker", WORKS)], **policy}


async def test_a_press_before_the_approval_without_asking_goes_back(client):
    found = await _examine(_draft())
    assert any("can run `browser_click`" in e and "without asking" in e for e in found.errors), found.errors


async def test_with_the_confirm_policy_it_does_not(client):
    found = await _examine(_draft(approvals={"asks_first": ["browser_click", "browser_type"]}))
    assert not [e for e in found.errors if "without asking" in e], found.errors


# ── F1301's own steer does not route around decision 4 (79, 11-03) ──────────────────────────────────────────────
# A browser agent with its site types there without stopping for the person, so before an approval the only guard
# left is decision 4's ask-first. A Browser agent holds the browser's tools however it was made (`_type_tools`): its
# presses are held whether the plan lists them or not; read_only sends nothing at all (the component's network gate),
# so its presses are not.
ACTING = sorted(n for n, t in ALLOWED.items() if not t.get("read_only"))


def _browser_draft(tools: list[str], mode: str = "co_drive", **policy) -> dict:
    agent = _agent("Portal Order Checker", tools, type="browser",
                   browser={"sites": ["portal.localhost"], "mode": mode})
    return {**_draft(**policy), "agents": [agent]}


async def test_a_co_drive_browser_agents_listed_press_before_the_approval_goes_back(client):
    found = await _examine(_browser_draft(WORKS))
    assert any("can run `browser_click`" in e and "without asking" in e for e in found.errors), found.errors
    found = await _examine(_browser_draft(WORKS, approvals={"asks_first": ["browser_click", "browser_type"]}))
    assert not [e for e in found.errors if "without asking" in e], found.errors


async def test_a_co_drive_browser_agent_holds_the_browsers_presses_unlisted(client):
    found = await _examine(_browser_draft([]))
    assert any("can run `browser_click`" in e and "without asking" in e for e in found.errors), found.errors
    found = await _examine(_browser_draft([], approvals={"asks_first": ACTING}))
    assert not [e for e in found.errors if "without asking" in e], found.errors


async def test_a_read_only_browser_agent_sends_nothing(client):
    found = await _examine(_browser_draft([], mode="read_only"))
    assert not [e for e in found.errors if "without asking" in e], found.errors
