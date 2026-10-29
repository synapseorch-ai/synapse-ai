"""F1225 (BLD1; CAP3 stage 1, P2): a plan uses the person's own API, from its spec, and their own MCP server, by URL.

"Here is our orders API's spec" and "use our tracker's MCP server at …" got a plan that made neither: the planner
wrote tool names nothing here had, and the lookup went looking for a provider. Now a plan's `apis` names the spec
(its URL or its Vault path): `read_api_spec` gives the planner the tools its operations become, and each one the plan
uses is made after the yes as a read API's is (F944), with every argument the operation has, its base locked, and its
key on a Connect card (none for an API that takes none). A spec URL is fetched as every model-written URL is, under the
Vault reader's cap, and held to the host's rule. `mcp_servers` names a server by its URL, held to F1201's rule: while
it is not connected its Connect card stands where the plan would, and nothing is offered to build (Naveen, 10-29).
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core import mcp_client
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, own_api
from core.native_builder.build_save import _connect_cards, save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store

SPEC = {
    "openapi": "3.0.0", "info": {"title": "Orders", "version": "1"},
    "servers": [{"url": "https://api.orders.example/v1"}],
    "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
    "paths": {
        "/orders/{order_id}": {"get": {"operationId": "getOrder", "summary": "Get an order", "parameters": [
            {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "expand", "in": "query", "schema": {"type": "string"}}]}},
        "/orders": {"post": {"operationId": "createOrder", "summary": "Create an order", "requestBody": {
            "content": {"application/json": {"schema": {"type": "object", "properties": {"sku": {"type": "string"}}}}}}}},
    },
}
GET_ORDER = "orders_api_getorder"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_own_api", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create",
                                         "user_message": "Look up an order in our orders API."})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


def _vault(spec: dict | str, path: str = "specs/orders.json") -> str:
    get_blob_store().put(f"vault/{path}", spec if isinstance(spec, str) else json.dumps(spec))
    return path


async def _call(run, tool: str, args: dict, server=None):
    with live(run):
        raw, events = await execute_builder_tool_with_events(tool, args, server or _Server(), run_id=run.run_id)
    return (json.loads(raw) if isinstance(raw, str) else raw), events


# ── the planner's read ────────────────────────────────────────────────────────────────────────────────────────────

class TestReadApiSpec:
    async def test_a_vault_spec_gives_its_tools_by_name(self, client, run):
        out, _ = await _call(run, "read_api_spec", {"spec": _vault(SPEC), "name": "Orders API"})
        assert [t["tool"] for t in out["tools"]] == [GET_ORDER, "orders_api_createorder"]
        assert out["tools"][0]["call"] == "GET /orders/{order_id}"
        assert "X-Api-Key header" in out["key"]
        assert builder_access.KINDS["read_api_spec"] == "read"

    async def test_a_spec_url_is_fetched_under_the_vault_reader_s_cap(self, client, run, monkeypatch):
        from core import safe_fetch

        asked = {}

        async def fetch(url, *, max_bytes=None):
            asked.update(url=url, max_bytes=max_bytes)
            return json.dumps(SPEC).encode()

        monkeypatch.setattr(safe_fetch, "fetch_bytes", fetch)
        out, _ = await _call(run, "read_api_spec", {"spec": "https://orders.example/openapi.json",
                                                    "name": "Orders API"})
        assert asked == {"url": "https://orders.example/openapi.json", "max_bytes": 2_000_000}
        assert len(out["tools"]) == 2

    async def test_the_host_s_rule_refuses_a_spec_url_before_any_fetch(self, client, run, monkeypatch):
        from core import safe_fetch

        async def never(url, **_):
            raise AssertionError("fetched")

        async def rule(url):
            return "That host is inside this network."

        monkeypatch.setattr(safe_fetch, "fetch_bytes", never)
        mcp_client.set_url_rule(rule)
        try:
            out, _ = await _call(run, "read_api_spec", {"spec": "https://10.0.0.5/openapi.json", "name": "Orders"})
        finally:
            mcp_client.set_url_rule(None)
        assert out["error"] == "That host is inside this network."

    @pytest.mark.parametrize("spec, said", [
        ("just some notes", "could not be read as OpenAPI"),
        ({**SPEC, "components": {"securitySchemes": {"o": {"type": "oauth2", "flows": {}}}}}, "signs in with OAuth"),
        ({**SPEC, "components": {"securitySchemes": {"q": {"type": "apiKey", "in": "query", "name": "k"}}}},
         "only in a header"),
        ({**SPEC, "servers": []}, "names no server URL"),
    ])
    async def test_what_cannot_be_used_is_said(self, client, run, spec, said):
        out, _ = await _call(run, "read_api_spec", {"spec": _vault(spec), "name": "Orders API"})
        assert said in out["error"]


# ── the plan's API ────────────────────────────────────────────────────────────────────────────────────────────────

def _plan(tool: str = GET_ORDER, **more) -> dict:
    return {"kind": "create", "title": "Look up an order", "summary": "Looks up an order in our orders API.",
            "steps": [{"id": "look", "type": "tool", "name": "Look up the order", "does": "Reads the order",
                       "tool": tool, "next": "end"}], **more}


async def _examine(run, draft, server=None):
    with live(run):
        return await build_plan.examine(draft, server or _Server(), state=run.shared_state)


class TestThePlansApi:
    async def test_its_tool_is_made_with_every_argument_on_the_spec_s_own_server(self, client, run):
        found = await _examine(run, _plan(apis=[{"name": "Orders API", "spec": _vault(SPEC)}]))
        assert found.errors == [], found.errors
        [part] = found.plan.apis
        assert part.tools == [GET_ORDER] and part.problem == ""
        [status] = found.plan.tools
        assert (status.status, status.via, status.provider) == ("found", "http_tool", "Orders API")
        [call] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_http_tool"]
        args = call["args"]
        assert args["url"] == "https://api.orders.example/v1/orders/{{order_id}}"
        assert args["base_lock"] == "https://api.orders.example/v1" and args["read_only"] is True
        assert set(json.loads(args["input_schema_json"])["properties"]) == {"order_id", "expand"}
        assert "“Orders API”, from the spec at `specs/orders.json`: made with it as `orders_api_getorder`" \
            in render(found.plan)

    async def test_its_key_is_asked_on_a_card_and_an_api_with_none_is_not(self, client, run):
        from core.store.resources import save_tools

        found = await _examine(run, _plan(apis=[{"name": "Orders API", "spec": _vault(SPEC)}]))
        # The card names only a tool the build made already (`step_make_tools`), as it is after the yes.
        await save_tools([{"name": GET_ORDER, "tool_type": "http", "method": "GET",
                           "url": "https://api.orders.example/v1/orders/{{order_id}}",
                           "base_lock": "https://api.orders.example/v1"}])
        events: list[dict] = []
        waits, _ = await _connect_cards(found.plan, [GET_ORDER], events)
        assert waits == ["Orders API"] and events[0]["tool"]["header"] == "X-Api-Key"
        open_spec = {k: v for k, v in SPEC.items() if k != "components"}
        found = await _examine(run, _plan(apis=[{"name": "Orders API", "spec": _vault(open_spec, "specs/open.json")}]))
        events = []
        assert await _connect_cards(found.plan, [GET_ORDER], events) == ([], "") and events == []

    async def test_a_name_its_spec_does_not_have_goes_back_with_the_names_it_has(self, client, run):
        found = await _examine(run, _plan("orders_api_list_orders",
                                          apis=[{"name": "Orders API", "spec": _vault(SPEC)}]))
        assert any("its spec has no operation orders_api_list_orders; its tools are: orders_api_getorder, "
                   "orders_api_createorder" in e for e in found.errors)

    async def test_a_spec_it_cannot_read_goes_back_to_the_planner(self, client, run):
        found = await _examine(run, _plan(apis=[{"name": "Orders API", "spec": "specs/gone.json"}]))
        assert any(e.startswith("apis 'Orders API': Not found: specs/gone.json") for e in found.errors)

    async def test_a_model_never_writes_what_code_fills(self, client, run):
        found = await _examine(run, _plan(apis=[{"name": "Orders API", "spec": _vault(SPEC),
                                                 "tools": ["anything"], "problem": "x"}]))
        assert found.plan.apis[0].tools == [GET_ORDER] and found.plan.apis[0].problem == ""


# ── the plan's MCP server ─────────────────────────────────────────────────────────────────────────────────────────

SERVER_PLAN = {"kind": "create", "title": "Triage tickets", "summary": "Triages new tickets in our tracker.",
               "steps": [{"id": "triage", "type": "agent", "name": "Triage", "does": "Triages the tickets",
                          "agent": "Triage", "prompt": "Triage {state.input}.", "next": "end"}],
               "agents": [{"name": "Triage", "purpose": "Triages tickets", "servers": ["Tracker"]}],
               "mcp_servers": [{"name": "Tracker", "url": "https://tracker.example/mcp", "connected": True}]}


class TestThePlansServer:
    async def test_not_connected_its_card_stands_where_the_plan_would(self, client, run):
        # The person gave the server by its URL (F1225): an address a plan writes is one they gave (F1253).
        run.shared_state["user_message"] = "Triage new tickets in our tracker: its MCP server is https://tracker.example/mcp"
        run.shared_state[build_plan.DRAFT_KEY] = {"finish": "submit_plan", **SERVER_PLAN}
        out, events = await _call(run, "check_build", {"draft": {"finish": "submit_plan", **SERVER_PLAN}})
        assert out["outcome"] == "review" and "build" not in out["offers"]
        assert out["lead"].startswith("Before I plan this, connect the MCP server you gave")
        assert out["review"].startswith("- MCP server “Tracker” at https://tracker.example/mcp is not connected yet")
        [card] = [e for e in events if e.get("type") == "connection_needed"]
        assert card["kind"] == "mcp_server" and card["server"]["url"] == "https://tracker.example/mcp"

    async def test_connected_by_its_url_it_is_the_agent_s_server(self, client, run, monkeypatch):
        from core import builder_guard
        from core.store.resources import save_mcp_server

        async def as_run_calls(names, server_module):
            return [n.partition("__")[2] or n for n in names]

        monkeypatch.setattr(builder_guard, "canonical_names", as_run_calls)
        await save_mcp_server({"name": "tracker", "label": "Tracker", "url": "https://tracker.example/mcp/"})

        class Connected(_Server):
            tool_router = {"tracker__list_tickets": object()}

        found = await _examine(run, SERVER_PLAN, Connected())
        assert found.errors == [], found.errors
        [part] = found.plan.mcp_servers
        assert part.connected is True and part.name == "tracker"
        assert found.plan.agents[0].servers == ["tracker"] and "list_tickets" in found.plan.agents[0].tools
        assert "MCP server “tracker” at https://tracker.example/mcp: connected" in render(found.plan)

    async def test_a_server_on_this_machine_goes_back(self, client, run):
        found = await _examine(run, {**SERVER_PLAN, "mcp_servers": [{"name": "Tracker",
                                                                     "url": "http://127.0.0.1:9000/mcp"}]})
        assert any(e.startswith("mcp_servers 'Tracker':") and "added on the Apps screen, by you" in e
                   for e in found.errors)
