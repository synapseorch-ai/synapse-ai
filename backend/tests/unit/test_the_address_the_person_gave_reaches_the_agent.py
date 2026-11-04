"""The address the person gave reaches the built agent as they gave it, never as its kept session's site (F1341;
CAP3's cap-01, round 2; eb 11-04).

cap-01's person names their supplier portal, http://portal.localhost:9411, and keeps a signed-in session for it. On
FF17 one run's step had no prompt, and the prompt writer wrote "the supplier portal at portal.localhost": the agent
opened http://portal.localhost/, port 80, got a 502 and posted "Supplier Portal Down". No code rewrote the address:
everything the builder hands the model names the site by its host (`browser.sites`, `browser.sessions`, the pack's
`signed_in_sites`), and the person's URL is only in their words. So code carries it:

- the plan check: an address the plan writes on a host the person gave at another scheme or port is sent back, in a
  tool step as in an agent's (the class); a step a browser agent runs on such a host holds the address as given, in
  its prompt or its new agent's purpose;
- the prompt writer: a new browser agent's instructions hold it; sent back once, then code adds the line itself, and
  the card shows what is saved;
- never a credential: a user:pass@ or a key-named query value is neither required nor copied.

A host's address the person never gave asks nothing here (F1253 asks about those).
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

PORTAL = "http://portal.localhost:9411"
OPENING = (f"Every morning, check our supplier portal at {PORTAL} for new orders and keep the tracking board "
           "current. I keep a signed-in session for it.")
LINE = "The address to open is {url}, as the person gave it."
#: c2's instructions on FF17 (ff17-c2, cap-01): the session's site, no port.
C2 = ("You are the Order Monitor. Every morning, you navigate the supplier portal at portal.localhost, find new "
      "orders and status changes, and keep a shared tracking board current.")
MONITOR = {"name": "Order Monitor", "purpose": "Checks the supplier portal for new orders",
           "tools": ["browser_navigate", "browser_snapshot", "browser_find"], "type": "browser",
           "browser": {"sites": ["portal.localhost"], "sessions": ["portal.localhost"], "mode": "read_only"}}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
def roles():
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _step(sid: str, prompt: str = "", **more) -> dict:
    return {"id": sid, "type": "agent", "name": sid.replace("_", " ").title(), "does": "Checks the portal",
            "agent": "Order Monitor", "prompt": prompt, "next": "end", **more}


def _draft(*steps: dict, agents: list[dict] | None = None) -> dict:
    return {"kind": "create", "title": "Supplier orders", "summary": "Tracks the portal's new orders.",
            "steps": list(steps), "agents": [MONITOR] if agents is None else agents}


def _ours(errors: list[str]) -> list[str]:
    return [e for e in errors if "as the person gave it" in e]


async def _plan_errors(draft: dict, opening: str = OPENING) -> list[str]:
    """What the plan check sends back about the person's addresses, read in a build's own run (F1290's way)."""
    run = await _run(opening=opening)
    with live(run):
        found = await build_plan.examine(draft, _Server(), state=run.shared_state)
    return _ours(found.errors)


async def _run(opening: str = OPENING, agents: list[dict] | None = None) -> OrchestrationRun:
    run = OrchestrationRun(run_id="builder_f1341", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": opening})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.CHECKED_KEY] = {"outcome": "prompts",
                                                "plan": {"agents": [MONITOR] if agents is None else agents}}
    return run


async def _prompts(run: OrchestrationRun, instructions: str, agent: str = "Order Monitor") -> dict:
    with live(run):
        raw, _ = await execute_builder_tool_with_events(
            "submit_prompts", {"prompts": [{"agent": agent, "instructions": instructions}]}, _Server(),
            run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestThePromptWriter:
    async def test_cap_01s_instructions_without_the_address_go_back_once_naming_it(self, client):
        sent = await _prompts(await _run(), C2)
        assert PORTAL in sent.get("error", ""), sent
        assert "as the person gave it" in sent["error"] and "portal.localhost" in sent["error"]

    async def test_the_second_time_code_adds_the_line_and_the_agents_prompt_holds_it(self, client):
        run = await _run()
        await _prompts(run, C2)
        out = await _prompts(run, C2)
        assert out.get("finish") == "submit_prompts", out
        written = out["prompts"]["Order Monitor"]
        assert written.startswith(C2) and written.endswith(LINE.format(url=PORTAL)), written

    @pytest.mark.parametrize("instructions", [
        f"Open {PORTAL} with browser_navigate and read the waiting orders.",
        f"Open {PORTAL}/orders and read the waiting orders.",  # a deeper path on the address as given
        f"The portal is at {PORTAL}.",  # a sentence's full stop after it
    ])
    async def test_instructions_holding_the_address_are_taken_as_written(self, client, instructions):
        out = await _prompts(await _run(), instructions)
        assert out.get("finish") == "submit_prompts" and out["prompts"]["Order Monitor"] == instructions, out

    @pytest.mark.parametrize("wrong", ["http://portal.localhost/", "https://portal.localhost:9411",
                                       "http://portal.localhost:94110"])
    async def test_the_portal_at_another_scheme_or_port_goes_back(self, client, wrong):
        sent = await _prompts(await _run(), f"Open {wrong} and read the waiting orders.")
        assert PORTAL in sent.get("error", ""), sent

    async def test_no_address_given_asks_nothing(self, client):
        out = await _prompts(await _run(opening="Check our supplier portal for new orders every morning."), C2)
        assert out.get("finish") == "submit_prompts" and out["prompts"]["Order Monitor"] == C2, out

    async def test_an_address_for_another_host_is_not_this_agents(self, client):
        out = await _prompts(await _run(opening="Post the new orders to https://shop.example.com/orders each day."),
                             C2)
        assert out.get("finish") == "submit_prompts" and out["prompts"]["Order Monitor"] == C2, out

    async def test_a_plain_agent_is_not_asked(self, client):
        plain = {"name": "Order Monitor", "purpose": "Summarises the orders", "tools": []}
        out = await _prompts(await _run(agents=[plain]), "Summarise the orders you are given.")
        assert out.get("finish") == "submit_prompts", out


class TestThePlan:
    async def test_cap_01s_empty_step_goes_back_naming_the_step_and_the_address(self, client):
        errors = await _plan_errors(_draft(_step("check_orders")))
        assert len(errors) == 1 and "check_orders" in errors[0] and PORTAL in errors[0], errors
        assert "browser.sessions" in errors[0], "the session's site is said not to be the address to open"

    @pytest.mark.parametrize("draft", [
        _draft(_step("check_orders", f"Open {PORTAL} and read the waiting orders.")),
        _draft(_step("check_orders"), agents=[{**MONITOR, "purpose": f"Checks the supplier portal at {PORTAL}"}]),
    ])
    async def test_the_address_in_the_step_or_its_new_agent_passes(self, client, draft):
        assert await _plan_errors(draft) == []

    async def test_a_plan_writing_the_portal_at_another_port_goes_back_in_a_tool_step_too(self, client):
        read = {"id": "read_orders", "type": "tool", "name": "Read the orders", "does": "Reads the orders page",
                "tool": "scrape_url", "config": {"tool_arguments": {"url": "http://portal.localhost/orders"}},
                "next": "end"}
        errors = await _plan_errors(_draft(read, agents=[]))
        assert errors and "http://portal.localhost/orders" in errors[0] and PORTAL in errors[0], errors

    async def test_no_address_given_asks_nothing(self, client):
        assert await _plan_errors(_draft(_step("check_orders")), opening="Check our supplier portal daily.") == []


class TestNeverACredential:
    async def test_a_user_and_password_or_a_key_in_the_query_is_neither_required_nor_copied(self, client):
        password, key = "hunter" + "2x9", "k" + "3y" * 6
        given = f"https://ops:{password}@portal.localhost:9411/x?api_key={key}&page=2"
        required = "https://portal.localhost:9411/x?page=2"
        opening = f"Check the supplier portal at {given} every morning."
        run = await _run(opening=opening)
        sent = await _prompts(run, C2)
        out = await _prompts(run, C2)
        plan_errors = await _plan_errors(_draft(_step("check_orders")), opening=opening)
        assert required in sent.get("error", "") and required in plan_errors[0], (sent, plan_errors)
        assert out["prompts"]["Order Monitor"].endswith(LINE.format(url=required)), out
        for said in (json.dumps(sent), json.dumps(out), json.dumps(plan_errors)):
            assert password not in said and key not in said and "ops:" not in said
