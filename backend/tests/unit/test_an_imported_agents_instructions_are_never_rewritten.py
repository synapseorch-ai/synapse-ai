"""A new agent that stands for a pasted export's Agent keeps that Agent's instructions, exactly, however the prompt
writer wrote them (F1615).

A pasted Agents SDK export says each Agent's instructions word for word, and the guide says they are copied whole by
code (F1550). When the plan made each Agent a new agent, the prompt writer (a model) wrote every new agent's
instructions, and code replaced only a text that referenced an Agent or kept at least 30% of its words. A rewrite in
the writer's own words ("You are the … Specialist for …", 12–28% of the Agent's words) was saved instead of the
instructions, for every agent of a four-Agent customer-service export.

Now a new agent stands for an export Agent when its name is that Agent's (case and spacing aside) or its purpose
names that Agent, and it gets that Agent's instructions whatever the writer wrote. A team and its members are new
agents too. An agent that stands for no Agent keeps the writer's words; a purpose naming two Agents goes back.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, export_state
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

LEAD = "Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:\n\n"

#: A made-up export in the shape of a support desk: a triage Agent and three that answer.
TRIAGE = ('Sort the message into one of: "refund", "keep", "question". Answer with the one word, nothing else.\n'
          "A message about money back is refund; one about leaving is keep; anything else is question.")
REFUND = "Offer a full refund to the original card within five days."
KEEP = ("You talk to a customer who wants to leave Harbor & Pine. Ask what went wrong, say what we can do about it, "
        "and offer one month free. Never argue; if they still want to leave, say how to cancel.")
QUESTIONS = ("You answer questions about Harbor & Pine's orders, delivery and products from what you are given.\n"
             "1. Read the question twice.\n2. Answer in at most four sentences.\n3. If you do not know, say so and "
             "point to help@example.com.\nNever promise a delivery date.")
EXPORT = f'''from agents import Agent, Runner

triage_agent = Agent(name="Triage agent", instructions="""{TRIAGE}""")
refund_agent = Agent(name="Refund agent", instructions="{REFUND}")
keep_agent = Agent(name="Keep Agent", instructions="{KEEP}")
questions_agent = Agent(name="Questions agent", instructions="""{QUESTIONS}""")
'''
BY_NAME = {"Triage agent": TRIAGE, "Refund agent": REFUND, "Keep Agent": KEEP, "Questions agent": QUESTIONS}

#: What a prompt writer wrote instead: each a rewrite in its own words, well under 30% of the Agent's.
REWRITES = {
    "Triage agent": "You are the Intake Specialist for Harbor & Pine. Read each incoming customer message carefully "
                    "and decide which team should handle it, replying with the team label only.",
    "Refund agent": "You are the Billing Specialist for Harbor & Pine. Help customers who were charged incorrectly, "
                    "explain our policy kindly, and confirm the next steps for their money.",
    "Keep Agent": "You are the Loyalty Specialist for Harbor & Pine. When someone is unhappy, listen, empathise and "
                  "look for a way to make things right before they go.",
    "Questions agent": "You are the Knowledge Specialist for Harbor & Pine. Provide friendly, accurate and concise "
                       "answers to customer questions about the store, shipping and the catalogue.",
}


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
def owner():
    builder_access.forget_runs()

    async def role(user_id):
        return Role.OWNER

    hooks.set_role_source(role)
    yield
    hooks.set_role_source(None)
    builder_access.forget_runs()


def _agent(name: str, keeps: str | None = None, **more) -> dict:
    purpose = f'Keeps the export\'s "{keeps}" instructions' if keeps else "Coordinates the desk"
    return {"name": name, "purpose": purpose, "tools": [], **more}


async def _prompts(agents: list[dict], written: dict[str, str]) -> dict:
    run = OrchestrationRun(run_id="builder_f1615", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "user_message": "x", **export_state(LEAD + EXPORT)})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.CHECKED_KEY] = {"outcome": "prompts", "plan": {"agents": agents}}
    with live(run):
        raw, _ = await execute_builder_tool_with_events(
            "submit_prompts", {"prompts": [{"agent": n, "instructions": t} for n, t in written.items()]}, _Server(),
            run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestEachAgentKeepsItsInstructions:
    async def test_every_rewritten_agent_named_as_its_agent_keeps_the_instructions(self, client, owner):
        """The four-Agent case: each new agent named as its Agent, one in another case, each rewritten."""
        names = {"Triage agent": "Triage agent", "Refund agent": "Refund agent", "Keep Agent": "Keep Agent",
                 "Questions Agent": "Questions agent"}
        out = await _prompts([_agent(n, keeps=src) for n, src in names.items()],
                             {n: REWRITES[src] for n, src in names.items()})
        assert out.get("finish") == "submit_prompts", out
        for name, src in names.items():
            assert out["prompts"][name] == BY_NAME[src], (name, out["prompts"][name][:80])

    async def test_a_renamed_agent_keeps_the_instructions_its_purpose_names(self, client, owner):
        out = await _prompts([_agent("FAQ specialist", keeps="Questions agent")],
                             {"FAQ specialist": REWRITES["Questions agent"]})
        assert out["prompts"]["FAQ specialist"] == QUESTIONS, out

    async def test_a_team_and_its_members_keep_theirs(self, client, owner):
        agents = [_agent("Refund agent", keeps="Refund agent"), _agent("Keep Agent", keeps="Keep Agent"),
                  _agent("Triage agent", keeps="Triage agent", type="team", members=["Refund agent", "Keep Agent"])]
        out = await _prompts(agents, {a["name"]: REWRITES[a["name"]] for a in agents})
        assert {n: out["prompts"][n] for n in ("Triage agent", "Refund agent", "Keep Agent")} == {
            "Triage agent": TRIAGE, "Refund agent": REFUND, "Keep Agent": KEEP}, out


class TestWhatStaysTheWritersOwn:
    async def test_an_agent_that_stands_for_no_agent_keeps_the_writers_words(self, client, owner):
        words = "You hand each message to the right specialist and keep a note of who has it."
        out = await _prompts([_agent("Desk coordinator")], {"Desk coordinator": words})
        assert out["prompts"]["Desk coordinator"] == words, out

    async def test_a_reference_still_puts_the_instructions_there(self, client, owner):
        out = await _prompts([_agent("Refund agent", keeps="Refund agent")],
                             {"Refund agent": '‹instructions of "Refund agent"›'})
        assert out["prompts"]["Refund agent"] == REFUND, out

    async def test_a_purpose_naming_two_agents_goes_back(self, client, owner):
        agent = {"name": "Helper", "purpose": 'Keeps the export\'s "Refund agent" and "Keep Agent" instructions',
                 "tools": []}
        out = await _prompts([agent], {"Helper": "You help."})
        assert "Helper" in out.get("error", "") and "Refund agent" in out["error"] and "Keep Agent" in out["error"], out


class TestAnAgentAlreadyHere:
    """A plan may use an agent the workspace already has for an export's Agent. Reusing it keeps its own instructions,
    as reusing means; the card says so when they are not the export's, so the person knows to check."""

    async def _notes(self, own: str) -> list[str]:
        ws = build_plan.Workspace(agents={"agent_q": {"id": "agent_q", "name": "Questions agent", "type": "agent",
                                                      "tools": [], "system_prompt": own}})
        plan = {"kind": "create", "title": "Desk", "summary": "Answers questions.", "steps": [
            {"id": "answer", "type": "agent", "name": "Answer", "does": "Answers the question", "agent": "agent_q",
             "prompt": "Answer: {{ state.user_input }}", "next": "end"}]}
        found = await build_plan.examine(plan, None, ws=ws, state={"user_message": "x",
                                                                  **export_state(LEAD + EXPORT)})
        return [n for n in found.notes if "Questions agent" in n]

    async def test_one_with_other_instructions_is_said_on_the_card(self, client, owner):
        [note] = await self._notes("You answer anything, briefly.")
        assert "keeps its own instructions, not the export's" in note, note

    async def test_one_with_the_exports_instructions_says_nothing(self, client, owner):
        assert await self._notes(QUESTIONS) == []
