"""A team's step says the team's members and how they work, as its card's agent list does (F1747).

The card's agent list reads a team's Team line from the plan's agent; each step's words read it from the agent's
parts, which carried no members and no way of working, so every team step said "Team: no members yet, up to 3 at
once" whatever the team had. Now the parts carry them: a new team's members as the plan names them and its
`at_once` and spend limit, and a team already here its members from its saved ids, each named as the agent here is,
an id with no agent here said as itself.

Near misses read as before: a team planned with no members, a team whose members work one at a time, a plain agent.
"""
from __future__ import annotations

from core.native_builder import objects as O
from core.native_builder.parts_words import describe

from tests.unit.test_a_build_is_saved_in_code import (  # noqa: F401 — the build's fixtures and helpers
    CHECK, _plan, _reviewed, workspace)

LEAD = {"name": "Onboarding Team", "purpose": "Runs each new customer's onboarding", "type": "team",
        "members": ["Welcome Writer", "Delivery Planner"],
        "team": {"mode": "team", "at_once": 2, "spend_limit_usd": 1}}
WRITER = {"name": "Welcome Writer", "purpose": "Writes welcome emails"}
PLANNER = {"name": "Delivery Planner", "purpose": "Plans first deliveries"}
STEP = {"id": "onboard", "type": "agent", "name": "Onboard", "does": "Runs the onboarding with its members",
        "agent": "Onboarding Team", "prompt": "Run the onboarding for {{ state.check }}."}
LINE = "Team: “Welcome Writer”, “Delivery Planner”, up to 2 at once, within $1.00"


def _words(*agents, step=None, saved=None) -> str:
    plan = O.Plan.model_validate({"kind": "create", "title": "Team onboarding", "summary": "Onboards each customer.",
                                  "agents": [dict(a) for a in agents], "steps": [dict(step or STEP)]})
    steps, _, _ = describe(plan, titles={}, agents=saved)
    return steps[(step or STEP)["id"]]


class TestTheStepSaysItsTeam:
    def test_a_new_teams_members_and_way_of_working(self):
        said = _words(LEAD, WRITER, PLANNER)
        assert LINE in said and "no members yet" not in said, said

    def test_a_team_already_here_names_its_members_from_their_ids(self):
        saved = {"agent_crew": {"id": "agent_crew", "name": "Crew", "type": "delegate",
                                "delegate_agent_ids": ["agent_billing", "agent_gone"],
                                "team": {"mode": "team", "at_once": 4}},
                 "agent_billing": {"id": "agent_billing", "name": "Billing"}}
        said = _words(step={**STEP, "agent": "agent_crew"}, saved=saved)
        assert 'the team "Crew"' in said, said
        assert "Team: “Billing”, “agent_gone”, up to 4 at once" in said, "an id with no agent here reads as itself"

    async def test_through_the_check_the_card_says_it(self, client):
        run, checked = await _reviewed(_plan(CHECK, STEP, agents=[LEAD, WRITER, PLANNER]), prompts={
            "Onboarding Team": "You run each new customer's onboarding.",
            "Welcome Writer": "You write welcome emails.", "Delivery Planner": "You plan first deliveries."})
        said = checked["plan"]["step_words"]["onboard"]
        assert LINE in said and "no members yet" not in said, said


class TestNearMisses:
    def test_a_team_planned_with_no_members_still_says_so(self):
        assert "Team: no members yet" in _words({**LEAD, "members": []})

    def test_members_who_work_one_at_a_time(self):
        said = _words({**LEAD, "team": {"mode": "one_at_a_time"}}, WRITER, PLANNER)
        assert "Team: “Welcome Writer”, “Delivery Planner”, one at a time" in said, said

    def test_a_plain_agent_has_no_team_line(self):
        assert "Team:" not in _words(WRITER, step={**STEP, "agent": "Welcome Writer"})
