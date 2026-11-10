"""The planner's pack says what code knows of each service the person named, and whose its `tools` are (F1495, A1 and
D; TRI2's class 2, pro drafts/tri2/TRIAGE.md; Naveen's pick 11-10, pro drafts/bld2/PLAN.md).

TRI2's prod run (run_01a1bf14…, 11-09), on Sonnet 4.6: "Make a workflow that connects to GitHub and triages our open
pull requests…". Classify named `providers ["GitHub"]`, and nothing in front of the planner said what that meant here,
so its first pass spent 11 of 14 turns looking for a GitHub tool: `list_tool_servers` first (a workflow tool in the
pack's `tools`, refused: "Blocked: Tool not available for this agent"), `list_custom_tools`, then `read_workspace` nine
times, against planner.json's own rule that such a tool goes in the plan and code finds its provider. At about 36k
tokens a turn, that emptied a Free wallet before any plan was shown. BLD2's OSS live check (11-10) saw the same
opening on Haiku 4.5: the refused `list_tool_servers`, then `list_custom_tools`.

Now (A1) the pack carries `named_services`: for each service the request names, from code, with no model and no web,
whether it is connected here (a tool server by its name, its tools named), an app in the Apps catalogue (by its whole
name, as `catalog_entry` reads it: "Google" is never Google Calendar) or neither, each with the rule: name its tools
for what they do, connected after the yes or looked up after the plan. Nothing to look for. And (D) the pack says that
its `tools` are the workflows': the planner calls none of them.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_connect, safe_fetch
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState

GITHUB_APP = {"slug": "github", "name": "GitHub", "auth": "bearer", "url": "https://api.githubcopilot.com/mcp/"}
CALENDAR_APP = {"slug": "google_calendar", "name": "Google Calendar", "auth": "oauth",
                "url": "https://calendar.google.com"}
MESSAGE = ("Make a workflow that connects to GitHub and triages our open pull requests each morning: it labels each by "
           "size and risk and lists the ones waiting longest for review.")


class _Server:
    agent_sessions: dict = {}

    def __init__(self, router: dict | None = None):
        self.tool_router = router or {}


@pytest.fixture(autouse=True)
def no_catalogue_after():
    yield
    builder_connect.set_connect_options(None)


@pytest.fixture
def no_web(monkeypatch):
    """The pack's named services are code's: no fetch, and never the web look-up."""
    async def never(*_a, **_k):
        raise AssertionError("the web was read")

    monkeypatch.setattr(safe_fetch, "fetch", never)
    monkeypatch.setattr(safe_fetch, "fetch_bytes", never)
    from core.native_builder import lookup

    monkeypatch.setattr(lookup, "look_up_providers", never)


def _catalogue(*apps: dict) -> None:
    async def options():
        return {"catalog": list(apps), "providers": {}, "screens": {"apps": "Apps"}}

    builder_connect.set_connect_options(options)


async def _pack(providers: list[str] | None, server: _Server | None = None, message: str = MESSAGE) -> dict:
    state = {builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", "user_message": message}
    if providers is not None:
        state["request"] = {"kind": "build", "effort": "heavy", "providers": providers}
    run = OrchestrationRun(run_id="builder_f1495_pack", orchestration_id="orch_native_builder", shared_state=state)
    await SharedState(run).checkpoint()
    with live(run):
        raw, _ = await execute_builder_tool_with_events("read_plan_context", {}, server or _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


RULE = "name its tools in the plan for what they do, with GitHub in the step's words"


class TestNamedServices:
    async def test_an_app_the_catalogue_holds_connects_after_the_yes(self, no_web):
        """The prod shape on cloud: GitHub in the Apps catalogue, not connected."""
        _catalogue(GITHUB_APP, CALENDAR_APP)
        said = (await _pack(["GitHub"]))["named_services"]["GitHub"]
        assert said.startswith("an app in the Apps catalog, not connected yet"), said
        assert RULE in said and "the person connects it on its card after the yes" in said, said
        assert "nothing to look for here" in said.lower(), said

    async def test_with_no_catalogue_code_looks_it_up_after_the_plan(self, no_web):
        """Self-hosted has no Apps catalogue."""
        said = (await _pack(["GitHub"]))["named_services"]["GitHub"]
        assert said.startswith("nothing here provides it yet"), said
        assert RULE in said and "code looks up who provides it after the plan" in said, said
        assert "nothing to look for here" in said.lower(), said

    async def test_a_connected_service_names_its_tools(self, no_web):
        _catalogue(GITHUB_APP)
        server = _Server({"github__list_pull_requests": object(), "github__add_labels": object()})
        said = (await _pack(["GitHub"], server))["named_services"]["GitHub"]
        assert said.startswith("connected here"), said
        assert "github__list_pull_requests" in said and "github__add_labels" in said, said
        assert "after the yes" not in said and "looks up" not in said, said

    async def test_a_shared_word_is_never_the_app(self, no_web):
        """"Google" is not Google Calendar: the app is the provider's by its whole name, as the look-up's verdict is."""
        _catalogue(GITHUB_APP, CALENDAR_APP)
        said = (await _pack(["Google"], message="Search Google for our brand each morning"))["named_services"]["Google"]
        assert said.startswith("nothing here provides it yet") and "Calendar" not in said, said

    async def test_each_named_service_has_its_line(self, no_web):
        _catalogue(GITHUB_APP)
        services = (await _pack(["GitHub", "Acme Payroll"]))["named_services"]
        assert list(services) == ["GitHub", "Acme Payroll"]
        assert services["Acme Payroll"].startswith("nothing here provides it yet"), services

    @pytest.mark.parametrize("providers", [None, []])
    async def test_a_request_that_names_none_has_no_entry(self, providers, no_web):
        assert "named_services" not in await _pack(providers, message="Summarise my notes each morning")


class TestThePacksTools:
    async def test_the_pack_says_its_tools_are_the_workflows(self, no_web):
        pack = await _pack(["GitHub"])
        keys = list(pack)
        assert "tools_are" in pack and keys.index("tools_are") == keys.index("tools") - 1, keys
        assert "you call none of them" in pack["tools_are"].lower(), pack["tools_are"]
