"""F1250 / F1251 (BLD1; TRI1's triage part 2, S6a/S6b): an agent the builder makes has a model this workspace can call,
and a plan's agent gets the best fit, not the cheapest.

Run 1892f failed at its second step: make-new's `create_agent` had saved `gemini-2.0-flash-lite` with no Google key,
and nothing checked it. Now every builder door that sets an agent's model (`create_agent`, `create_agents`,
`update_agent`) asks what the change route asks (`models_menu.model_refusal`): one the agent editor's picker lists, a
model said by its own id being that one, and the best fit here named when it is not; a model an agent already had is
never asked about again. The app's own agent save is unchanged (Naveen, 10-30: builder only).

The plan's pick (Naveen, 10-30, replacing F966's cheapest-first): the best fit among the workspace's own-key models,
then the platform's; the workspace's default where it fits, else the strongest within the ceiling; the cheapest only
for a narrow job (`build_plan.narrow`).
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core import model_choices
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, models_menu as MM, objects as O
from core.orchestration.engine import live
from core.orchestration.state import SharedState


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def run():
    from core.store import get_store

    await get_store()

    async def choices():
        # Google has no key here, so the picker lists none of its models (TRI1's run 1892f).
        return {"anthropic": ["claude-haiku-4-5", "claude-sonnet-4-5"], "openai": ["gpt-4.1-mini"]}

    model_choices.set_model_choices(choices)
    builder_access.forget_runs()
    run = OrchestrationRun(run_id="builder_models", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
    await SharedState(run).checkpoint()
    yield run
    builder_access.forget_runs()
    model_choices.set_model_choices(None)


async def _call(run, tool, args):
    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestTheBuildersDoors:
    async def test_create_agent_refuses_a_model_the_workspace_cannot_call_and_names_its_best_fit(self, client, run):
        out = await _call(run, "create_agent", {"name": "Pricing reader", "type": "conversational", "tools": [],
                                                "model": "gemini-2.0-flash-lite"})
        assert out["error"].startswith('"gemini-2.0-flash-lite" is not a model this workspace can use. Its best fit '
                                       "here is claude-sonnet-4-5 (")
        assert "It can use: anthropic: claude-haiku-4-5, claude-sonnet-4-5; openai: gpt-4.1-mini." in out["error"]
        assert out["error"].endswith("Or leave the model out for the workspace's default.")

    async def test_a_model_said_by_its_own_id_is_that_one_and_none_is_the_default(self, client, run):
        out = await _call(run, "create_agent", {"name": "Writer", "type": "conversational", "tools": [],
                                                "model": "Claude Haiku 4-5"})
        assert out["agent"]["model"] == "claude-haiku-4-5"
        out = await _call(run, "create_agent", {"name": "Plain", "type": "conversational", "tools": []})
        assert out["agent"]["model"] is None

    async def test_create_agents_refuses_that_one_and_makes_the_rest(self, client, run):
        out = await _call(run, "create_agents", {"agents": [
            {"name": "One", "role": "one", "type": "conversational", "tools": [], "model": "gpt-4.1-mini",
             "description": "One", "system_prompt": "Do one."},
            {"name": "Two", "role": "two", "type": "conversational", "tools": [], "model": "gemini-2.0-flash-lite",
             "description": "Two", "system_prompt": "Do two."}]})
        assert [e["index"] for e in out["errors"]] == [1]
        assert "is not a model this workspace can use" in out["errors"][0]["error"]

    async def test_update_agent_asks_only_about_a_model_it_did_not_have(self, client, run):
        await S.seed_agents([S.make_agent(id="agent_old", name="Old", model="gemini-2.0-flash-lite")])
        out = await _call(run, "update_agent", {"agent_id": "agent_old", "fields": {"model": "gemini-2.0-flash-lite",
                                                                                     "description": "Renamed"}})
        assert out["agent"]["model"] == "gemini-2.0-flash-lite"
        out = await _call(run, "update_agent", {"agent_id": "agent_old", "fields": {"model": "gemini-2.5-pro"}})
        assert "is not a model this workspace can use" in out["error"]

    async def test_the_app_s_own_save_is_unchanged(self, client):
        resp = await client.post("/api/agents", json=S.make_agent(id="agent_app", model="gemini-2.0-flash-lite"))
        assert resp.status_code == 200, resp.text


class TestANarrowJob:
    @pytest.mark.parametrize("agent, steps, narrow", [
        ({"name": "Labeller", "purpose": "Labels a ticket", "tools": ["label_ticket"]}, 1, True),
        ({"name": "Labeller", "purpose": "Labels a ticket", "tools": ["label_ticket"]}, 2, False),
        ({"name": "Writer", "purpose": "Writes", "needs": "reasoning"}, 1, False),
        ({"name": "Reader", "purpose": "Reads", "tools": ["a", "b", "c"]}, 1, False),
        ({"name": "Reader", "purpose": "Reads", "long_inputs": True}, 1, False),
        ({"name": "Helper", "purpose": "Helps", "memory": True}, 1, False),
        ({"name": "Helper", "purpose": "Helps", "knowledge": ["Help docs"]}, 1, False),
    ])
    def test_it_is_one_small_simple_job(self, agent, steps, narrow):
        assert build_plan.narrow(O.NewAgent.model_validate(agent), steps) is narrow


class TestThePlansPick:
    async def test_a_general_agent_gets_the_default_where_it_fits_and_a_narrow_one_the_cheapest(self, client,
                                                                                                monkeypatch):
        menu = [MM.ModelEntry(id=i, name=i, provider="anthropic", source="key", tools="native", vision=False,
                              reasoning=r, price_in=p / 5, price_out=p, can_pay=True)
                for i, p, r in (("claude-haiku-4-5", 5, False), ("claude-sonnet-4-5", 15, True),
                                ("gpt-4.1-mini", 1.6, False))]

        async def listed():
            return menu

        monkeypatch.setattr(MM, "menu", listed)
        monkeypatch.setattr(MM, "workspace_model", lambda: "claude-haiku-4-5")
        plan = O.Plan.from_draft({
            "kind": "create", "title": "Triage", "summary": "Triages tickets.",
            "steps": [{"id": "a", "type": "agent", "name": "Read", "does": "Reads", "agent": "Reader", "next": "b"},
                      {"id": "b", "type": "agent", "name": "Label", "does": "Labels", "agent": "Labeller",
                       "next": "c"},
                      {"id": "c", "type": "agent", "name": "Again", "does": "Reads again", "agent": "Reader",
                       "next": "end"}],
            "agents": [{"name": "Reader", "purpose": "Reads tickets", "tools": ["read_ticket"]},
                       {"name": "Labeller", "purpose": "Labels one ticket", "tools": ["label_ticket"]}]})
        picks, issues, _ = await build_plan.pick_models(plan, [])
        assert issues == []
        assert picks["Reader"].model == "claude-haiku-4-5" and picks["Reader"].reason.startswith(
            "your workspace's default model, which fits, from your own keys")
        assert picks["Labeller"].model == "gpt-4.1-mini" and picks["Labeller"].reason.startswith(
            "the lowest-cost model that fits a narrow job")
