"""A reopened review reads back the object its live event carried, masked as it was, to the same people (F1333, TRI1;
BLD1's F1328 shots; Naveen 11-04: "Build as planned"; eb's conditions: the live event's masking, the live event's
audience).

A Synapse AI review reopened from the chat's history drew from its markdown (`readPlan`, F795's fallback), because
`GET /api/builder/conversation` gave back the question's words alone: its detail cut, a diff untinted, F1308's
"Described as" gone, so the same review looked different live and reopened. A chat following a run whose journal had
shrunk the question's event past its size lost the object the same way, re-read from the run (`fromTheRun`). The waiting
question now reads back `context_key` and, when it names itself, `context`: the very object its live event carried,
chosen by the same function from the same state (`steps.asked_context`, `pauses.asked_again`), through the same public
door with the workspace's secret variables and held secrets held (`public_event`). The conversation's `pending` carries
it; the run route says it as `human_context_key` and `human_context` while the run waits. A question whose context names
nothing reads back none. Both routes stay behind the login gate: a caller without a session, an API key and a widget
visitor get nothing from either.

The value below is assembled at run time: a literal one in a source file trips secret scanners.
"""
from __future__ import annotations

import json

import httpx
import pytest

from core.native_builder import seed_native_builder
from core.native_builder.door import CREDENTIAL_MARK

from test_a_button_through_the_change_route import _monday
from test_builder_comes_back import _park
from test_builder_routes import route, stream, tool_call

CODE = "zebra-" + "4410-" + "kq77"


@pytest.fixture
async def builder():
    await seed_native_builder()


async def _pending(client, cid: str) -> dict:
    response = await client.get("/api/builder/conversation", params={"session": cid})
    assert response.status_code == 200, response.text
    return response.json()["conversation"]["pending"]


async def _review(client, fake_llm, cid: str, says: str = "Move it to 10am") -> dict:
    """A change parked on its review, through the builder's own chat."""
    await _monday(True)
    fake_llm.script([route("manage"), tool_call("submit_change", ops=[
        {"op": "set_schedule", "target": {"kind": "schedule", "name": "Monday invoices"},
         "fields": {"cron_expression": "0 10 * * 1"}, "says": says}])])
    events = await stream(client, "/api/builder/chat",
                          {"message": "Move the Monday invoices schedule to 10am", "client_session_id": cid})
    [asked] = [e for e in events if e["type"] == "human_input_required"]
    assert asked["orch_step_id"] == "step_manage_review" and asked.get("context", {}).get("object") == "change"
    return asked


class TestItReadsBackWhatItShowedLive:
    async def test_a_waiting_question(self, client, builder, fake_llm):
        asked = await _park(client, fake_llm, cid="f1333-question")
        pending = await _pending(client, "f1333-question")
        assert pending["context_key"] == asked["context_key"] == "plan_checked"
        assert pending.get("context") == asked["context"] and asked["context"]["object"] == "plan"

    async def test_a_waiting_change_review_on_both_routes(self, client, builder, fake_llm):
        asked = await _review(client, fake_llm, "f1333-change")
        pending = await _pending(client, "f1333-change")
        assert pending["context_key"] == asked["context_key"] == "change_checked"
        assert pending.get("context") == asked["context"]
        run = (await client.get(f"/api/orchestrations/runs/{asked['run_id']}")).json()
        assert run.get("human_context_key") == "change_checked" and run.get("human_context") == asked["context"]

    async def test_a_question_whose_context_names_nothing_reads_back_none(self, client, builder, seed_orchestration):
        from core.models_orchestration import Orchestration
        from core.orchestration.engine import OrchestrationEngine

        orch = await seed_orchestration(
            id="orch_f1333", entry_step_id="ask", state_schema={},
            steps=[{"id": "ask", "name": "Ask", "type": "human", "human_prompt": "Approve?", "output_key": "ok",
                    "next_step_id": None}])
        events = [e async for e in OrchestrationEngine(Orchestration.model_validate(orch), None).run(
            initial_input="go", run_id="run_f1333_words")]
        [asked] = [e for e in events if e["type"] == "human_input_required"]
        assert "context" not in asked
        run = (await client.get("/api/orchestrations/runs/run_f1333_words")).json()
        assert run["waiting_for_human"] and "human_context_key" in run and run["human_context_key"] is None
        assert "human_context" not in run


class TestItIsMaskedAsTheLiveEventIs:
    async def test_a_value_held_since_the_pause_reads_back_masked(self, client, builder, fake_llm):
        """The live event went through `public_event` with the workspace's held secrets of its moment; the read-back
        goes through it with today's, so a value saved on a card since the pause is marked in what is read back."""
        from core.store.resources import save_tool

        asked = await _review(client, fake_llm, "f1333-held", says=f"Move it to 10am, ticket {CODE}")
        assert CODE in json.dumps(asked["context"]), "not held when it was asked"
        await save_tool({"name": "tickets", "description": "Tickets.", "tool_type": "http", "method": "GET",
                         "url": "https://tickets.example.com/x", "headers": {"X-Ticket-Key": CODE}})
        pending = await _pending(client, "f1333-held")
        # Read as written (`ensure_ascii=False`): the mark's ‹ › are not ASCII, and an escaped dump never holds them.
        shown = json.dumps(pending["context"], ensure_ascii=False)
        assert CODE not in shown and CREDENTIAL_MARK in shown, shown
        run = (await client.get(f"/api/orchestrations/runs/{asked['run_id']}")).json()
        shown = json.dumps(run["human_context"], ensure_ascii=False)
        assert CODE not in shown and CREDENTIAL_MARK in shown, shown


class TestOnlyTheLiveEventsAudienceReadsIt:
    """Both routes are internal ones behind the login gate (`internal_auth`): with login on, the internal token alone
    is not enough, an API key reaches only `/api/v1/*` and a widget visitor only `/api/widget/*`."""

    PATHS = ("/api/builder/conversation", "/api/orchestrations/runs/run_f1333")

    @staticmethod
    async def _get(path: str, headers: dict) -> int:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        from core.internal_auth import InternalTokenMiddleware

        async def ok(_request):
            return JSONResponse({"pending": {"context": {"object": "change"}}})

        inner = Starlette(routes=[Route(p, ok) for p in TestOnlyTheLiveEventsAudienceReadsIt.PATHS])
        inner.add_middleware(InternalTokenMiddleware)
        transport = httpx.ASGITransport(app=inner, client=("203.0.113.9", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            return (await http.get(path, headers=headers)).status_code

    @pytest.fixture
    def gate(self, monkeypatch):
        from core import internal_auth

        monkeypatch.setenv("SYNAPSE_INTERNAL_TOKEN", "t0ken")
        monkeypatch.setattr(internal_auth, "_login_required", lambda: True)
        monkeypatch.setattr(internal_auth, "session_ok", lambda token: token == "good-session")

    @pytest.mark.parametrize("path", PATHS)
    @pytest.mark.parametrize("who, headers, refused", [
        ("no session", {"X-Synapse-Internal": "t0ken"}, 401),
        ("an API key", {"X-Synapse-Internal": "t0ken", "X-API-Key": "sk_" + "test_" + "Q" * 24}, 401),
        ("a widget visitor", {"X-Synapse-Internal": "t0ken", "Authorization": "Bearer visitor-" + "token-1"}, 401),
        ("no internal token, an API key", {"X-API-Key": "sk_" + "test_" + "Q" * 24}, 403),
    ])
    async def test_a_caller_with_less_reads_nothing(self, gate, path, who, headers, refused):
        assert await self._get(path, headers) == refused, who

    @pytest.mark.parametrize("path", PATHS)
    async def test_the_person_signed_in_reads_it(self, gate, path):
        assert await self._get(path, {"X-Synapse-Internal": "t0ken", "X-Synapse-Session": "good-session"}) == 200
