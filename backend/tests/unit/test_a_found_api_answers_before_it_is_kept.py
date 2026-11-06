"""A found API is kept only when it answers (F1423; BLD1's clip 5 take4 on v2.0 2429a3a1; Naveen 11-06, in eb's tab
and CAP3's: probe it, keyed ones too, a 401 or 403 counting for an API that needs its key).

For "an agent I can ask about exchange rates any time, using the Frankfurter API" the research read frankfurter.dev's
docs page and reported its base URL as https://frankfurter.dev, with /v1/latest beside it. There that is a 404 web
page; the API is at https://api.frankfurter.dev. `check_api_spec` kept the spec, the host being one the research
read, and nothing asked whether it answered, so the tools would have been made on a page. Now one plain GET goes to
an endpoint that takes no arguments, with no key, through the egress guard, once a URL a build: data (2xx, not a web
page) keeps the spec, as does a 401 or 403 that is not a web page for an API that takes a key; anything else drops
it, and the plan asks the person for the API's address or its spec. A spec with no such endpoint, or a placeholder in
its host, is kept as before. Every other test's probe answers (conftest), so no test reaches the network.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from _fakes import seed as S
from core import builder_access, builder_research as br, builder_tools, egress_guard
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

DOCS = "https://frankfurter.dev/"
BLANK = {"in": "header", "name": "", "scheme": ""}
NOT_DATA = "404, a web page, not data"


def _take4(base="https://frankfurter.dev", auth=BLANK, endpoints=None) -> dict:
    return {"base_url": base, "auth": auth, "docs_url": DOCS, "endpoints": endpoints or [
        {"method": "GET", "path": "/v1/latest", "does": "Get latest exchange rates", "tool": "get_exchange_rates"},
        {"method": "GET", "path": "/v1/{date}", "does": "Historical rates for a date", "tool": "get_historical_rate"},
        {"method": "GET", "path": "/v1/currencies", "does": "List all supported currencies",
         "tool": "list_currencies"}]}


def _option(api) -> dict:
    return {"provider": "Frankfurter", "how": "api", "detail": "Free, no-key API serving ECB exchange rates.",
            "needs": "No account or API key required", "sources": [DOCS], "own_domains": ["frankfurter.dev"],
            "api": api}


class _Probe:
    """Answers by URL as the live sites did on 11-06 (eb's check): the docs host is a page, the API's is data."""

    def __init__(self, answers=None):
        self.calls: list[tuple[str, bool]] = []
        self.answers = answers or {"https://frankfurter.dev/v1/latest": NOT_DATA}

    async def __call__(self, url, *, keyed, own):
        self.calls.append((url, keyed))
        assert own == ["frankfurter.dev"], own
        return self.answers.get(url)


@pytest.fixture
def probe(monkeypatch):
    p = _Probe()
    monkeypatch.setattr(br, "_api_probe", p, raising=False)
    return p


@pytest.fixture(autouse=True)
async def workspace(monkeypatch):
    br._LOOSE.clear()
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["frankfurter.dev"]}
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


async def _report(api) -> dict:
    br._LOOSE[""] = {**br._fresh(), "read_hosts": ["frankfurter.dev"]}  # a report spends the research's record
    out = await br.report_findings({"goal": "exchange rates any time", "options": [_option(api)]}, None, [])
    [found] = out["options"]
    return found


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


async def _call(tool, args, run):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


def _run(**state) -> OrchestrationRun:
    return OrchestrationRun(run_id="builder_rates", orchestration_id="orch_native_builder",
                            shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create", **state})


class TestTake4:
    async def test_the_docs_host_is_not_kept(self, probe):
        found = await _report(_take4())
        assert probe.calls == [("https://frankfurter.dev/v1/latest", False)], "one call, to the endpoint with no args"
        assert found["how"] == "api" and "api" not in found, found
        assert found["detail"].startswith(f"{br.UNANSWERED}https://frankfurter.dev/v1/latest ({NOT_DATA}).")

    async def test_the_apis_own_host_is_kept(self, probe):
        found = await _report(_take4("https://api.frankfurter.dev"))
        assert probe.calls == [("https://api.frankfurter.dev/v1/latest", False)]
        assert found["api"]["base_url"] == "https://api.frankfurter.dev" and "did not answer" not in found["detail"]

    async def test_the_plan_asks_for_its_address_or_its_spec(self, probe):
        found = await _report(_take4())
        run = _run(**{build_plan.FINDINGS_KEY: {"goal": "exchange rates any time", "options": [found]}})
        await SharedState(run).checkpoint()
        draft = {"finish": "submit_plan", "kind": "create", "title": "Exchange rates", "summary": "Today's rates.",
                 "steps": [{"id": "rates", "type": "tool", "name": "Read today's rates", "does": "Reads the rates",
                            "tool": "frankfurter_get_exchange_rates"}]}
        run.shared_state[build_plan.DRAFT_KEY] = draft
        checked, _ = await _call("check_build", {"draft": draft}, run)
        assert "build" not in checked["offers"], checked
        assert ("I can't build `frankfurter_get_exchange_rates` yet: Frankfurter's API didn't answer at "
                f"https://frankfurter.dev/v1/latest ({NOT_DATA}). Give me its address or its OpenAPI spec's address, "
                "or say \"build without it\".") in checked["say"], checked["say"]
        assert "Add an MCP server" not in checked["say"]
        assert checked["no_tool"] == ["frankfurter_get_exchange_rates"]


class TestWhatIsAsked:
    async def test_once_a_url_a_build(self, probe):
        from core.orchestration.engine import ACTIVE_TIME_KEY

        run = _run()
        await SharedState(run).checkpoint()
        for _ in range(2):  # a report spends the research's record: each reads the docs page again
            run.step_progress.setdefault(ACTIVE_TIME_KEY, {})[br.RESEARCH_KEY] = {
                **br._fresh(), "read_hosts": ["frankfurter.dev"]}
            out, _ = await _call("report_findings", {"goal": "rates", "options": [_option(_take4())]}, run)
            assert "api" not in out["options"][0], out
        assert probe.calls == [("https://frankfurter.dev/v1/latest", False)]

    async def test_an_api_that_takes_a_key_is_asked_as_one(self, probe):
        await _report(_take4("https://api.frankfurter.dev", auth={"in": "header", "name": "X-Api-Key", "scheme": ""}))
        assert probe.calls == [("https://api.frankfurter.dev/v1/latest", True)]

    async def test_the_first_get_that_takes_no_arguments(self, probe):
        await _report(_take4("https://api.frankfurter.dev", endpoints=[
            {"method": "GET", "path": "/v1/{date}", "tool": "get_historical_rate"},
            {"method": "POST", "path": "/v1/alerts", "tool": "make_alert"},
            {"method": "GET", "path": "/v1/currencies", "tool": "list_currencies"}]))
        assert probe.calls == [("https://api.frankfurter.dev/v1/currencies", False)]

    async def test_with_none_to_ask_it_is_kept_as_before(self, probe):
        found = await _report(_take4("https://api.frankfurter.dev", endpoints=[
            {"method": "GET", "path": "/v1/{date}", "tool": "get_historical_rate"}]))
        assert probe.calls == [] and found["api"]["base_url"] == "https://api.frankfurter.dev"
        spec = {**_take4("https://{region}.frankfurter.dev"), "host_params": [{"name": "region", "says": "eu"}]}
        found = await _report(spec)
        assert probe.calls == [] and found["api"]["base_url"] == "https://{region}.frankfurter.dev"


# ── the probe itself, against a stand-in on this machine ─────────────────────

class _StandIn(BaseHTTPRequestHandler):
    seen: list[dict] = []
    ROUTES = {"/data": (200, "application/json", b'{"base": "EUR", "rates": {"USD": 1.08}}'),
              "/page": (200, "text/html; charset=utf-8", b"<!DOCTYPE html><html><body>Docs</body></html>"),
              "/missing": (404, "text/html", b"<!doctype html><html>Not found</html>"),
              "/sniffed": (200, "text/plain", b"  <html><body>a page</body></html>"),
              "/key": (401, "application/json", b'{"error": "missing api key"}'),
              "/key-page": (401, "text/html", b"<html>Sign in</html>"),
              "/broken": (500, "application/json", b'{"error": "down"}')}

    def do_GET(self):  # noqa: N802 — http.server's own name
        _StandIn.seen.append(dict(self.headers))
        if self.path == "/away":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/data")
            self.end_headers()
            return
        status, kind, body = self.ROUTES[self.path]
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def stand_in(monkeypatch):
    monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
    egress_guard._resolve_cache.clear()
    _StandIn.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StandIn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    egress_guard._resolve_cache.clear()


OWN = [br._registrable_domain("127.0.0.1")]


class TestTheProbe:
    @pytest.mark.parametrize("path, keyed, said", [
        ("/data", False, None),
        ("/page", False, "a web page, not data"),
        ("/missing", False, "404, a web page, not data"),
        ("/sniffed", False, "a web page, not data"),
        ("/key", True, None),
        ("/key", False, "401"),
        ("/key-page", True, "401, a web page, not data"),
        ("/broken", True, "500"),
    ])
    async def test_what_counts_as_an_answer(self, stand_in, path, keyed, said):
        assert await br.answers(stand_in + path, keyed=keyed, own=OWN) == said

    async def test_it_sends_no_key(self, stand_in):
        await br.answers(stand_in + "/key", keyed=True, own=OWN)
        [headers] = _StandIn.seen
        assert not {h.lower() for h in headers} & {"authorization", "x-api-key", "cookie", "api-key"}, headers

    async def test_an_answer_from_another_site_is_not_its_own(self, stand_in, monkeypatch):
        monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1,localhost")
        said = await br.answers(stand_in + "/away", keyed=False, own=OWN)
        assert said == "it answered from localhost, not its own site", said

    async def test_the_egress_guard_decides_where_it_may_go(self, stand_in, monkeypatch):
        monkeypatch.delenv("EGRESS_ALLOW_HOSTS")
        egress_guard._resolve_cache.clear()
        said = await br.answers(stand_in + "/data", keyed=False, own=OWN)
        assert said.startswith("Synapse may not reach it") and _StandIn.seen == [], said

    async def test_no_answer_in_time(self, monkeypatch):
        monkeypatch.setattr(br, "PROBE_SECONDS", 0.3)
        monkeypatch.setenv("EGRESS_ALLOW_HOSTS", "127.0.0.1")
        async def hold(_reader, writer):  # accepts, never answers, and keeps the connection open
            await asyncio.sleep(5)
            writer.close()

        quiet = await asyncio.start_server(hold, "127.0.0.1", 0)
        port = quiet.sockets[0].getsockname()[1]
        try:
            said = await br.answers(f"http://127.0.0.1:{port}/data", keyed=False, own=OWN)
        finally:
            quiet.close()
        assert said == "no answer within 0.3 s", said
