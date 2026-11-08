"""The person's own API spec, read once in a conversation, is what its provider's tools are made from (F1298, prod-13;
79 11-02).

prod-13's rerun (a0da2070): the person gave Ledgerline's spec, and the planner read it (`read_api_spec`) in the
conversation's first run; the plan, drawn in a later run, made Ledgerline's tools from the web lookup's guess instead
(api.ledgerlineapi.com, `apis` []), so even a saved build would call the wrong host. A read spec is now kept for the
conversation (its name and where it is, never a key: a URL with a query or a user part is not kept) and carried to the
next run as the sites it read are (`named_sites.PAGES_KEY`). In examine, a provider the lookup found whose spec was
read is that spec's API part (F1225): each of its endpoints is the spec's operation of the same method and path, by
the spec's tool name, on the spec's own server; one the spec has not goes back to the planner with the spec's tools.
"""
from __future__ import annotations

import inspect
import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, own_api
from core.native_builder.build_save import save_calls
from core.native_builder.seed import bundled_definitions
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store

#: The run's key for the specs the conversation read (F1298): the engine's own, never in a public read.
SPECS = "_specs_read"


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


SPEC = {
    "openapi": "3.0.0", "info": {"title": "Ledgerline", "version": "1"},
    "servers": [{"url": "https://billing.ledgerline.example/api"}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {"/invoices": {"post": {"operationId": "createInvoice", "summary": "Create a draft invoice",
                                     "requestBody": {"content": {"application/json": {"schema": {
                                         "type": "object", "properties": {"customer": {"type": "string"},
                                                                          "amount": {"type": "number"}}}}}}}}},
}
CREATE = "ledgerline_createinvoice"
# What the web lookup found for Ledgerline on prod-13's rerun: a guessed host, and endpoints by its own names.
LOOKUP = {"provider": "Ledgerline", "how": "api", "sources": ["https://ledgerlineapi.com/developers"],
          "detail": "Create invoices in Ledgerline via REST API.",
          "api": {"base_url": "https://api.ledgerlineapi.com/api/v1",
                  "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
                  "docs_url": "https://ledgerlineapi.com/developers/recipes",
                  "endpoints": [{"method": "POST", "path": "/invoices", "does": "Create a new draft invoice",
                                 "tool": "ledgerline_create_invoice"},
                                {"method": "POST", "path": "/invoices/{id}/post", "does": "Post the invoice",
                                 "tool": "ledgerline_post_invoice"}]}}
FOUND = {"goal": "x", "options": [LOOKUP]}


def _vault(spec: dict, path: str = "specs/ledgerline.json") -> str:
    get_blob_store().put(f"vault/{path}", json.dumps(spec))
    return path


@pytest.fixture
async def run(client):
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_f1298", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create",
                                         "user_message": "Make an invoice in Ledgerline for each order.",
                                         build_plan.FINDINGS_KEY: FOUND})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


async def _call(run, tool: str, args: dict):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


def _plan(*tools: str, **more) -> dict:
    # A person approves first: each call here acts (a POST), and decision 4 holds one nobody approves (BWA1).
    ask = {"id": "approve", "type": "human", "name": "Approve the invoices", "does": "The person reads the orders",
           "prompt": "Make these invoices?", "next": "s0"}
    steps = [{"id": f"s{i}", "type": "tool", "name": f"Call {t}", "does": "Works on the invoice", "tool": t,
              "next": f"s{i + 1}" if i + 1 < len(tools) else "end"} for i, t in enumerate(tools)]
    return {"kind": "create", "title": "Orders to invoices", "summary": "Makes an invoice for each order.",
            "steps": [ask, *steps], **more}


class TestTheConversationKeepsTheSpecsItRead:
    async def test_a_read_keeps_the_api_by_its_name_and_where_its_spec_is(self, run):
        out = await _call(run, "read_api_spec", {"spec": _vault(SPEC), "name": "Ledgerline"})
        assert [t["tool"] for t in out["tools"]] == [CREATE], out
        assert run.shared_state.get(SPECS) == {"ledgerline": {"name": "Ledgerline", "spec": "specs/ledgerline.json"}}

    async def test_a_read_that_failed_keeps_nothing(self, run):
        out = await _call(run, "read_api_spec", {"spec": "specs/gone.json", "name": "Ledgerline"})
        assert "error" in out and not run.shared_state.get(SPECS)

    @pytest.mark.parametrize("spec", ["https://billing.ledgerline.example/openapi.json?token=s3cr3t",
                                      "https://me:s3cr3t@billing.ledgerline.example/openapi.json"])
    async def test_a_spec_url_carrying_a_key_is_never_kept(self, run, monkeypatch, spec):
        # Its read is the planner's now; the conversation's state never holds a credential (79).
        async def read(name, at):
            return {"tools": [{"name": CREATE, "method": "POST", "url": "https://billing.ledgerline.example/api/invoices",
                               "description": "Create", "inputSchema": {"type": "object"}}],
                    "base": "https://billing.ledgerline.example/api", "auth": None, "problem": ""}

        monkeypatch.setattr(own_api, "read", read)
        out = await _call(run, "read_api_spec", {"spec": spec, "name": "Ledgerline"})
        assert out.get("tools"), out
        assert "s3cr3t" not in json.dumps(run.shared_state.get(SPECS) or {})
        assert not run.shared_state.get(SPECS)

    def test_the_next_run_starts_with_them(self):
        from core.routes import builder

        carried = getattr(own_api, "carried", None)
        assert carried is not None, "the chat route's carry"
        kept = {"ledgerline": {"name": "Ledgerline", "spec": "specs/ledgerline.json"}}
        assert carried({SPECS: {**kept, "junk": "not a read"}}) == kept
        assert carried({}) == {} and carried(None) == {}
        assert "own_api.SPECS_KEY: own_api.carried(previous)" in inspect.getsource(builder)
        from core.events_public import public_state

        assert public_state({SPECS: kept, "order": "A-1"}) == {"order": "A-1"}, "the person's spec locations"
        assert SPECS not in bundled_definitions()["orchestration"]["state_schema"], "the engine's own, undeclared"


class TestAReadSpecWinsOverTheLookup:
    async def test_prod_13s_tool_is_made_from_the_persons_spec_on_its_server(self, run):
        run.shared_state[SPECS] = {"ledgerline": {"name": "Ledgerline", "spec": _vault(SPEC)}}
        found = await _examine(run, _plan("ledgerline_create_invoice"))
        assert found.errors == [], found.errors
        assert [(p.name, p.spec) for p in found.plan.apis] == [("Ledgerline", "specs/ledgerline.json")]
        assert [s.tool for s in found.plan.steps if s.type == "tool"] == [CREATE]
        [call] = [c for c in save_calls(found.plan, {}, run.shared_state) if c["tool"] == "create_http_tool"]
        assert call["args"]["url"].startswith("https://billing.ledgerline.example/api/invoices"), call["args"]
        assert "ledgerlineapi.com" not in json.dumps(call["args"])

    async def test_an_endpoint_the_spec_has_not_goes_back_with_its_tools(self, run):
        run.shared_state[SPECS] = {"ledgerline": {"name": "Ledgerline", "spec": _vault(SPEC)}}
        found = await _examine(run, _plan("ledgerline_create_invoice", "ledgerline_post_invoice"))
        assert any("`ledgerline_post_invoice`" in e and "has no POST /invoices/{id}/post" in e and CREATE in e
                   for e in found.errors), found.errors

    async def test_with_no_spec_read_the_lookups_tool_is_made_as_before(self, run):
        found = await _examine(run, _plan("ledgerline_create_invoice"))
        assert found.plan.apis == []
        [status] = found.plan.tools
        assert (status.status, status.via) == ("found", "http_tool")
        assert status.api["base_url"] == "https://api.ledgerlineapi.com/api/v1"

    async def test_a_spec_read_for_another_api_leaves_it(self, run):
        run.shared_state[SPECS] = {"orders api": {"name": "Orders API", "spec": _vault(SPEC, "specs/orders.json")}}
        found = await _examine(run, _plan("ledgerline_create_invoice"))
        assert found.plan.apis == [] and [s.tool for s in found.plan.steps if s.tool] == ["ledgerline_create_invoice"]

    async def test_a_plan_that_names_the_api_already_is_left_as_it_is(self, run):
        run.shared_state[SPECS] = {"ledgerline": {"name": "Ledgerline", "spec": _vault(SPEC)}}
        found = await _examine(run, _plan(CREATE, apis=[{"name": "Ledgerline", "spec": "specs/ledgerline.json"}]))
        assert found.errors == [], found.errors
        assert [p.name for p in found.plan.apis] == ["Ledgerline"]
