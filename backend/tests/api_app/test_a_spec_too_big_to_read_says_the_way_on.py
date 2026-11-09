"""A spec too big to read says what to do instead: the provider's app when the Apps catalogue holds it, else only the
calls the plan needs (F1496; TRI2's class 3, pro drafts/tri2/TRIAGE.md).

TRI2's prod run (run_01a1bf14…, 11-09): "Make a workflow that connects to GitHub and triages our open pull
requests…". The planner put GitHub's public spec in the plan's `apis`, and `submit_plan` said only "apis 'GitHub REST
API': its spec could not be read: the file is larger than the 1.90735 MB limit". Nothing said what to do, so the
planner sent the same entry again and got the same refusal, two of the turns that emptied a Free wallet before any
plan was shown. A spec in the Vault over the cap was worse: it said "Use read_file with a line range", a tool the
planner does not have.

Now a spec too big to read is its own case. The card's line says the fact. The planner's refusal, from `submit_plan`,
from `read_api_spec` and on a named site's spec, says the way on:
* the Apps catalogue holds the API's provider (its name without "REST API" and the like, by whole name as
  `catalog_entry` reads it: "Google" is never Google Calendar): leave it out of `apis`, name the tools for what they
  do, and the person connects that app on its card after the yes (F1486, apps first);
* else: leave it out of `apis` and name only the tools the plan needs; the lookup finds each one's endpoint, and only
  those are made.
A spec unread for any other reason keeps its own words.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_connect, builder_tools  # noqa: F401 — the builder's modules, as at boot
from core import safe_fetch
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store

GITHUB_APP = {"slug": "github", "name": "GitHub", "auth": "bearer", "url": "https://api.githubcopilot.com/mcp/"}
CALENDAR_APP = {"slug": "google_calendar", "name": "Google Calendar", "auth": "oauth",
                "url": "https://calendar.google.com"}
GITHUB_SPEC = ("https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/"
               "api.github.com.json")
#: What the fetch raised in prod: the body passed `vault_tools._MAX_BYTES` (2,000,000), said in MiB.
TOO_LARGE = "the file is larger than the 1.90735 MB limit"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_spec_too_big", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create",
                                         "user_message": "Triage our open pull requests each morning."})
    await SharedState(run).checkpoint()
    yield run
    builder_connect.set_connect_options(None)
    builder_access.forget_runs()


def _catalogue(*apps: dict) -> None:
    async def options():
        return {"catalog": list(apps), "providers": {}, "screens": {"apps": "Apps"}}

    builder_connect.set_connect_options(options)


def _fetch_raises(monkeypatch, exc: Exception) -> list[str]:
    asked: list[str] = []

    async def fetch(url, *, max_bytes=None):
        asked.append(url)
        raise exc

    monkeypatch.setattr(safe_fetch, "fetch_bytes", fetch)
    return asked


def _plan(name: str, spec: str, tool: str = "list_open_pull_requests") -> dict:
    return {"kind": "create", "title": "Daily PR Triage", "summary": "Triages our open pull requests.",
            "steps": [{"id": "list", "type": "tool", "name": "Fetch open PRs", "does": "Lists the open pull requests",
                       "tool": tool, "next": "end"}],
            "apis": [{"name": name, "spec": spec}]}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


async def _call(run, tool: str, args: dict):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _the_error(found, name: str) -> str:
    [error] = [e for e in found.errors if e.startswith(f"apis '{name}'")]
    return error


APP_WAY = ("GitHub is in the Apps catalog: leave 'GitHub REST API' out of `apis` and name each tool the plan needs "
           "for what it does, with GitHub in the step's words: the person connects the GitHub app on its card after "
           "the yes")
CALLS_WAY = ("leave 'Orders API' out of `apis` and name only the tools the plan needs, each for what it does, with "
             "Orders API in the step's words: the lookup finds each one's endpoint, and only those are made")


class TestSubmitPlan:
    async def test_the_prod_shape_names_the_app_the_catalogue_holds(self, client, run, monkeypatch):
        """TRI2's run: GitHub's public spec in `apis`, on a host whose catalogue holds the GitHub app."""
        _catalogue(GITHUB_APP, CALENDAR_APP)
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        found = await _examine(run, _plan("GitHub REST API", GITHUB_SPEC))
        error = _the_error(found, "GitHub REST API")
        assert error.startswith("apis 'GitHub REST API': its spec is larger than the 2 MB a build reads"), error
        assert APP_WAY in error, error
        assert "the lookup finds" not in error, "the app is the way on, not the API's calls"

    async def test_with_no_app_it_takes_only_the_calls_the_plan_needs(self, client, run, monkeypatch):
        """Self-hosted has no Apps catalogue; a cloud one may not hold the provider either."""
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        found = await _examine(run, _plan("Orders API", "https://orders.example/openapi.json"))
        error = _the_error(found, "Orders API")
        assert error.startswith("apis 'Orders API': its spec is larger than the 2 MB a build reads"), error
        assert CALLS_WAY in error, error

    async def test_a_shared_word_is_never_the_app(self, client, run, monkeypatch):
        """"Google" is not Google Calendar: the app is the provider's by its whole name, as the look-up's verdict is."""
        _catalogue(GITHUB_APP, CALENDAR_APP)
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        found = await _examine(run, _plan("Google Maps API", "https://maps.example/openapi.json"))
        error = _the_error(found, "Google Maps API")
        assert "Apps catalog" not in error and "Google Calendar" not in error, error
        assert "the lookup finds each one's endpoint" in error, error

    async def test_a_spec_in_the_vault_over_the_cap_says_the_way_on_not_read_file(self, client, run):
        """`read_file` is the Vault reader's way on for a big file, a tool the planner does not have."""
        get_blob_store().put("vault/specs/orders.json", "x" * 2_000_100)
        found = await _examine(run, _plan("Orders API", "specs/orders.json"))
        error = _the_error(found, "Orders API")
        assert "read_file" not in error, error
        assert "its spec is larger than the 2 MB a build reads" in error and CALLS_WAY in error, error

    async def test_the_card_says_the_fact_in_the_person_s_words(self, client, run, monkeypatch):
        """The part's `problem` is said on the card: the fact, never the planner's `apis` or what it should name."""
        _catalogue(GITHUB_APP)
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        found = await _examine(run, _plan("GitHub REST API", GITHUB_SPEC))
        [part] = found.plan.apis
        assert part.problem.startswith("its spec is larger than the 2 MB a build reads"), part.problem
        assert "`apis`" not in part.problem and "step's words" not in part.problem, part.problem

    async def test_a_spec_unread_for_another_reason_keeps_its_own_words(self, client, run, monkeypatch):
        _catalogue(GITHUB_APP)
        _fetch_raises(monkeypatch, safe_fetch.FetchRefused(f"the server answered 404 for {GITHUB_SPEC}"))
        found = await _examine(run, _plan("GitHub REST API", GITHUB_SPEC))
        error = _the_error(found, "GitHub REST API")
        assert error == f"apis 'GitHub REST API': its spec could not be read: the server answered 404 for {GITHUB_SPEC}"


class TestThePlannersRead:
    async def test_read_api_spec_says_the_way_on(self, client, run, monkeypatch):
        _catalogue(GITHUB_APP)
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        out = await _call(run, "read_api_spec", {"spec": GITHUB_SPEC, "name": "GitHub REST API"})
        assert out["error"].startswith("Its spec is larger than the 2 MB a build reads"), out
        assert APP_WAY in out["error"], out


class TestANamedSitesSpec:
    async def test_a_named_site_s_spec_too_big_says_the_way_on(self, client, run, monkeypatch):
        """A spec the person's named site links (F1317) is read as theirs; too big, the pack says what to do."""
        _fetch_raises(monkeypatch, safe_fetch.FetchTooLarge(TOO_LARGE))
        page = {"url": "https://orders.example", "api_spec": "https://orders.example/openapi.json",
                "api_title": "Orders API"}
        await build_plan._spec_of(page, run.shared_state)
        assert "api" not in page
        assert "its spec is larger than the 2 MB a build reads" in page["why_not"], page
        assert CALLS_WAY in page["why_not"], page
