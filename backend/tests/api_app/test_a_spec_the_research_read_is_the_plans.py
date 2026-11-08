"""An API spec the research read becomes the plan's, as one the planner read does (F1385, TRI1; cap-02 c3, BLD1's
trace; eb 11-05; Naveen 11-05: "Build as planned").

cap-02 c3: the research answered the planner's question surely, from the Orders API's own spec (GET /orders/{order_id},
its key in X-Api-Key, the spec at its usual address), yet the redraft still had its tool missing and no `apis`: only a
spec the planner read itself (`read_api_spec`) was kept for the conversation (`own_api.SPECS_KEY`, F1298). Earlier
gates got the tool only when the planner read the spec too. Now:

- a sure answer whose source is an API spec's address is read once a run (`own_api.read`, its guard, deadline and cap),
  kept for the conversation by the spec's own title, and its tools are given with the answer, as `read_api_spec` gives
  them; a spec that cannot be read says nothing and changes nothing;
- a plan with a tool nothing here provides, while a spec this conversation read is in no part of it, goes back to the
  planner once a run with that spec's tools by name and call.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, own_api
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store

SPECS = "_specs_read"
URL = "http://localhost:9412/openapi.json"
SPEC = {
    "openapi": "3.0.0", "info": {"title": "Orders API", "version": "1"},
    "servers": [{"url": "https://orders.example/api"}],
    "components": {"securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}}},
    "paths": {"/orders/{order_id}": {"get": {
        "operationId": "getOrderStatus", "summary": "An order's status",
        "parameters": [{"name": "order_id", "in": "path", "required": True, "schema": {"type": "string"}}]}}},
}
TOOL = "orders_api_getorderstatus"
ASK = "How does the Orders API give an order's status, and how does it take its key?"


def _answer(**more) -> dict:
    return {"ask": ASK, "answer": "GET /orders/{order_id}, with the key in the X-Api-Key header.", "sources": [URL],
            "sure": True, **more}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def run(client):
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_f1385", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create",
                                         "user_message": "Tell me an order's status from our Orders API."})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


@pytest.fixture
def fetched(monkeypatch) -> list[str]:
    """Each fetch of a spec's text, by address: the spec at URL, and nothing anywhere else."""
    got: list[str] = []

    async def spec_text(spec):
        got.append(spec)
        return (json.dumps(SPEC), "") if spec == URL else (None, "its spec could not be read: 404")

    monkeypatch.setattr(own_api, "spec_text", spec_text)
    return got


async def _report(run, *answers: dict) -> dict:
    with live(run):
        raw, _ = await execute_builder_tool_with_events("report_findings", {"goal": "an order's status",
                                                                             "answers": list(answers)},
                                                         _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestTheResearchsSpecIsKept:
    async def test_a_sure_answer_from_a_spec_keeps_it_and_gives_its_tools(self, run, fetched):
        out = await _report(run, _answer())
        [answer] = out["answers"]
        assert [t["tool"] for t in answer["api"]["tools"]] == [TOOL], answer
        assert answer["api"]["tools"][0]["call"] == "GET /orders/{order_id}"
        assert "X-Api-Key" in answer["api"]["key"]
        assert run.shared_state[SPECS] == {"orders api": {"name": "Orders API", "spec": URL}}, "by the spec's title"

    async def test_one_read_per_address_a_run(self, run, fetched):
        await _report(run, _answer(), _answer(ask="And its base URL?"))
        out = await _report(run, _answer(ask="Which header takes the key?"))
        assert fetched == [URL], fetched
        assert out["answers"][0]["api"]["tools"][0]["tool"] == TOOL

    async def test_an_unreadable_spec_says_nothing_and_changes_nothing(self, run, fetched):
        gone = "http://localhost:9412/gone/openapi.json"
        out = await _report(run, _answer(sources=[gone]))
        assert out["answers"] == [_answer(sources=[gone])], "said as the research said it"
        assert SPECS not in run.shared_state

    @pytest.mark.parametrize("answer", [_answer(sure=False), _answer(sources=["http://localhost:9412/docs"])])
    async def test_an_unsure_answer_or_a_page_reads_nothing(self, run, fetched, answer):
        out = await _report(run, answer)
        assert fetched == [] and out["answers"] == [answer] and SPECS not in run.shared_state

    async def test_a_spec_with_no_name_given_is_named_by_its_title(self, fetched, monkeypatch):
        got = await own_api.read("", URL)
        assert got["problem"] == "" and got["name"] == "Orders API" and got["tools"][0]["name"] == TOOL


def _vault(spec: dict, path: str = "specs/orders.json") -> str:
    get_blob_store().put(f"vault/{path}", json.dumps(spec))
    return path


def _plan(tool: str, **more) -> dict:
    return {"kind": "create", "title": "Order status", "summary": "Tells an order's status.",
            "steps": [{"id": "s0", "type": "tool", "name": "Look the order up", "does": "Reads the order's status",
                       "tool": tool, "next": "end"}], **more}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


class TestAPlanLeavingItOutGoesBackOnce:
    async def test_a_missing_tool_goes_back_with_the_read_specs_tools(self, run):
        run.shared_state[SPECS] = {"orders api": {"name": "Orders API", "spec": _vault(SPEC)}}
        found = await _examine(run, _plan("lookup_order_status"))
        said = [e for e in found.errors if "Orders API's own spec" in e]
        assert said and "`lookup_order_status`" in said[0] and f"{TOOL} (GET /orders/{{order_id}})" in said[0], \
            found.errors
        again = await _examine(run, _plan("lookup_order_status"))
        assert not any("Orders API's own spec" in e for e in again.errors), "once a run: the planner's to leave"
        assert again.missing == ["lookup_order_status"]

    async def test_a_plan_using_it_is_left_as_it_is(self, run):
        run.shared_state[SPECS] = {"orders api": {"name": "Orders API", "spec": _vault(SPEC)}}
        found = await _examine(run, _plan(TOOL, apis=[{"name": "Orders API", "spec": "specs/orders.json"}]))
        assert not any("Orders API's own spec" in e for e in found.errors), found.errors

    async def test_with_no_spec_read_a_missing_tool_is_as_before(self, run):
        found = await _examine(run, _plan("lookup_order_status"))
        assert not any("own spec" in e for e in found.errors) and found.missing == ["lookup_order_status"]
