"""An import's own words are never asked about as the person's, and a value its instructions read is named (F1614).

A pasted Agents SDK export makes the turn's message the whole paste: the person's words, then the code. The builder's
period question (F1210: "When you say 'recent', which days do you mean?") read all of it, so an Agent's instructions
that said "recent" asked the person about words they never wrote. Now the question reads only what the person wrote
above the code; a period they say there is still asked, once.

Each value an Agent's instructions read at run time (a template's `${topic}`, an f-string's `{customer_name}`) was
written as the words "given in the message", so "Generate ${numKeywords} keywords about ${topic}" read "Generate given
in the message keywords about given in the message". Now each is named by its own variable: "the topic (given in the
message)". A value that is not a plain name keeps the words as they were.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from _fakes import seed as S
from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import build_plan, export_state, periods
from core.orchestration.engine import live
from core.orchestration.state import SharedState

LEAD = "Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:\n\n"
TS_RECENT = '''import { Agent, Runner } from "@openai/agents";

const newsAgent = new Agent({
  name: "News Agent",
  instructions: "Find the recent news about the shop's suppliers and list it.",
  model: "gpt-5",
});
'''
PY_LAST_WEEK = '''from agents import Agent, Runner

sales_agent = Agent(
    name="Sales Agent",
    instructions="Total last week's sales by region.",
)
'''


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(periods, "_now", lambda timezone: date(2026, 10, 29))


class _Server:
    agent_sessions: dict = {}
    tool_router: dict = {}


@pytest.fixture
async def desk():
    from core.store.resources import save_tool

    builder_access.forget_runs()
    await save_tool({"name": "read_sales", "description": "Reads the sales CSV.", "tool_type": "http",
                     "method": "GET", "url": "https://api.sales.example/v1/sales", "headers": {},
                     "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": True}})
    await S.seed_agents([S.make_agent(id="agent_desk", name="Sales desk", tools=["read_sales"])])
    yield
    builder_access.forget_runs()


PLAN = {"finish": "submit_plan", "kind": "create", "title": "Supplier news",
        "summary": "Lists supplier news and totals sales.",
        "steps": [{"id": "read", "type": "tool", "name": "Read the sales", "does": "Reads the CSV", "tool": "read_sales",
                   "next": "total"},
                  {"id": "total", "type": "agent", "name": "Total by region", "does": "Totals the sales by region",
                   "agent": "agent_desk", "prompt": "Total the sales by region from {state.read}.", "next": "end"}]}


async def _check(pasted: str) -> dict:
    run = OrchestrationRun(run_id="builder_f1614", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         "person_timezone": "Europe/London", **export_state(pasted)})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.DRAFT_KEY] = PLAN
    with live(run):
        raw, _ = await execute_builder_tool_with_events("check_build", {"draft": PLAN}, _Server(), run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


def _asked_periods(out: dict) -> list[str]:
    return [p["said"] for p in out.get("asking_periods") or []]


class TestThePeriodQuestion:
    async def test_recent_inside_an_agents_instructions_is_not_asked(self, client, desk):
        out = await _check(LEAD + TS_RECENT)
        assert _asked_periods(out) == [], out.get("question")
        assert "which days do you mean" not in (out.get("question") or ""), out.get("question")

    async def test_last_week_inside_python_instructions_is_not_asked(self, client, desk):
        out = await _check(LEAD + PY_LAST_WEEK)
        assert _asked_periods(out) == [], out.get("question")

    async def test_a_period_the_person_writes_above_the_code_is_asked_once(self, client, desk):
        out = await _check(LEAD + "Use last week's orders for the totals.\n\n" + TS_RECENT)
        assert out.get("outcome") == "ask", out
        assert _asked_periods(out) == ["last week"], out.get("question")


class TestAValueTheInstructionsRead:
    def test_a_templates_values_are_named_by_their_variables(self):
        from core.native_builder import export

        code = ('import { Agent } from "@openai/agents";\n'
                'const k = new Agent({ name: "Keywords", instructions: `Generate ${numKeywords} keywords about '
                '${topic}.` });\n')
        assert export.agent_instructions(code)["Keywords"] == (
            "Generate the num keywords (given in the message) keywords about the topic (given in the message).")

    def test_an_f_strings_values_are_named_by_their_variables(self):
        from core.native_builder import export

        code = ('from agents import Agent\n\n'
                'cart = Agent(name="Cart", instructions=f"You keep {customer_name}\'s cart.")\n')
        assert export.agent_instructions(code)["Cart"] == "You keep the customer name (given in the message)'s cart."

    @pytest.mark.parametrize("value", ["state.topic", "stateTopic", "state_topic", "runContext.context.stateTopic",
                                       "inputs.topic", "topic"])
    def test_a_value_read_through_its_state_or_input_is_named_by_its_own_name(self, value):
        """Agent Builder's exports read a state variable as `stateTopic` (from `runContext.context`): the path and
        the `state`/`input` prefix it is read through are not its name."""
        from core.native_builder import export

        code = ('import { Agent } from "@openai/agents";\n'
                f'const k = new Agent({{ name: "Keywords", instructions: `About ${{{value}}}.` }});\n')
        assert export.agent_instructions(code)["Keywords"] == "About the topic (given in the message)."

    def test_a_python_attribute_is_named_by_its_own_name(self):
        from core.native_builder import export

        code = 'from agents import Agent\n\ncart = Agent(name="Cart", instructions=f"About {state.topic}.")\n'
        assert export.agent_instructions(code)["Cart"] == "About the topic (given in the message)."

    def test_a_value_that_is_no_name_keeps_the_words(self):
        from core.native_builder import export

        code = ('import { Agent } from "@openai/agents";\n'
                'const k = new Agent({ name: "Keywords", instructions: `About ${pick(x)} and ${items[0]}.` });\n')
        assert export.agent_instructions(code)["Keywords"] == (
            "About given in the message and given in the message.")
