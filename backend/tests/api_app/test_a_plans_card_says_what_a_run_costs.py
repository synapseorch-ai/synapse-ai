"""D340 D3 (BLD1; TRI1's spec §3.7): a plan's card says about what one run costs in model calls.

From the plan and the model menu's own prices (the effort check's source): each step a model answers at its model's
price for a typical turn (4,000 in, 600 out, and 2,000 more out for a model that reasons), an agent with tools at a few turns, a team's members each, a loop's body
once per item (its cap, else per item), a plan's eval's judge on its sample, and nothing for a Tool step. A note:
never a reason a plan cannot be built.
"""
from __future__ import annotations

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, estimate, models_menu as MM, objects as O
from core.native_builder.render import render
from core.orchestration.engine import live
from core.orchestration.state import SharedState

#: A turn is (price_in × 4,000 + price_out × 600) / 1M: A $0.007, B $0.021.
A, B = "model-a", "model-b"


def _entry(model, price_in, price_out, reasoning=False):
    return MM.ModelEntry(id=model, name=model, provider="anthropic", source="key", tools="native", vision=False,
                         reasoning=reasoning, price_in=price_in, price_out=price_out, can_pay=True)


@pytest.fixture(autouse=True)
def menu(monkeypatch):
    async def listed():
        return [_entry(A, 1.0, 5.0), _entry(B, 3.0, 15.0), _entry("model-free", None, None),
                _entry("model-thinks", 3.0, 15.0, reasoning=True)]

    monkeypatch.setattr(MM, "menu", listed)
    monkeypatch.setattr(MM, "workspace_model", lambda: A)


def _ws(**agents):
    return build_plan.Workspace(agents=agents)


def _plan(steps, agents=(), models=None, **more) -> O.Plan:
    plan = O.Plan.from_draft({"kind": "create", "title": "T", "summary": "S", "steps": list(steps),
                              "agents": list(agents), **more})
    plan.models = {k: O.ModelPick(model=v, name=v, reason="test") for k, v in (models or {}).items()}
    return plan


LLM = {"id": "write", "type": "llm", "name": "Write", "does": "Writes", "prompt": "Write.", "next": "end"}
TOOL = {"id": "read", "type": "tool", "name": "Read", "does": "Reads", "tool": "read_orders", "next": "write"}


class TestTheFigure:
    async def test_a_model_step_at_its_price_and_a_tool_step_at_none(self):
        got = await estimate.of(_plan([TOOL, LLM]), _ws())
        assert got.dollars == pytest.approx(0.007) and got.per_item == 0

    async def test_an_agent_with_tools_takes_a_few_turns_on_its_picked_model(self):
        step = {"id": "triage", "type": "agent", "name": "Triage", "does": "Triages", "agent": "Triager",
                "prompt": "Triage.", "next": "end"}
        plan = _plan([step], [{"name": "Triager", "purpose": "Triages", "tools": ["read_orders"]}],
                     models={"Triager": B})
        assert (await estimate.of(plan, _ws())).dollars == pytest.approx(0.021 * 3)

    async def test_an_agent_already_here_runs_on_its_own_model_and_a_team_counts_its_members(self):
        step = {"id": "lead", "type": "agent", "name": "Lead", "does": "Leads", "agent": "agent_lead",
                "prompt": "Lead.", "next": "end"}
        ws = _ws(agent_lead={"model": B, "tools": [], "delegate_agent_ids": ["agent_m"]},
                 agent_m={"model": A, "tools": ["read_orders"]})
        assert (await estimate.of(_plan([step], reuse_agents=["agent_lead"]), ws)).dollars == pytest.approx(
            0.021 * 1 + 0.007 * 3)

    async def test_a_loop_counts_its_body_per_item_at_its_cap_else_says_per_item(self):
        loop = {"id": "each", "type": "loop", "name": "Each", "does": "For each", "loop_over": "{{ state.read }}",
                "body": ["write"], "next": "end"}
        capped = await estimate.of(_plan([{**loop, "config": {"loop_max_iterations": 10}}, LLM]), _ws())
        assert capped.dollars == pytest.approx(0.07) and capped.per_item == 0
        open_ended = await estimate.of(_plan([loop, LLM]), _ws())
        assert open_ended.dollars == 0 and open_ended.per_item == pytest.approx(0.007)
        assert "and $0.0070 more for each item its loop goes through" in open_ended.words()

    async def test_an_eval_adds_its_judge_on_its_sample(self):
        plan = _plan([LLM], eval={"name": "Q", "criteria": [{"name": "Clear"}], "judge_every": 10})
        plan.eval.judge = B
        assert (await estimate.of(plan, _ws())).dollars == pytest.approx(0.007 + 0.021 / 10)

    async def test_a_model_that_reasons_counts_its_thinking(self):
        # B's price, thinking 2,000 more output tokens a turn: (3 × 4,000 + 15 × 2,600) / 1M.
        got = await estimate.of(_plan([{**LLM, "config": {"model": "model-thinks"}}]), _ws())
        assert got.dollars == pytest.approx(0.051) and estimate.THINKING_OUT == 2_000

    async def test_a_model_with_no_price_is_said_not_guessed(self):
        got = await estimate.of(_plan([{**LLM, "config": {"model": "model-free"}}]), _ws())
        assert got.dollars is None and got.words() == ""
        mixed = await estimate.of(_plan([{**LLM, "config": {"model": "model-free"}, "next": "two"},
                                         {**LLM, "id": "two"}]), _ws())
        assert "(not counted, no price here: model-free)" in mixed.words()


class TestTheCard:
    async def test_it_is_said_on_the_card_and_by_check_draft_and_never_blocks(self, client):
        from core.store import get_store

        await get_store()
        run = OrchestrationRun(run_id="builder_cost", orchestration_id="orch_native_builder",
                               shared_state={builder_access.CALLER_KEY: {"user_id": None}, "intent": "create"})
        await SharedState(run).checkpoint()

        class _Server:
            agent_sessions: dict = {}
            tool_router: dict = {}

        draft = {"kind": "create", "title": "Digest", "summary": "Writes a digest.", "steps": [LLM]}
        with live(run):
            found = await build_plan.examine(draft, _Server(), state=run.shared_state)
        assert found.errors == []
        assert ("## Each run\nAbout $0.0070 a run in model calls; Tool, transform and code steps call no model."
                in render(found.plan))
