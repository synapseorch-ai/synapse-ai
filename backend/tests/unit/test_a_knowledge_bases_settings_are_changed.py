"""A knowledge base's plain settings are changed by a change, as its screen changes them (F1276; Naveen 10-30).

Its language, whether visitors may search it, and its monthly budget: the op `knowledge_settings`, a write, through
the Knowledge screen's own model and validators (`KnowledgeBaseUpdate`) at the base's current revision, and the host's
own save. The review shows each one before → after. A new language indexes the base again beside the index in use,
and the review says what that costs by the screen's own estimate (Naveen: show the estimate). Opening a base to
visitors exposes what is in it, so it needs the screen's role (a builder), says plainly who can then search it, and is
never on one card with another change (8a). "Undo that" sets them back.
"""
from __future__ import annotations

import json

import pytest

from _fakes.knowledge_db import knowledge_store  # noqa: F401 — fixture
from _fakes.knowledge_env import enable_knowledge, use_saved_settings
from core import builder_access
from core.builder_tools import execute_builder_tool_with_events
from core.knowledge import bases, costs, seams
from core.knowledge.api_models import CostPreview, KnowledgeBaseCreate
from core.knowledge.testing.fakes import RecordingDispatcher
from core.models_orchestration import OrchestrationRun
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import confirm, hooks
from core.product_tools.roles import Role

pytestmark = pytest.mark.usefixtures("knowledge_store")


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def world(monkeypatch):
    seams.reset_all()
    use_saved_settings(monkeypatch)
    await enable_knowledge()
    seams.set_ingest_dispatcher(RecordingDispatcher())
    confirm.set_claim_store(None)
    builder_access.forget_runs()
    role = {"role": Role.OWNER}

    async def source(user_id):
        return role["role"]

    hooks.set_role_source(source)

    async def priced(kb, overrides=None, **_):
        return CostPreview(docs=12, pages=30, total_est_usd=0.04)

    monkeypatch.setattr(costs, "preview", priced)
    kb = await bases.create_base(KnowledgeBaseCreate(name="Support handbook", description="x",
                                                     folder="knowledge/support"))
    yield {"kb": kb, "role": role}
    hooks.set_role_source(None)
    builder_access.forget_runs()
    seams.reset_all()


_N = {"n": 0}


async def _run():
    _N["n"] += 1
    run = OrchestrationRun(run_id=f"builder_kset_{_N['n']}", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}})
    await SharedState(run).checkpoint()
    return run


async def _call(tool, args, run):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw)


async def _change(draft):
    run = await _run()
    checked = await _call("check_change", {"draft": {"finish": "submit_change", **draft}}, run)
    if checked["outcome"] != "review":
        return run, checked, None
    run.shared_state["change_checked"] = json.dumps(checked)
    run.shared_state["manage_review"] = {"feedback": "approve"}
    await _call("approve_change", {}, run)
    return run, checked, await _call("apply_change", {}, run)


def _set(kb, **fields):
    return {"op": "knowledge_settings", "target": {"kind": "knowledge_base", "name": kb.name}, "fields": fields}


async def _kb(world):
    return await bases.get_base(world["kb"].id)


class TestEachSetting:
    async def test_a_new_language_says_its_rebuild_and_cost(self, world):
        _, checked, applied = await _change({"ops": [_set(world["kb"], language="english")]})
        assert "language: simple → english" in checked["review"], checked["review"]
        assert "indexed again" in checked["review"] and "about $0.04 for its 12 documents" in checked["review"]
        assert applied["applied"] == ["knowledge_settings"], applied
        assert (await _kb(world)).language == "english"

    async def test_its_cost_unknown_is_said(self, world, monkeypatch):
        async def broken(kb, overrides=None, **_):
            raise RuntimeError("no embedder")

        monkeypatch.setattr(costs, "preview", broken)
        _, checked, _ = await _change({"ops": [_set(world["kb"], language="french")]})
        assert "could not be estimated here" in checked["review"]

    async def test_a_budget_is_set_and_cleared(self, world):
        _, checked, _ = await _change({"ops": [_set(world["kb"], budget_monthly_usd=25)]})
        assert "monthly budget: no budget → $25.00 a month" in checked["review"]
        assert (await _kb(world)).budget_monthly_usd == 25
        _, checked, _ = await _change({"ops": [_set(world["kb"], clear_budget=True)]})
        assert "$25.00 a month → no budget" in checked["review"]
        assert (await _kb(world)).budget_monthly_usd is None

    async def test_nothing_to_change_is_said(self, world):
        _, checked, _ = await _change({"ops": [_set(world["kb"], language="simple")]})
        assert checked["outcome"] == "said" and "already that way" in checked["say"]


class TestTheScreensChecks:
    @pytest.mark.parametrize("fields, says", [({"language": "klingon"}, "language` is one of"),
                                              ({"budget_monthly_usd": -5}, "0 or more"),
                                              ({"budget_monthly_usd": 5, "clear_budget": True}, "not both")])
    async def test_a_value_the_screen_refuses_is_sent_back(self, world, fields, says):
        out = await _call("submit_change", {"ops": [_set(world["kb"], **fields)]}, await _run())
        assert says in json.dumps(out), out

    async def test_a_base_changed_since_is_said(self, world):
        from core.knowledge.api_models import KnowledgeBaseUpdate

        run = await _run()
        checked = await _call("check_change", {"draft": {"finish": "submit_change",
                                                         "ops": [_set(world["kb"], language="english")]}}, run)
        assert checked["outcome"] == "review", checked
        await bases.apply_update(world["kb"].id, KnowledgeBaseUpdate(revision=world["kb"].revision, language="german"))
        run.shared_state["change_checked"] = json.dumps(checked)
        run.shared_state["manage_review"] = {"feedback": "approve"}
        await _call("approve_change", {}, run)
        applied = await _call("apply_change", {}, run)
        assert applied["applied"] == [] and (await _kb(world)).language == "german", applied
        assert 'the knowledge base "Support handbook" changed since you approved this' in " ".join(applied["failed"])


class TestOpeningItToVisitors:
    async def test_it_says_who_can_then_search_it(self, world):
        _, checked, applied = await _change({"ops": [_set(world["kb"], visitor_access=True)]})
        assert "visitors may search it: off → on" in checked["review"]
        assert 'will be able to search "Support handbook": everything in it' in checked["review"]
        assert applied["applied"] == ["knowledge_settings"] and (await _kb(world)).visitor_access is True

    async def test_never_with_another_setting_in_one_op(self, world):
        out = await _call("submit_change", {"ops": [_set(world["kb"], visitor_access=True, language="english")]},
                          await _run())
        assert "a change of its own" in json.dumps(out), out

    async def test_never_on_one_card_with_another_change(self, world):
        other = await bases.create_base(KnowledgeBaseCreate(name="Contracts", description="y",
                                                            folder="knowledge/contracts"))
        _, checked, applied = await _change({"ops": [_set(world["kb"], visitor_access=True),
                                                     _set(other, language="english")]})
        assert "is a change of its own: ask for it by itself" in json.dumps(checked), checked
        assert (await _kb(world)).visitor_access is False
        assert (await bases.get_base(other.id)).language == "english", "the rest is still made"

    @pytest.mark.parametrize("role", [Role.APPROVER, Role.VIEWER])
    async def test_it_needs_the_screens_role(self, world, role):
        world["role"]["role"] = role
        _, checked, _ = await _change({"ops": [_set(world["kb"], visitor_access=True)]})
        assert checked["outcome"] == "said" and "needs the builder role" in checked["say"], checked
        assert (await _kb(world)).visitor_access is False


class TestUndo:
    async def test_undo_sets_them_back(self, world):
        first, _, _ = await _change({"ops": [_set(world["kb"], language="english")]})
        _, checked, undone = await _change({"undo_of": first.run_id})
        assert checked["outcome"] == "review" and "english → simple" in checked["review"], checked
        assert undone["applied"] == ["knowledge_settings"] and (await _kb(world)).language == "simple"


async def test_the_settings_are_read_with_each_base(world):
    from core.builder_readers import read_workspace, shape_problems

    read = await read_workspace({"topic": "knowledge"}, None, [])
    [kb] = read["items"]
    assert (kb["language"], kb["visitor_access"], kb["budget_monthly_usd"]) == ("simple", False, None)
    assert shape_problems("knowledge", {k: v for k, v in read.items() if k != "topic"}) == []
