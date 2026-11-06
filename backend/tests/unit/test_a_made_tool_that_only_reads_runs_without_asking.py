"""A tool Synapse AI makes that only reads runs without asking first (F1424; BLD1's clip 5 take4 on v2.0 2429a3a1;
Naveen 11-06, in eb's tab and CAP3's: a made GET with no key runs without asking, the person's own spec's too; asking
is turned on per tool in the agent editor).

take4's Frankfurter plan made three keyless GET tools, and each was under "Asks you first": every tool the research
found was taken to act ("an outside service: it acts until shown otherwise"), so each rate lookup would have waited
on the person. Now a tool this build makes after the yes that only reads is not asked first: an `http_tool` it makes
(the research's spec or the person's own), a GET (no body), taking no key (no header), with no account part in its
host, its base locked as made. The card says so beside it. Everything else asks as before: any other method, a key, a
host part, and every tool already here. The run asks whenever the agent's own policy says so for that tool.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_research as br, builder_tools
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.native_builder import objects as O
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

DOCS = "https://frankfurter.dev/"
BLANK = {"in": "header", "name": "", "scheme": ""}
KEY = {"in": "header", "name": "X-Api-Key", "scheme": ""}
READS = "It only reads, so it runs without asking you first."


def _spec(method="GET", path="/latest", auth=BLANK, base="https://api.frankfurter.dev/v1", **more) -> dict:
    return {"base_url": base, "auth": auth, "docs_url": DOCS, **more, "endpoints": [
        {"method": method, "path": path, "does": "Today's rates", "tool": "frankfurter_rates"}]}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    hooks.set_role_source(owner)
    monkeypatch.setattr(build_plan, "MAKES_HTTP_TOOLS", True)
    await S.seed_agents([S.make_agent(id="agent_desk", name="Front desk", tools=[])])
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()
    builder_tools.set_room_check(None)
    br._LOOSE.clear()


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _checked(api) -> tuple[OrchestrationRun, dict]:
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["frankfurter.dev"]}
    option = {"provider": "Frankfurter", "how": "api", "detail": "Today's exchange rates, free and open",
              "needs": "No account", "sources": [DOCS], "own_domains": ["frankfurter.dev"], "api": api}
    [found] = (await br.report_findings({"goal": "rates on request", "options": [option]}, None, []))["options"]
    assert found.get("api"), found
    run = OrchestrationRun(run_id="builder_rates", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: {"goal": "rates on request", "options": [found]}})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Rates desk", "summary": "Answers about rates.",
             "agents": [{"name": "Rates desk", "purpose": "Answers questions about exchange rates",
                         "tools": ["frankfurter_rates"]}],
             "steps": [{"id": "ask", "type": "agent", "name": "Answer", "does": "Answers with today's rates",
                        "agent": "Rates desk"}]}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, _ = await _call("check_build", {"draft": draft}, run)
    if checked["outcome"] == "prompts":  # its new agent's instructions, written, then the card
        run.shared_state[build_plan.CHECKED_KEY] = checked
        written, _ = await _call("submit_prompts", {"prompts": [{"agent": "Rates desk", "instructions":
                                                                 "Answer with today's rates."}]}, run)
        run.shared_state[build_plan.PROMPTS_KEY] = written
        checked, _ = await _call("check_build", {"draft": draft}, run)
    return run, checked


class TestTake4:
    async def test_a_keyless_get_is_not_asked_first(self):
        _run, checked = await _checked(_spec())
        assert "build" in checked["offers"], checked["say"]
        approvals = checked["plan"]["approvals"]
        assert "frankfurter_rates" not in approvals["asks_first"], approvals
        assert "frankfurter_rates" not in approvals["acts_without_asking"]
        assert "alone:frankfurter_rates" not in checked["offers"]
        assert "## Asks you first" not in checked["review"]
        assert (f"`frankfurter_rates`: GET /latest on api.frankfurter.dev (Frankfurter). It takes no key. {READS}"
                in checked["review"]), checked["review"]

    async def test_the_built_agent_runs_it_without_asking_and_asks_when_its_policy_says(self, client):
        from core.config import load_settings
        from core.routes.agents import load_user_agents
        from core.routes.tools import load_custom_tools
        from core.tool_policy import TOOL_KEY, decide, resolve_policy
        from core.tools import resolve_one_tool

        run, checked = await _checked(_spec())
        run.shared_state[build_plan.CHECKED_KEY] = checked
        run.shared_state["review_feedback"] = {"feedback": "Build it"}
        approved, _ = await _call("approve_plan", {}, run)
        run.shared_state["plan_approval"] = approved
        made, _ = await _call("save_build", {"only": "tools"}, run)
        assert made["outcome"] == "tools_made", made
        saved, _ = await _call("save_build", {}, run)
        assert saved["outcome"] == "saved", saved
        [desk] = [a for a in await load_user_agents() if a.get("name") == "Rates desk"]
        assert f"{TOOL_KEY}frankfurter_rates" not in (desk.get("tool_policy") or {}), desk.get("tool_policy")
        custom = await load_custom_tools()
        [made_tool] = [t for t in custom if t["name"] == "frankfurter_rates"]
        assert made_tool["base_lock"] == "https://api.frankfurter.dev/v1" and not made_tool.get("headers")
        tool = await resolve_one_tool(_Server(), "frankfurter_rates", {"tools": ["frankfurter_rates"]}, custom)
        settings = load_settings()
        assert decide(tool, resolve_policy(desk, settings))[0] == "allow"
        # Asking turned on for it in the agent editor (its tool policy) is asked, as for any tool.
        asked = {**desk, "tool_policy": {f"{TOOL_KEY}frankfurter_rates": "confirm"}}
        assert decide(tool, resolve_policy(asked, settings)) == ("confirm", "tool")


@pytest.mark.parametrize("api", [
    _spec(auth=KEY),
    _spec(method="POST", path="/alerts"),
    _spec(method="PUT", path="/alerts/{id}"),
    _spec(method="DELETE", path="/alerts/{id}"),
    _spec(base="https://{region}.frankfurter.dev/v1", host_params=[{"name": "region", "says": "eu or us"}]),
], ids=["keyed-get", "post", "put", "delete", "host-part"])
async def test_everything_else_still_asks_first(api):
    _run, checked = await _checked(api)
    assert "frankfurter_rates" in checked["plan"]["approvals"]["asks_first"], checked["plan"]["approvals"]
    assert "## Asks you first" in checked["review"] and READS not in checked["review"]


def _status(**api) -> O.ToolStatus:
    endpoint = {"method": "GET", "path": "/latest", "does": "", "tool": "rates"}
    return O.ToolStatus(name="rates", status="found", provider="Rates", via="http_tool",
                        api={"base_url": "https://api.rates.example", "auth": None, "keyless": True,
                             "host_params": [], "endpoint": endpoint, **api})


class TestWhatOnlyReads:
    def test_the_persons_own_spec_too(self):
        own = _status(own=True, tool={"url": "https://api.rates.example/latest", "inputSchema": {}, "description": ""})
        assert O.only_reads(own) is True

    @pytest.mark.parametrize("status", [
        _status(keyless=False, auth={"in": "header", "name": "X-Api-Key", "scheme": ""}),
        _status(endpoint={"method": "POST", "path": "/latest", "does": "", "tool": "rates"}),
        _status(host_params=[{"name": "region", "says": ""}]),
        O.ToolStatus(name="rates", status="present"),
        O.ToolStatus(name="rates", status="found", provider="Rates", via="mcp_server"),
        O.ToolStatus(name="rates", status="found", provider="Rates", via="none"),
    ], ids=["keyed", "post", "host-part", "present", "mcp", "lead"])
    def test_nothing_else(self, status):
        assert O.only_reads(status) is False
