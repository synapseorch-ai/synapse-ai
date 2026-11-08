"""A step's arguments for a tool the build makes after the yes are checked before it (F1295, prod-13; 79 11-02).

prod-13's rerun (a0da2070): the plan's Tool step "Create Draft Invoice" set `customer`, `amount` and `description` on
`ledgerline_create_invoice`, a read API's call the build makes after the yes (F944). The plan check passed it, since
the tool was not there to read, and the save after the yes refused it: "which `ledgerline_create_invoice` does not
take: it takes body". The shape a made tool takes is the plan's own (`build_save.made_schema`: a read API's path parts
and, for a call that sends, one `body`; the person's own API's operation as its spec has it, F1225), so the one rule
(`builder_guard.argument_issues`) holds it before the yes too, in the planner's turn, with the save's own words.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def desk(client):
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


async def _examine(draft: dict, **state) -> build_plan.Examined:
    run = OrchestrationRun(run_id="builder_f1295", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": "Make an invoice in Ledgerline for each order.", **state})
    await SharedState(run).checkpoint()
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


def _step(tool: str, arguments: dict | None, sid: str = "invoice") -> dict:
    step = {"id": sid, "type": "tool", "name": "Create Draft Invoice", "does": "Creates a draft invoice in Ledgerline",
            "tool": tool, "next": "end"}
    return {**step, "config": {"tool_arguments": arguments}} if arguments is not None else step


def _plan(*steps, **more) -> dict:
    return {"kind": "create", "title": "Orders to invoices", "summary": "Makes an invoice for each order.",
            "steps": [{"id": "orders", "type": "human", "name": "Give the orders", "does": "Asks for the orders",
                       "prompt": "Paste the orders.", "next": steps[0]["id"]}, *steps], **more}


# What the lookup found for Ledgerline on prod-13's rerun (plan_checked's tools): a read API, made after the yes.
LEDGERLINE = {"provider": "Ledgerline", "how": "api", "sources": ["https://ledgerlineapi.com/developers"],
              "detail": "Create invoices in Ledgerline billing system via REST API.",
              "api": {"base_url": "https://api.ledgerlineapi.com/api/v1",
                      "auth": {"in": "header", "name": "Authorization", "scheme": "Bearer"},
                      "docs_url": "https://ledgerlineapi.com/developers/recipes",
                      "endpoints": [{"method": "POST", "path": "/invoices", "does": "Create a new draft invoice",
                                     "tool": "ledgerline_create_invoice"},
                                    {"method": "POST", "path": "/invoices/{id}/post", "does": "Post the invoice",
                                     "tool": "ledgerline_post_invoice"}]}}
FOUND = {"plan_findings": {"goal": "x", "options": [LEDGERLINE]}}
FLAT = {"customer": "{{ state.orders }}", "amount": "{{ state.orders }}", "description": "{{ state.orders }}"}
SAID = ("sets 'customer', 'amount', 'description', which `ledgerline_create_invoice` does not take: it takes body")


def _said(found: build_plan.Examined, words: str) -> bool:
    return any(words in e for e in found.errors)


class TestALookedUpApisToolBeforeTheYes:
    async def test_prod_13s_flat_arguments_are_said_with_the_saves_words(self, client):
        found = await _examine(_plan(_step("ledgerline_create_invoice", FLAT)), **FOUND)
        assert [t.status for t in found.plan.tools] == ["found"], "made after the yes, not here now"
        assert _said(found, SAID), found.errors

    async def test_what_it_sends_in_body_is_what_it_takes(self, client):
        found = await _examine(_plan(_step("ledgerline_create_invoice", {"body": FLAT})), **FOUND)
        assert not _said(found, "does not take"), found.errors

    async def test_a_path_part_left_out_is_said(self, client):
        found = await _examine(_plan(_step("ledgerline_post_invoice", {"body": {}})), **FOUND)
        assert _said(found, "leaves out 'id', which `ledgerline_post_invoice` needs"), found.errors
        found = await _examine(_plan(_step("ledgerline_post_invoice", {"id": "{{ state.orders }}"})), **FOUND)
        assert not _said(found, "ledgerline_post_invoice"), found.errors

    async def test_arguments_left_to_the_model_are_not_checked(self, client):
        found = await _examine(_plan(_step("ledgerline_create_invoice", None)), **FOUND)
        assert not _said(found, "ledgerline_create_invoice` does not take"), found.errors


# The person's own API (F1225): its operation's arguments, as its spec has them, flat.
SPEC = {
    "openapi": "3.0.0", "info": {"title": "Orders", "version": "1"},
    "servers": [{"url": "https://api.orders.example/v1"}],
    "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
    "paths": {"/orders/{order_id}": {"get": {"operationId": "getOrder", "summary": "Get an order", "parameters": [
        {"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}},
        {"name": "expand", "in": "query", "schema": {"type": "string"}}]}}},
}
GET_ORDER = "orders_api_getorder"


def _own(arguments: dict) -> dict:
    get_blob_store().put("vault/specs/orders.json", json.dumps(SPEC))
    return _plan(_step(GET_ORDER, arguments, "look"), apis=[{"name": "Orders API", "spec": "specs/orders.json"}])


class TestThePersonsOwnApisToolBeforeTheYes:
    async def test_its_own_arguments_flat_are_what_it_takes(self, client):
        found = await _examine(_own({"order_id": "{{ state.orders }}", "expand": "lines"}))
        assert not _said(found, GET_ORDER), found.errors

    async def test_an_argument_its_spec_has_not_is_said(self, client):
        found = await _examine(_own({"id": "{{ state.orders }}"}))
        assert _said(found, f"sets 'id', which `{GET_ORDER}` does not take: it takes order_id, expand"), found.errors
        assert _said(found, f"leaves out 'order_id', which `{GET_ORDER}` needs"), found.errors
