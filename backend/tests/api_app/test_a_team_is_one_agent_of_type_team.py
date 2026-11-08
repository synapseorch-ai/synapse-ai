"""Agents that hand work to each other, asked for as a team, are one agent of type team (F1284, prod-06; 79 11-02).

prod-06 asked for "a research team: a lead … and a writer" and got two agents of type agent in a row, a person's
step between them, the writer's step reading the lead's answer through state: no team holds them. Code tells it
from two things, both needed: the plan's own shape (two or more new agents, no team, one's step reads another's
step through `{{ state.x }}`) and the quick classify's own reason `team` (`request.why`, a closed list code already
reads for the effort offer, never the request's words). A pipeline of two agents not asked for as a team is a plan
like any other (# Structure, rule 3). The finding goes back to the planner once (D340 D3), then the card says it:
never a reason a plan cannot be built, never a rewrite.
"""
from __future__ import annotations

import json

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan
from core.orchestration.state import SharedState

ASK = ("Set up a research team: a lead who plans what to find out about a topic, and a writer who reads each source on "
       "the list and writes one summary from them all.")
TEAM = {"kind": "build", "effort": "heavy", "why": ["team"]}
SAID = ('structure: agents "Research Lead" and "Research Writer" hand their work to each other through state (step '
        'write_summary reads {{ state.plan_research }}): these agents work as a team: make an agent of type team with '
        'a lead and its members (type team, members: the agents it hands to), and one agent step for it in place of '
        'theirs')


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture(autouse=True)
async def desk(client):
    builder_access.forget_runs()
    yield
    builder_access.forget_runs()


async def _run(request: dict | None = None) -> OrchestrationRun:
    run = OrchestrationRun(run_id="builder_f1284", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": ASK, "request": TEAM if request is None else request})
    await SharedState(run).checkpoint()
    return run


async def _call(run, tool: str, args: dict):
    from core.orchestration.engine import live

    with live(run):
        raw, _ = await execute_builder_tool_with_events(tool, args, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


# prod-06's plan on P1 (t1-p1 11-02 18:06), as the planner wrote it.
TOPIC = {"id": "input_topic", "type": "human", "name": "Get research topic", "does": "Asks for the topic",
         "prompt": "What topic would you like us to research?", "next": "plan_research"}
PLAN = {"id": "plan_research", "type": "agent", "name": "Research Lead plans approach", "does": "Plans the research",
        "agent": "Research Lead", "prompt": "Plan the research approach for this topic: {{ state.input_topic }}.",
        "next": "get_sources"}
SOURCES = {"id": "get_sources", "type": "human", "name": "Provide sources", "does": "Asks for the sources",
           "prompt": "Which sources should the writer read?", "next": "write_summary"}
WRITE = {"id": "write_summary", "type": "agent", "name": "Writer creates summary", "does": "Writes the summary",
         "agent": "Research Writer", "next": "end",
         "prompt": "Based on these sources:\n\n{{ state.get_sources }}\n\nAnd this research plan:\n\n"
                   "{{ state.plan_research }}\n\nWrite one well-organised summary."}
LEAD = {"name": "Research Lead", "purpose": "Plans what to find out about a topic"}
WRITER = {"name": "Research Writer", "purpose": "Reads each source and writes one summary from them all"}


def _plan(steps, agents=(), **more) -> dict:
    return {"kind": "create", "title": "Research Team", "summary": "A lead plans the research; a writer sums it up.",
            "steps": list(steps), "agents": list(agents), **more}


PROD_06 = _plan([TOPIC, PLAN, SOURCES, WRITE], [LEAD, WRITER])


class TestAgentsHandingWorkOnAreATeam:
    async def test_prod_06s_plan_is_said_to_be_a_team(self, client):
        out = await _call(await _run(), "check_draft", {"draft": PROD_06})
        assert SAID in out["structure"], out["structure"]

    async def test_a_hand_off_with_no_step_between_is_the_same(self, client):
        plan = _plan([{**TOPIC, "next": "plan_research"}, {**PLAN, "next": "write_summary"}, WRITE], [LEAD, WRITER])
        out = await _call(await _run(), "check_draft", {"draft": plan})
        assert any(f.startswith('structure: agents "Research Lead" and "Research Writer"') for f in out["structure"])

    async def test_it_goes_back_once_then_the_card_says_it(self, client):
        run = await _run()
        first = await _call(run, "submit_plan", PROD_06)
        assert first["error"].startswith("Before this goes to the person, a better shape (said once"), first
        assert "these agents work as a team" in first["error"]
        again = await _call(run, "submit_plan", PROD_06)
        assert again.get("finish") == "submit_plan", "sent as it is: the planner's to keep"
        with __import__("core.orchestration.engine", fromlist=["live"]).live(run):
            found = await build_plan.examine(PROD_06, _Server(), state=run.shared_state)
        assert any("these agents work as a team" in n for n in found.notes)
        assert found.errors == [], "never a reason a plan cannot be built"


class TestNotATeamsWork:
    async def test_the_team_prod_06_needed_is_not_said(self, client):
        writer = {**WRITER, "helpers": True}
        lead = {**LEAD, "type": "team", "members": ["Research Writer"], "team": {"mode": "team", "spend_limit_usd": 1}}
        plan = _plan([TOPIC, {**PLAN, "next": "end"}], [writer, lead])
        out = await _call(await _run(), "check_draft", {"draft": plan})
        assert not [f for f in out["structure"] if "work as a team" in f], out["structure"]

    async def test_a_plan_that_makes_a_team_is_not_said(self, client):
        desk = {"name": "Research Desk", "purpose": "Leads the research", "type": "team",
                "members": ["Research Lead", "Research Writer"]}
        plan = _plan([TOPIC, PLAN, SOURCES, WRITE], [LEAD, WRITER, desk])
        out = await _call(await _run(), "check_draft", {"draft": plan})
        assert not [f for f in out["structure"] if "work as a team" in f], out["structure"]

    async def test_a_pipeline_not_asked_for_as_a_team_is_a_plan_like_any_other(self, client):
        for request in ({"kind": "build", "effort": "light", "why": []}, {"kind": "build", "effort": "heavy",
                                                                           "why": ["many_steps"]}, {}):
            out = await _call(await _run(request), "check_draft", {"draft": PROD_06})
            assert not [f for f in out["structure"] if "work as a team" in f], (request, out["structure"])

    async def test_asked_for_as_a_team_with_no_hand_off_is_not_said(self, client):
        # Both are needed (79): the classify's `team` alone, the writer never reading the lead's answer.
        write = {**WRITE, "prompt": "Based on these sources:\n\n{{ state.get_sources }}\n\nWrite one summary."}
        out = await _call(await _run(), "check_draft", {"draft": _plan([TOPIC, PLAN, SOURCES, write],
                                                                       [LEAD, WRITER])})
        assert not [f for f in out["structure"] if "work as a team" in f], out["structure"]

    async def test_agents_already_here_are_the_persons(self, client):
        await S.seed_agents([S.make_agent(id="agent_lead", name="Research Lead"),
                             S.make_agent(id="agent_writer", name="Research Writer")])
        plan = _plan([TOPIC, {**PLAN, "agent": "agent_lead"}, SOURCES, {**WRITE, "agent": "agent_writer"}],
                     reuse_agents=["agent_lead", "agent_writer"])
        out = await _call(await _run(), "check_draft", {"draft": plan})
        assert not [f for f in out["structure"] if "work as a team" in f], out["structure"]

    async def test_a_browser_agent_is_its_own_kind(self, client):
        browser = {**LEAD, "type": "browser", "browser": {"sites": ["example.com"]}}
        out = await _call(await _run(), "check_draft", {"draft": _plan([TOPIC, PLAN, SOURCES, WRITE],
                                                                       [browser, WRITER])})
        assert not [f for f in out["structure"] if "work as a team" in f], out["structure"]
