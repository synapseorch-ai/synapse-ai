"""F1224 (BLD1; CAP3 stage 1, P1): a plan carries the knowledge base it needs, made with the build as a board is.

Brief 2's help docs and brief 6's pricing pages needed a knowledge base, and the plan had no part for one: the
planner scraped instead, or asked the person to make it by hand (18 questions in brief 6). Now a plan's `knowledge`
names each base it makes or uses, with the Vault files the person has. One of that name already here is used, never
made again; a new one is checked as `create_knowledge_base` checks it; a file not in the Vault is said on the card and
left out; one of a kind an upload refuses goes back to the planner. Its agents and its Retrieve and Ingest steps name
it by name; the save makes it first (`create_knowledge_base`, `add_knowledge_file`, the tools the Knowledge screen's
own path is behind) and fills its id into them. While Knowledge is off it is Knowledge's card (F1203).
"""
from __future__ import annotations

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.knowledge import seams
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.product_tools.approved import PLACEHOLDER as MADE
from core.native_builder.build_save import save_calls
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.storage import get_blob_store

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_kb_part", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()


@pytest.fixture
async def on(client):
    assert (await client.post("/api/settings", json={"knowledge_enabled": True})).status_code == 200
    seams.set_ingest_dispatcher(RecordingDispatcher())
    get_blob_store().put("vault/help/returns.md", "# Returns\nWithin 30 days.")
    return client


FIND = {"id": "find", "type": "retrieve", "name": "Find the answer", "does": "Searches the help docs",
        "config": {"retrieve": {"kb_ids": ["help docs"], "query": "{{state.input}}"}}, "next": "answer"}
ANSWER = {"id": "answer", "type": "agent", "name": "Answer", "does": "Answers from what it found",
          "agent": "Help desk", "prompt": "Answer {state.input} from {state.find}.", "next": "end"}


def _plan(*parts: dict) -> dict:
    return {"kind": "create", "title": "Answer from the help docs", "summary": "Answers from the docs.",
            "steps": [FIND, ANSWER], "knowledge": list(parts),
            "agents": [{"name": "Help desk", "purpose": "Answers from the docs", "knowledge": ["Help Docs"]}]}


async def _examine(run, draft):
    with live(run):
        return await build_plan.examine(draft, _Server(), state=run.shared_state)


def _calls(found, run):
    return save_calls(found.plan, {}, run.shared_state)


class TestTheBuildMakesIt:
    async def test_a_new_base_is_made_first_and_named_by_its_id_everywhere(self, on, run):
        found = await _examine(run, _plan({"name": "Help docs", "purpose": "Our help pages",
                                           "files": ["help/returns.md", "/help/returns.md"]}))
        assert found.errors == [], found.errors
        [part] = found.plan.knowledge
        assert part.existing is False and part.files_missing == []
        calls = _calls(found, run)
        assert [c["tool"] for c in calls][:3] == ["create_knowledge_base", "add_knowledge_file", "create_agent"]
        made = calls[0]["makes"]
        assert made.startswith(MADE) and calls[0]["args"] == {"name": "Help docs", "description": "Our help pages"}
        assert calls[1]["args"] == {"knowledge_base": "Help docs", "path": "help/returns.md"}
        assert calls[2]["args"]["knowledge"] == [made]
        [flow] = [c for c in calls if c["tool"] == "create_orchestration"]
        assert f'"kb_ids": ["{made}"]' in flow["args"]["steps_json"]

    async def test_the_card_says_where_it_is_made_and_what_it_is_given(self, on, run):
        found = await _examine(run, _plan({"name": "Help docs", "files": ["help/returns.md"]}))
        card = render(found.plan)
        assert "## Knowledge bases\n- “Help docs”: made with it, in the Vault folder knowledge/help-docs; it is given " \
               "`help/returns.md`" in card

    async def test_a_file_not_in_the_vault_is_said_and_left_out(self, on, run):
        found = await _examine(run, _plan({"name": "Help docs", "files": ["help/returns.md", "help/gone.md"]}))
        assert found.errors == [] and found.plan.knowledge[0].files_missing == ["help/gone.md"]
        assert "`help/gone.md` is not in the Vault: add it there, then to “Help docs” on the Knowledge screen" \
            in render(found.plan)
        assert [c["args"]["path"] for c in _calls(found, run) if c["tool"] == "add_knowledge_file"] == \
            ["help/returns.md"]


class TestOneAlreadyHere:
    async def test_it_is_used_never_made_again(self, on, run):
        kb = (await on.post("/api/knowledge/bases", json={"name": "Help docs", "folder": "help"})).json()
        found = await _examine(run, _plan({"name": "help DOCS"}))
        assert found.errors == [] and found.plan.knowledge[0].existing is True
        calls = _calls(found, run)
        assert "create_knowledge_base" not in [c["tool"] for c in calls]
        [agent] = [c for c in calls if c["tool"] == "create_agent"]
        assert agent["args"]["knowledge"] == [kb["id"]]
        assert "“Help docs”: already here, used as it is" in render(found.plan)


class TestWhatGoesBack:
    async def test_a_file_an_upload_refuses_goes_back_to_the_planner(self, on, run):
        get_blob_store().put("vault/help/tool.exe", "MZ")
        found = await _examine(run, _plan({"name": "Help docs", "files": ["help/tool.exe"]}))
        assert any(e.startswith("knowledge 'Help docs': help/tool.exe:") for e in found.errors)

    async def test_a_base_named_twice_goes_back(self, on, run):
        found = await _examine(run, _plan({"name": "Help docs"}, {"name": "help docs"}))
        assert any("'help docs' is in the plan twice" in e for e in found.errors)

    async def test_knowledge_off_is_knowledge_s_card(self, client, run):
        found = await _examine(run, _plan({"name": "Help docs"}))
        assert "knowledge" in found.features
