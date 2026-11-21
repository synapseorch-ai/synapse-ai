"""A knowledge base the plan makes or uses that nothing in the plan reads or fills goes back to the planner, with how
to put it right (F1646).

Built from a drawing of a support inbox, a plan listed the FAQ knowledge base in its `knowledge` and made an Answerer
agent "to answer from the FAQ", but no agent's `knowledge` named the base and no retrieve step read it: the base
would be made and the Answerer would answer from nothing. The plan check already sends back an inner workflow nothing
runs; a knowledge base had no such line.

A base is read when a new agent's `knowledge` names it, when an agent the plan reuses already answers from it, or
when a retrieve step searches it; a base an ingest step fills is used too (a workflow that keeps a base up to date).

Near misses: each of those four ways passes; a plan with no knowledge bases says nothing.
"""
from __future__ import annotations

import pytest

from _fakes import seed as S
from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.engine import live
from core.orchestration.state import SharedState

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_kb_unread", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    return client


async def _kb(client, name="FAQ", folder="faq"):
    resp = await client.post("/api/knowledge/bases", json={"name": name, "folder": folder})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _draft(*, steps=None, agents=None, reuse=None, knowledge=("FAQ",)) -> dict:
    return {"kind": "create", "title": "Support inbox", "summary": "Answers customer questions.",
            "steps": steps or [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers the question",
                                "agent": "Answerer", "prompt": "Answer {state.input}.", "next": "end"}],
            "agents": agents if agents is not None else [{"name": "Answerer", "purpose": "Answers from the FAQ"}],
            "reuse_agents": reuse or [],
            "knowledge": [{"name": n, "purpose": "Customer questions and answers"} for n in knowledge]}


async def _errors(run, draft) -> list[str]:
    with live(run):
        found = await build_plan.examine(draft, _Server(), state=run.shared_state)
    return [e for e in found.errors if "never read" in e]


class TestABaseNothingReads:
    async def test_the_drawing_s_plan_goes_back_with_how_to_put_it_right(self, on, run):
        await _kb(on)
        [said] = await _errors(run, _draft())
        assert "knowledge 'FAQ' is never read" in said, said
        assert "`knowledge` of the agent that answers from it" in said and "retrieve step" in said, said

    async def test_a_new_base_nothing_reads_goes_back_too(self, on, run):
        [said] = await _errors(run, _draft(knowledge=("Returns policy",)))
        assert "knowledge 'Returns policy' is never read" in said, said


class TestNearMisses:
    async def test_a_new_agent_that_answers_from_it(self, on, run):
        await _kb(on)
        assert await _errors(run, _draft(agents=[{"name": "Answerer", "purpose": "Answers", "knowledge": ["faq"]}])) \
            == []

    async def test_an_agent_the_plan_reuses_that_already_answers_from_it(self, on, run):
        kb = await _kb(on)
        await S.seed_agents([S.make_agent(id="agent_answerer", name="Answerer", knowledge=[kb["id"]])])
        steps = [{"id": "answer", "type": "agent", "name": "Answer", "does": "Answers the question",
                  "agent": "agent_answerer", "prompt": "Answer {state.input}.", "next": "end"}]
        assert await _errors(run, _draft(steps=steps, agents=[], reuse=["agent_answerer"])) == []

    async def test_a_retrieve_step_that_searches_it(self, on, run):
        kb = await _kb(on)
        steps = [{"id": "find", "type": "retrieve", "name": "Find", "does": "Searches the FAQ", "next": "end",
                  "config": {"retrieve": {"kb_ids": [kb["id"]], "query": "{{ state.input }}"}}}]
        assert await _errors(run, _draft(steps=steps, agents=[])) == []

    async def test_an_ingest_step_that_fills_it(self, on, run):
        kb = await _kb(on)
        steps = [{"id": "fill", "type": "ingest", "name": "Fill", "does": "Adds the page", "next": "end",
                  "config": {"ingest": {"kb_id": kb["id"]}}}]
        assert await _errors(run, _draft(steps=steps, agents=[])) == []

    async def test_a_plan_with_no_knowledge_says_nothing(self, on, run):
        assert await _errors(run, _draft(knowledge=())) == []
