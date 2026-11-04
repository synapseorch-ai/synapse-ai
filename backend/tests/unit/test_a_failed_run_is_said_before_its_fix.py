"""A failed run is said before its fix: its step and its error first, then what fixes it (F1378, REACH1; CAP3's FF18
prod-07, Naveen 11-04).

"why did my run fail? fix it" is two asks. The quick model chose which went first, and on FF18 it chose the fix: the
change route asked for an address before anything read the failed run, the loop guard ended the turn, and the cause
came turns later. The answer read the failure only when its model chose to, and a tool's 404 was read as what was
sent to it, whose fixes never touch the tool's address. These pin the class:

- A: a question about a past failure goes to the answer route first, whatever the model chose, with the fix kept
  for after it; a change that only names a failure ("add a retry") stays a change;
- B: that answer is given the failed run as code reads it: its step, its error (scrubbed as the run page scrubs it),
  what it means and what fixes it;
- C: a Tool step's own HTTP tool answering 404 or 410 has a wrong address, fixed by changing the tool, then resuming
  the run; an agent's tool or a model answering 404 is not that;
- the wire: the fix after it reads the same failure.
"""
from __future__ import annotations

import json

import pytest

from core.native_builder.classify import override
from core.native_builder.objects import Request
from core.orchestration import triage
from test_synapse_ai_triages_a_failed_run import KEY, _ago, fresh  # noqa: F401

ASKED = "why did my run fail? fix it"
INVENTORY = {"schedule": ["Morning digest"], "agent": ["Front desk"], "workflow": ["Daily price check"]}
PRICE_FEED = {"name": "price_feed", "description": "Today's prices from the supplier feed.", "tool_type": "http",
              "method": "GET", "url": "http://localhost:9418/feed/prices", "headers": {},
              "inputSchema": {"type": "object", "properties": {}}}
FETCH = {"id": "fetch", "name": "Fetch prices", "type": "tool", "forced_tool": "price_feed", "output_key": "prices"}
NOT_FOUND = "ToolCallFailed: HTTP 404 from 'price_feed': {\"detail\":\"Not Found\"}"


def _route(message, kind, **request):
    return override(Request(kind=kind, **request), message=message, state={"may_change": True, "may_act": True},
                    inventory=INVENTORY, connectable=set())


async def _failed_at(step: dict, error: str, *, entry: dict | None = None, run_id: str = "run_prices") -> None:
    from core.models_orchestration import OrchestrationRun
    from core.orchestration.state import SharedState
    from core.store.resources import save_orchestration, save_tool
    from tests._fakes.seed import make_orchestration

    await save_tool(dict(PRICE_FEED))
    await save_orchestration(make_orchestration(id="orch_prices", name="Daily price check", entry_step_id=step["id"],
                                                steps=[step]))
    run = OrchestrationRun(
        run_id=run_id, orchestration_id="orch_prices", status="failed", started_at=_ago(hours=1),
        ended_at=_ago(minutes=59), current_step_id=step["id"],
        definition={"id": "orch_prices", "name": "Daily price check", "entry_step_id": step["id"], "steps": [step]},
        step_history=[{"step_id": step["id"], "step_name": step["name"], "step_type": step["type"],
                       "status": "failed", "error": error, **(entry or {})}])
    await SharedState(run).checkpoint()


class TestTheCauseGoesFirst:
    @pytest.mark.parametrize("kind, also", [("change", "why did my run fail?"), ("change", None), ("build", None)])
    def test_a_why_it_failed_goes_to_the_answer_with_the_fix_kept_for_after(self, kind, also):
        routed = _route(ASKED, kind, also=also)
        assert (routed.kind, routed.route, routed.needs) == ("answer", "step_answer", "workspace"), routed
        assert routed.overridden_by == "cause_first" and routed.also == "fix it", routed

    @pytest.mark.parametrize("message", ["fix the schedule's time to 09:00", "rename the failing agent to Poster",
                                         "this keeps failing, can you add a retry?"])
    def test_a_change_that_only_names_a_failure_stays_a_change(self, message):
        routed = _route(message, "change")
        assert routed.kind == "change" and routed.overridden_by != "cause_first", routed


class TestAToolsWrongAddress:
    @pytest.mark.parametrize("status", [404, 410])
    async def test_a_tool_steps_http_tool_answering_not_found_is_its_address(self, client, status):
        await _failed_at(FETCH, NOT_FOUND.replace("404", str(status)))
        got = await triage.failure("run_prices")
        assert got["class"] == "tool_address" and got["fixes"] == ["update_tool", "resume_run"], got
        assert got["step"]["name"] == "Fetch prices" and got["http_status"] == status

    async def test_an_agents_tool_answering_not_found_is_not(self, client):
        step = {"id": "look", "name": "Look it up", "type": "agent", "agent_id": "a1", "output_key": "found"}
        await _failed_at(step, "ToolCallFailed: HTTP 404 from 'notion__search': not found")
        assert (await triage.failure("run_prices"))["class"] == "tool_input"

    async def test_a_model_answering_not_found_is_not(self, client):
        step = {"id": "look", "name": "Look it up", "type": "agent", "agent_id": "a1", "output_key": "found"}
        await _failed_at(step, "LLMError: HTTP 404: model not found",
                         entry={"model_failure": {"code": "provider_refused"}})
        assert (await triage.failure("run_prices"))["class"] == "model_refused"

    async def test_a_tool_step_whose_tool_is_not_an_http_tool_is_not(self, client):
        await _failed_at({**FETCH, "forced_tool": "feeds__prices"}, NOT_FOUND.replace("price_feed", "feeds__prices"))
        assert (await triage.failure("run_prices"))["class"] == "tool_input"


class TestTheAnswerIsGivenTheFailure:
    async def _pack(self, message: str) -> str:
        from core import builder_access
        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live
        from core.orchestration.state import SharedState

        run = OrchestrationRun(run_id="builder_why", orchestration_id="orch_native_builder",
                               session_id="builder:t:panel:why",
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "user_message": message,
                                             "workspace_name": "Saltmarsh", "org_profile": "",
                                             "request": {"kind": "answer", "needs": "workspace",
                                                         "route": "step_answer"}})
        await SharedState(run).checkpoint()

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        with live(run):
            raw, _ = await execute_builder_tool_with_events("answer_pack", {}, _Server(), run_id=run.run_id)
        return json.loads(raw)["pack"]

    async def test_its_step_its_error_and_its_fix_come_first(self, client):
        await _failed_at(FETCH, NOT_FOUND)
        pack = await self._pack(ASKED)
        assert "Say the failing step and its error first, then what fixes it" in pack, pack
        assert '"Fetch prices"' in pack and "HTTP 404" in pack and "Daily price check" in pack, pack
        assert "update_tool" in pack, pack

    async def test_a_key_its_error_quoted_is_not_given(self, client):
        await _failed_at(FETCH, f"ToolCallFailed: HTTP 401 from 'price_feed': {{\"error\":\"bad key {KEY}\"}}")
        pack = await self._pack(ASKED)
        assert "HTTP 401" in pack and KEY not in pack, pack

    async def test_a_message_not_about_a_failure_is_given_none(self, client):
        await _failed_at(FETCH, NOT_FOUND)
        pack = await self._pack("what does my Daily price check do?")  # its steps are said, as for any workflow named
        assert "Say the failing step and its error first" not in pack and "HTTP 404" not in pack, pack


class TestTheFixReadsTheSameFailure:
    async def test_the_change_route_is_given_it(self, client):
        from core import builder_access
        from core.builder_tools import execute_builder_tool_with_events
        from core.models_orchestration import OrchestrationRun
        from core.orchestration.engine import live
        from core.orchestration.state import SharedState

        await _failed_at(FETCH, NOT_FOUND)
        run = OrchestrationRun(run_id="builder_fix", orchestration_id="orch_native_builder",
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "user_message": "fix it"})
        await SharedState(run).checkpoint()

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        with live(run):
            raw, _ = await execute_builder_tool_with_events("read_change_context", {}, _Server(), run_id=run.run_id)
        got = json.loads(raw)["failed_run"]
        assert got["step"]["name"] == "Fetch prices" and got["fixes"][0] == "update_tool", got
