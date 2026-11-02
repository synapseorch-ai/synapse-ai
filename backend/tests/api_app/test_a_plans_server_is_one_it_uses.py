"""A plan's MCP server is one something in it uses, and the server the person gave is in it (F1290 b+c, CAP3's cap-03;
79 11-02).

Cap-03's A2: the person gave their tracker's MCP server in a question's answer, the plan had no server at all, and its
agent was built "with no tools"; a plan can also make a server and give it to nothing. Code holds both, from the plan
and the person's own words, never guessed from wording:

* (b) a server the plan makes or connects that no agent has in its `servers`, and whose tools no agent or Tool step
  names (`<server>__tool`), is sent back naming it;
* (c) a URL the person gave (their message, the conversation, their answers) whose path is an MCP endpoint's (ends
  `/mcp` or `/sse`), that is neither the plan's server nor one here, is sent back: put it in mcp_servers and give it
  to the agent that uses it. `/mcp` in a query string, or a page about MCP, is not one.
"""
from __future__ import annotations

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_f1290", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create",
                                         "user_message": "Triage new tickets from our tracker."})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


TRIAGE = {"id": "triage", "type": "agent", "name": "Triage", "does": "Triages the tickets", "agent": "Triage",
          "prompt": "Triage {{ state.input }}.", "next": "end"}
SERVER = {"name": "Tracker", "url": "https://tracker.example/mcp"}


def _plan(agent: dict, **more) -> dict:
    return {"kind": "create", "title": "Triage tickets", "summary": "Triages new tickets in our tracker.",
            "steps": [TRIAGE], "agents": [{"name": "Triage", "purpose": "Triages tickets", **agent}], **more}


async def _said(run, draft, prefix: str) -> list[str]:
    with live(run):
        found = await build_plan.examine(draft, _Server(), state=run.shared_state)
    return [e for e in found.errors if prefix in e]


class TestAServerSomethingUses:
    async def test_one_nothing_uses_goes_back_naming_it(self, client, run):
        said = await _said(run, _plan({}, mcp_servers=[SERVER]), "nothing in the plan uses it")
        assert said and "'Tracker'" in said[0], said

    async def test_an_agents_server_is_used(self, client, run):
        assert await _said(run, _plan({"servers": ["Tracker"]}, mcp_servers=[SERVER]), "nothing in the plan uses") == []

    async def test_a_tool_of_it_is_used(self, client, run):
        draft = _plan({"tools": ["tracker__list_tickets"]}, mcp_servers=[SERVER])
        assert await _said(run, draft, "nothing in the plan uses") == []


class TestTheServerThePersonGave:
    @pytest.mark.parametrize("where", ["user_message", "plan_answers"])
    async def test_given_and_left_out_goes_back(self, client, run, where):
        words = "It's our custom tracker on the MCP server at https://tracker.example/mcp."
        run.shared_state[where] = words if where == "user_message" else [{"question": "Which tracker?",
                                                                          "answer": words}]
        said = await _said(run, _plan({}), "gave their MCP server")
        assert said and "https://tracker.example/mcp" in said[0], said

    async def test_an_sse_endpoint_too(self, client, run):
        run.shared_state["user_message"] = "Tickets come from https://tracker.example/v1/sse/ please."
        assert await _said(run, _plan({}), "gave their MCP server")

    async def test_in_the_plan_it_is_not_said(self, client, run):
        run.shared_state["user_message"] = "Our tracker's MCP server is https://tracker.example/mcp"
        draft = _plan({"servers": ["Tracker"]}, mcp_servers=[SERVER])
        assert await _said(run, draft, "gave their MCP server") == []

    async def test_one_saved_here_is_not_said(self, client, run):
        from core.store.resources import save_mcp_server

        await save_mcp_server({"name": "tracker", "label": "Tracker", "url": "https://tracker.example/mcp"})
        run.shared_state["user_message"] = "Our tracker's MCP server is https://tracker.example/mcp"
        assert await _said(run, _plan({}), "gave their MCP server") == []

    @pytest.mark.parametrize("words", [
        "See https://docs.example.com/page?next=/mcp for how it works.",
        "We read https://modelcontextprotocol.io/docs/getting-started first.",
        "The guide is https://example.com/blog/what-is-mcp and it helped.",
    ])
    async def test_mcp_in_a_query_or_a_page_about_it_is_left_alone(self, client, run, words):
        run.shared_state["user_message"] = words
        assert await _said(run, _plan({}), "gave their MCP server") == []
