"""TEAM1d (D289): what a delegate's Agent step will cost, read from its definition (20 Q7, Q8).

A delegate's step keeps its one `agent` entry, which carries the member and helper tasks it ran and is priced 1
credit plus 1 per task. Before the run CE1 shows that as a range: a Team agent ("as a team") runs from one task per
member up to its task budget; a one-at-a-time delegate from one hand-off up to its step's turn limit (Naveen, TEAM1d's plan).
Its model dollars are the lead's own plus its members' tasks: one per member at the agent prior's low, up to the
whole budget at the dearest member's high. 20's example: gpt-4.1-mini, 3 members, budget 6 is 4-7 credits and about
$0.01-$0.62. Every step that is not a delegate's is priced exactly as before.
"""
import pytest

from core.orchestration.estimate import (
    STEP_MAX_TURNS,
    OWN_KEYS,
    WALLET,
    ModelRate,
    model_cost,
    models,
    span,
    tasks,
)

MINI = ModelRate(OWN_KEYS, 0.40, 1.60)    # gpt-4.1-mini's rate card, USD per million tokens


def credits(step):
    return 1000.0, 1000.0


def _orch(*steps):
    return {"id": "o", "name": "o", "entry_step_id": steps[0]["id"], "steps": list(steps)}


def _agent_step(sid="s", agent_id="lead", nxt=None):
    return {"id": sid, "name": sid, "type": "agent", "agent_id": agent_id, "next_step_id": nxt}


def _team(n_members=3, budget=6, mode="team", **lead):
    members = {f"m{i}": {"id": f"m{i}", "type": "conversational", "model": "gpt-4.1-mini"} for i in range(n_members)}
    lead_agent = {"id": "lead", "type": "delegate", "model": "gpt-4.1-mini",
                  "delegate_agent_ids": list(members), "team": {"mode": mode, "task_budget": budget}, **lead}
    return {"lead": lead_agent, **members}


def _price(model):
    return MINI if model == "gpt-4.1-mini" else None


class TestTheTaskRange:
    def test_a_team_runs_one_task_per_member_up_to_its_budget(self):
        assert tasks(_agent_step(), _team(3, 6)) == (3, 6)

    def test_more_members_than_its_budget_is_the_budget_at_both_ends(self):
        assert tasks(_agent_step(), _team(8, 5)) == (5, 5)

    def test_a_one_at_a_time_delegate_runs_one_hand_off_up_to_its_steps_turn_limit(self):
        agents = _team(3, 6, mode="one_at_a_time")
        assert tasks(_agent_step(), agents) == (1, STEP_MAX_TURNS)
        agents["lead"]["max_turns"] = 30            # the agent's own limit is chat's; a step runs for its own
        assert tasks({**_agent_step(), "max_turns": 8}, agents) == (1, 8)

    def test_a_delegate_with_no_team_block_is_one_at_a_time(self):
        agents = _team(2)
        del agents["lead"]["team"]
        assert tasks(_agent_step(), agents) == (1, STEP_MAX_TURNS)

    def test_a_team_naming_no_member_may_hand_work_to_anyone_so_at_least_one_task(self):
        agents = _team(0, 6)
        assert tasks(_agent_step(), agents) == (1, 6)

    @pytest.mark.parametrize("agents, step", [
        ({"a": {"id": "a", "type": "conversational"}}, _agent_step(agent_id="a")),
        ({}, _agent_step(agent_id="missing")),
        (_team(), {"id": "x", "type": "llm"}),
    ])
    def test_nothing_else_has_tasks(self, agents, step):
        assert tasks(step, agents) is None

    def test_the_default_turn_limit_is_a_steps(self):
        from core.models_orchestration import StepConfig

        assert STEP_MAX_TURNS == StepConfig.model_fields["max_turns"].default


class TestCredits:
    def test_twenties_example_is_4_to_7_credits(self):
        out = span(_orch(_agent_step()), credits, agents=_team(3, 6), per_task=True)
        assert (out.low, out.high) == (4000.0, 7000.0)

    def test_without_per_task_a_delegate_step_is_its_one_price_as_before(self):
        out = span(_orch(_agent_step()), credits, agents=_team(3, 6))
        assert (out.low, out.high) == (1000.0, 1000.0)

    def test_a_plain_agent_step_is_unchanged(self):
        agents = {"a": {"id": "a", "type": "conversational", "model": "gpt-4.1-mini"}}
        out = span(_orch(_agent_step(agent_id="a")), credits, agents=agents, per_task=True)
        assert (out.low, out.high) == (1000.0, 1000.0)

    def test_a_delegate_inside_a_count_loop_is_priced_per_iteration(self):
        loop = {"id": "l", "name": "l", "type": "loop", "loop_count": 2, "loop_step_ids": ["s"]}
        definition = {"id": "o", "entry_step_id": "l", "steps": [loop, _agent_step()]}
        out = span(definition, credits, agents=_team(3, 6), per_task=True)
        assert (out.low, out.high) == (1000.0 + 2 * 4000.0, 1000.0 + 2 * 7000.0)


class TestModelDollars:
    def test_twenties_example_is_about_one_cent_to_62_cents(self):
        out = models(_orch(_agent_step()), agents=_team(3, 6), workspace_model="", price_model=_price)
        side = out.sides[OWN_KEYS]
        one = model_cost("agent", MINI)
        assert side.low == pytest.approx(4 * one[0]) and side.high == pytest.approx(7 * one[1])
        assert round(side.low, 2) == 0.01 and round(side.high, 2) == 0.62

    def test_the_high_end_is_the_dearest_members_and_the_low_end_each_members_own(self):
        agents = _team(2, 4)
        agents["m1"]["model"] = "dear"
        dear = ModelRate(OWN_KEYS, 4.0, 16.0)
        out = models(_orch(_agent_step()), agents=agents, workspace_model="",
                     price_model=lambda m: dear if m == "dear" else _price(m))
        side = out.sides[OWN_KEYS]
        mini, big = model_cost("agent", MINI), model_cost("agent", dear)
        assert side.low == pytest.approx(mini[0] + mini[0] + big[0])        # the lead, then one task each
        assert side.high == pytest.approx(mini[1] + 4 * big[1])             # the lead, then the budget at the dearest
        assert set(out.models) == {"gpt-4.1-mini", "dear"}

    def test_a_member_with_no_model_runs_on_its_leads(self):
        agents = _team(1, 2)
        agents["m0"].pop("model")
        agents["lead"]["model"] = "synapse.nova-pro"
        wallet = ModelRate(WALLET, 0.8, 3.2)
        out = models(_orch(_agent_step()), agents=agents, workspace_model="gpt-4.1-mini",
                     price_model=lambda m: wallet if m == "synapse.nova-pro" else _price(m))
        assert list(out.sides) == [WALLET]
        one = model_cost("agent", wallet)
        assert out.sides[WALLET].high == pytest.approx(3 * one[1])

    def test_an_unpriced_member_is_named_never_free(self):
        agents = _team(2, 4)
        agents["m1"]["model"] = "mystery"
        out = models(_orch(_agent_step()), agents=agents, workspace_model="", price_model=_price)
        assert out.unpriced == ["mystery"]

    def test_a_plain_agent_step_is_priced_as_before(self):
        agents = {"a": {"id": "a", "type": "conversational", "model": "gpt-4.1-mini"}}
        out = models(_orch(_agent_step(agent_id="a")), agents=agents, workspace_model="", price_model=_price)
        one = model_cost("agent", MINI)
        assert (out.sides[OWN_KEYS].low, out.sides[OWN_KEYS].high) == pytest.approx(one)
