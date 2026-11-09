"""Apps first: a service the Apps catalogue holds is connected with its app's card, whatever the research made of its
API (F1486; TRI2, pro drafts/tri2/TRIAGE.md, "Second run").

Naveen (11-09): "priority is apps.. first we check whether we can just connect the app with the cards then only we
check for api spec". TRI2's prod run (run_01a1bfda…, 11-09): "Make a workflow that connects to GitHub and triages our
open pull requests…". The quick classify named no providers that time, so GitHub was only a word in a tool's name and
went to the research, which reported GitHub's REST API; its one endpoint carried a query (`/pulls?state=open`), so the
spec was dropped and the tool was left a lead (`found`, `via none`). The card said "GitHub has an API, but I can't make
a tool for it yet. Add an MCP server for it…", offered no Build it, and never the GitHub app in the catalogue, whose
Connect card takes a personal access token.

Now a tool the research put on a provider the catalogue holds by its whole name (`catalog_entry`) is that app's: the
review says it will ask for it after the yes, and the yes puts up the app's Connect card, as for any app. A provider
the catalogue does not hold keeps what the research found, and a shared word is never the app ("Google" is not
Google Calendar). Self-hosted has no Apps catalogue, so nothing changes there.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_connect
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

GITHUB_APP = {"slug": "github", "name": "GitHub", "auth": "bearer", "url": "https://api.githubcopilot.com/mcp/",
              "token_hint": "A GitHub personal access token with access to the repositories you want reachable."}
CALENDAR_APP = {"slug": "google_calendar", "name": "Google Calendar", "auth": "oauth", "url": "https://calendar.google.com"}

#: The prod run's finding: an API whose spec was not used, so a lead (`builder_research.report_findings`).
LEAD = {"provider": "GitHub", "how": "api",
        "detail": "GitHub's REST API lists open pull requests with a personal access token (Its API spec was not used: "
                  "an endpoint (GET /repos/{owner}/{repo}/pulls?state=open) is not a method and a path under its "
                  "base.)",
        "needs": "A GitHub personal access token", "sources": ["https://docs.github.com/en/rest/pulls/pulls"],
        "own_domains": ["github.com"]}
#: The same API with a spec code could make the tool from.
WORKING = {**LEAD, "detail": "GitHub's REST API lists open pull requests", "api": {
    "base_url": "https://api.github.com", "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
    "docs_url": "https://docs.github.com/en/rest/pulls/pulls",
    "endpoints": [{"method": "GET", "path": "/repos/{owner}/{repo}/pulls", "does": "Lists open pull requests",
                   "tool": "github_list_open_prs"}]}}
#: The research found no way in.
NO_WAY = {"provider": "GitHub", "how": "none", "detail": "No public way in that I could find",
          "sources": ["https://github.com"], "own_domains": ["github.com"]}

ASK = {"id": "ask", "type": "human", "name": "Approve the triage", "does": "The person reads it", "prompt": "Post it?"}
LIST = {"id": "list", "type": "tool", "name": "Fetch open PRs", "does": "Lists the open pull requests on GitHub",
        "tool": "github_list_open_prs"}
APP_LINE = "`github_list_open_prs`: after your yes, I'll ask you to connect GitHub (in the Apps catalog)."


class _Server:
    agent_sessions: dict = {}

    def __init__(self):
        self.tool_router = {}


SERVER = _Server()


@pytest.fixture(autouse=True)
def workspace():
    confirm.set_claim_store(None)
    builder_access.forget_runs()

    async def owner(user_id):
        return Role.OWNER

    async def options():
        return {"catalog": [GITHUB_APP, CALENDAR_APP], "providers": {}, "screens": {"apps": "Apps"}}

    hooks.set_role_source(owner)
    builder_connect.set_connect_options(options)
    yield
    hooks.set_role_source(None)
    builder_connect.set_connect_options(None)
    builder_access.forget_runs()


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, SERVER, run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


async def _reviewed(option, run_id, steps=(ASK, LIST)):
    run = OrchestrationRun(run_id=run_id, orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         build_plan.FINDINGS_KEY: {"goal": "x", "options": [option]}})
    await SharedState(run).checkpoint()
    draft = {"finish": "submit_plan", "kind": "create", "title": "Daily PR Triage", "summary": "Triages open PRs.",
             "steps": list(steps)}
    run.shared_state[build_plan.DRAFT_KEY] = draft
    checked, events = await _call("check_build", {"draft": draft}, run)
    run.shared_state[build_plan.CHECKED_KEY] = checked
    return run, checked, events


def _status(checked, name="github_list_open_prs") -> dict:
    return next(t for t in checked["plan"]["tools"] if t["name"] == name)


async def _cards_after_the_yes(run):
    run.shared_state["review_feedback"] = {"feedback": "Build it"}
    approved, _ = await _call("approve_plan", {}, run)
    run.shared_state["plan_approval"] = approved
    made, events = await _call("save_build", {"only": "tools"}, run)
    return made, [e for e in events if e.get("type") == "connection_needed"]


@pytest.mark.parametrize("option", [LEAD, WORKING, NO_WAY], ids=["lead", "working_api", "no_way"])
async def test_a_service_the_catalogue_holds_is_planned_on_its_app(option):
    _, checked, events = await _reviewed(option, f"builder_apps_first_{option['how']}_{'api' in option}")
    status = _status(checked)
    assert (status["status"], status["via"], status["provider"]) == ("found", "catalog_app", "GitHub")
    assert checked["plan"]["connections_needed"] == ["GitHub"]
    assert checked["outcome"] == "review" and "build" in checked["offers"]
    assert checked["after_yes"] == [APP_LINE]
    assert "I can't build" not in checked["say"] and "I can't build" not in checked["review"]
    assert not [e for e in events if e.get("type") == "connection_needed"], "the plan first, the card after the yes"


async def test_the_yes_puts_up_the_app_s_card_which_takes_its_token():
    """What Naveen expected: GitHub's app takes a personal access token, so its card asks for one."""
    run, _, _ = await _reviewed(LEAD, "builder_apps_first_card")
    made, cards = await _cards_after_the_yes(run)
    assert made["tools"] == [] and made["waits"] == ["GitHub"], "no API tool is made beside the app"
    [card] = cards
    assert card["kind"] == "app" and card["app"]["slug"] == "github" and card["app"]["auth"] == "bearer"
    assert card["app"]["token_hint"] == GITHUB_APP["token_hint"]


async def test_a_service_the_catalogue_does_not_hold_keeps_what_the_research_found():
    lead = {**LEAD, "provider": "Acme Payroll", "own_domains": ["acmepayroll.com"]}
    pay = {"id": "pay", "type": "tool", "name": "Read payroll", "does": "Reads this month's Acme Payroll run",
           "tool": "acme_payroll_read_run"}
    _, checked, _ = await _reviewed(lead, "builder_apps_first_no_app", steps=(ASK, pay))
    status = _status(checked, "acme_payroll_read_run")
    assert (status["status"], status["via"]) == ("found", "none")
    assert "build" not in checked["offers"]
    assert "Acme Payroll has an API, but I can't make a tool for it yet" in checked["say"]


async def test_a_shared_word_is_never_the_app():
    """"Google" is not Google Calendar: the app is the provider's by its whole name, as the look-up's verdict is."""
    lead = {**LEAD, "provider": "Google", "own_domains": ["google.com"]}
    find = {"id": "find", "type": "tool", "name": "Search", "does": "Searches Google for the term",
            "tool": "google_search"}
    _, checked, _ = await _reviewed(lead, "builder_apps_first_word", steps=(ASK, find))
    assert _status(checked, "google_search")["via"] == "none"
    assert checked["after_yes"] == [] or all("Google Calendar" not in line for line in checked["after_yes"])
