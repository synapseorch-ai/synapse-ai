"""Instructions the person pasted reach what the build makes whole — F1550 (BLD3; MIG1's bug B, 11-12).

MIG1 pasted a 46 KB Agent Builder export (a WhatsApp shop bot, 8 agents) into Build with AI on Haiku 4.5, and six of
the eight came out 25–70% shorter, said nowhere. BLD3 built it again on Sonnet 4.6 (the CLI, $0, 11-12): every prompt
lost its typography (— to -, ’ to ', “” to "), the "Data Base Agent" kept 62% (its Examples section and every code
fence gone), the Confirmation Agent 91% and the Checkout Agent 95%. The guide said "word for word, however long", but a
model retyping 30,000 characters into one `submit_plan` call is the only copy there was, and nothing measured it. The
prompt writer was told both "under 250 words each" and "word for word".

The export says, exactly, which text is which Agent's instructions, so code copies them:
- `export.agent_instructions` reads each Agent's instructions out of the code, by its name, as written: Python parsed
  (never run), a literal, an f-string or the function that returns one; TypeScript's string and template literals,
  inline or a function's; a value read at run time is the words "given in the message" (the guide's rule) and a key
  taken out is "the key it held";
- by reference: a step's prompt or a new agent's instructions may hold `‹instructions of "<Agent name>"›`, and code
  puts that Agent's instructions there; an Agent the code does not have is sent back, naming the ones it has;
- checked: a prompt or instructions that copies most of an Agent's words, in order, but not whole has the copy
  replaced by the instructions themselves, what the step adds around them kept (its `{{ state.x }}` inputs too), and
  the card says how much the copy had kept. An ordinary build, a prompt that is not a copy, and one holding the
  instructions whole are left as written.
The guide, the code's heading and the prompt writer say so; "under 250 words" is never for copied instructions.
"""
from __future__ import annotations

import json

import pytest

from core import builder_access, builder_tools  # noqa: F401 — the builder's modules, loaded as at boot
from core.builder_tools import execute_builder_tool_with_events
from core.models_orchestration import OrchestrationRun
from core.native_builder import AGENTS_SDK_GUIDE, CREDENTIAL_MARK, build_plan, export_state
from core.orchestration.engine import live
from core.orchestration.state import SharedState
from core.product_tools import hooks
from core.product_tools.roles import Role

LEAD = "Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:\n\n"

#: The Data Base Agent's shape in MIG1's T3 (written for this test): typography, a section of examples, code fences.
DATABASE = """You are the integration node between the checkout assistant and the order database — nothing else.

Your job: take the customer’s confirmed order and write it as one JSON record, “exactly as given”.

---

### Examples

**Example 1: Valid Input**
- Input:
```
{ "customer_id": "123", "channel": "whatsapp", "total": 20000 }
```
- Output:
```
{ "status": "ok", "order_id": "d5a22bca" }
```

**Example 2: Missing field**
- Input:
```
{ "customer_id": "123" }
```
- Output:
```
{ "status": "error", "missing": ["total"] }
```

---

Never invent a total; every required field must be present and precise."""

PY_EXPORT = '''from agents import Agent, RunContextWrapper, Runner, trace
from pydantic import BaseModel


class CartContext(BaseModel):
  customer_name: str


def cart_agent_instructions(run_context: RunContextWrapper[CartContext], _agent: Agent[CartContext]):
  customer_name = run_context.context.customer_name
  return f"""You keep {customer_name}’s cart — in PYG, never USD.
Totals are sums of “price × qty”."""


data_base_agent = Agent(
  name="Data Base Agent",
  instructions="""''' + DATABASE + '''""",
  model="gpt-5",
)

cart_agent = Agent(
  name="Cart Agent",
  instructions=cart_agent_instructions,
  model="gpt-5-mini",
)

greeting_agent = Agent(name="Greeting Agent", instructions="Say hello — briefly.", model="gpt-5")

mcp_agent = Agent(
  name="Mailer",
  instructions="Send the summary with the token ''' + CREDENTIAL_MARK + ''' to the team.",
)


async def run_workflow(workflow_input):
  with trace("Shop"):
    return await Runner.run(greeting_agent, input=[], run_config=None)  # "__trace_source__": "agent-builder"
'''

TS_EXPORT = r'''import { Agent, AgentInputItem, RunContext, Runner, withTrace } from "@openai/agents";

interface ResearchContext { stateTopic: string; }
const researchAgentInstructions = (runContext: RunContext<ResearchContext>, _agent: Agent<ResearchContext>) => {
  const { stateTopic } = runContext.context;
  return `You research ${stateTopic} — one keyword at a time.
Say \`done\` when finished.`
}
const researchAgent = new Agent({
  name: "Research Agent",
  instructions: researchAgentInstructions,
  model: "gpt-5",
});

const summaryAgent = new Agent({
  name: 'Summary Agent',
  instructions: `Summarise every keyword’s findings.\n\n"Quote" sources, and use — dashes.`,
  model: "gpt-5",
});

const returnAgent = new Agent({
  name: "Return agent",
  instructions: "Offer a replacement device with \"free\" shipping.",
});
'''

CART = "You keep given in the message’s cart — in PYG, never USD.\nTotals are sums of “price × qty”."

#: The Data Base Agent as Sonnet 4.6 wrote it (its own words, kept whole by BLD3's run): typography flattened, the
#: examples and fences gone, and the step's own input line after it.
RETYPED = ("You are the integration node between the checkout assistant and the order database - nothing else.\n\n"
           "Your job: take the customer's confirmed order and write it as one JSON record, \"exactly as given\".\n\n"
           "Never invent a total; every required field must be present and precise.")
INPUT_LINE = "\n\nCheckout Agent output: {{ state.checkout_step }}"


def _state(code: str = PY_EXPORT) -> dict:
    return {"user_message": "x", **export_state(LEAD + code)}


def _step(sid: str, prompt: str, kind: str = "llm", **more) -> dict:
    return {"id": sid, "type": kind, "name": sid.replace("_", " ").title(), "does": "Does its part",
            "prompt": prompt, "next": "end", **more}


CHECKOUT = _step("checkout_step", "Check the order out.", next="database_step")


async def _examined(*steps: dict, state: dict | None = None):
    first = {**CHECKOUT, "next": steps[0]["id"]}
    plan = {"kind": "create", "title": "Mar Insumos", "summary": "Takes WhatsApp orders.", "steps": [first, *steps]}
    return await build_plan.examine(plan, None, ws=build_plan.Workspace(), state=_state() if state is None else state)


def _prompt(found, sid: str) -> str:
    assert found.plan is not None, found.errors
    return next(s.prompt for s in found.plan.steps if s.id == sid)


# ── what the code says each Agent's instructions are ──────────────────────────────────────────────────────────────

class TestReadOutOfTheCode:
    def test_python_literals_an_f_string_function_and_a_key_taken_out(self):
        from core.native_builder import export

        found = export.agent_instructions(PY_EXPORT)
        assert found["Data Base Agent"] == DATABASE
        assert found["Cart Agent"] == CART
        assert found["Greeting Agent"] == "Say hello — briefly."
        assert found["Mailer"] == "Send the summary with the token the key it held to the team."

    def test_typescript_template_and_string_literals_inline_or_a_functions(self):
        from core.native_builder import export

        found = export.agent_instructions(TS_EXPORT)
        assert found == {
            "Research Agent": "You research given in the message — one keyword at a time.\nSay `done` when finished.",
            "Summary Agent": "Summarise every keyword’s findings.\n\n\"Quote\" sources, and use — dashes.",
            "Return agent": "Offer a replacement device with \"free\" shipping.",
        }

    @pytest.mark.parametrize("text", ["Build me a workflow that drafts tweets.", "from agents import (", "new Agent({"])
    def test_anything_that_does_not_read_has_none(self, text):
        from core.native_builder import export

        assert export.agent_instructions(text) == {}

    def test_it_reads_the_code_and_never_runs_it(self):
        from core.native_builder import export

        boom = PY_EXPORT + "\nimport os\nos.remove('/nonexistent/f1550')\nraise SystemExit(3)\n"
        assert export.agent_instructions(boom)["Greeting Agent"] == "Say hello — briefly."


# ── the plan: by reference, and checked ───────────────────────────────────────────────────────────────────────────

class TestThePlan:
    async def test_a_reference_is_the_agents_instructions_word_for_word(self):
        found = await _examined(_step("database_step", '‹instructions of "Data Base Agent"›' + INPUT_LINE))
        assert _prompt(found, "database_step") == DATABASE + INPUT_LINE
        assert not found.errors, found.errors

    async def test_a_reference_in_an_evaluators_prompt_and_a_functions_instructions(self):
        judge = _step("route", '‹instructions of "Cart Agent"›', kind="evaluator",
                      branches={"a": "database_step", "b": "end"})
        found = await _examined(judge, _step("database_step", "Write the record."))
        assert _prompt(found, "route") == CART

    async def test_a_reference_to_an_agent_the_code_does_not_have_goes_back_naming_the_ones_it_has(self):
        found = await _examined(_step("database_step", '‹instructions of "Database Agent"›'))
        [said] = [e for e in found.errors if "Database Agent" in e]
        assert "database_step" in said and '"Data Base Agent"' in said and '"Cart Agent"' in said, said

    async def test_a_retyped_copy_is_the_instructions_whole_with_what_the_step_adds(self):
        """Sonnet 4.6's Data Base Agent: 62% of it, no examples, no typography; its input line after it."""
        found = await _examined(_step("database_step", RETYPED + INPUT_LINE))
        assert _prompt(found, "database_step") == DATABASE + INPUT_LINE
        [note] = [n for n in found.notes if "Data Base Agent" in n]
        assert "database_step" in note and "%" in note, note

    async def test_typography_alone_is_a_retyped_copy(self):
        flat = "Say hello - briefly."
        found = await _examined(_step("greet", "Greeting step.\n\n" + flat + "\n\nThe message: {{ state.user_input }}"))
        assert _prompt(found, "greet") == "Greeting step.\n\nSay hello — briefly.\n\nThe message: {{ state.user_input }}"

    async def test_a_value_the_step_reads_inside_the_copy_is_kept(self):
        inside = RETYPED.replace("Your job:", "The order: {{ state.checkout_step }}\n\nYour job:")
        found = await _examined(_step("database_step", inside))
        prompt = _prompt(found, "database_step")
        assert prompt.startswith(DATABASE) and "{{ state.checkout_step }}" in prompt, prompt

    async def test_an_inner_workflows_step_too(self):
        plan = {"kind": "create", "title": "Mar Insumos", "summary": "Takes WhatsApp orders.",
                "steps": [CHECKOUT, _step("database_step", "Run the writer.", kind="workflow", workflow="Writer")],
                "inner_workflows": [{"title": "Writer", "does": "Writes the record.",
                                     "steps": [_step("write", RETYPED)]}]}
        found = await build_plan.examine(plan, None, ws=build_plan.Workspace(), state=_state())
        assert found.plan is not None, found.errors
        assert found.plan.inner_workflows[0].steps[0].prompt == DATABASE


class TestLeftAsWritten:
    async def test_the_instructions_whole_with_more_around_them(self):
        whole = "You are step one.\n\n" + DATABASE + INPUT_LINE
        found = await _examined(_step("database_step", whole))
        assert _prompt(found, "database_step") == whole and not found.notes

    @pytest.mark.parametrize("prompt", [
        "Write the confirmed order to the database as one JSON record.",  # its own words, not a copy
        "Check the message against the Moderation, Jailbreak and PII checks the code names.",
        "",
    ])
    async def test_a_prompt_that_is_not_a_copy(self, prompt):
        found = await _examined(_step("database_step", prompt))
        assert _prompt(found, "database_step") == prompt

    async def test_an_ordinary_build(self):
        found = await _examined(_step("database_step", RETYPED + INPUT_LINE), state={"user_message": "x"})
        assert _prompt(found, "database_step") == RETYPED + INPUT_LINE and not found.notes


# ── the prompt writer: a new agent's instructions ─────────────────────────────────────────────────────────────────

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


async def _prompts(instructions: str, agent: str = "Data Base Agent") -> dict:
    run = OrchestrationRun(run_id="builder_f1550", orchestration_id="orch_native_builder",
                           shared_state={builder_access.CALLER_KEY: {"user_id": "u1"}, "intent": "create",
                                         **_state()})
    await SharedState(run).checkpoint()
    run.shared_state[build_plan.CHECKED_KEY] = {"outcome": "prompts", "plan": {"agents": [
        {"name": agent, "purpose": f'Keeps the export\'s "{agent}" instructions', "tools": ["send_email"]}]}}
    with live(run):
        raw, _ = await execute_builder_tool_with_events(
            "submit_prompts", {"prompts": [{"agent": agent, "instructions": instructions}]}, _Server(),
            run_id=run.run_id)
    return json.loads(raw) if isinstance(raw, str) else raw


class TestThePromptWriter:
    async def test_a_reference(self, client, owner):
        out = await _prompts('‹instructions of "Data Base Agent"›')
        assert out.get("finish") == "submit_prompts", out
        assert out["prompts"]["Data Base Agent"] == DATABASE

    async def test_a_retyped_copy(self, client, owner):
        out = await _prompts(RETYPED)
        assert out["prompts"]["Data Base Agent"] == DATABASE, out

    async def test_an_unknown_reference_goes_back(self, client, owner):
        out = await _prompts('‹instructions of "Database"›')
        assert '"Data Base Agent"' in out.get("error", ""), out


# ── what the builder is told ──────────────────────────────────────────────────────────────────────────────────────

class TestTheBuilderIsTold:
    def test_the_guide_has_the_planner_write_the_reference(self):
        rule = AGENTS_SDK_GUIDE[AGENTS_SDK_GUIDE.index("3. "):AGENTS_SDK_GUIDE.index("4. ")]
        assert '‹instructions of "<Agent name>"›' in rule and "code" in rule, rule

    def test_the_codes_heading_says_it_too(self):
        source = export_state(LEAD + PY_EXPORT)["export_source"]
        heading = source[:source.index("from agents import")]
        assert '‹instructions of "<Agent name>"›' in heading, heading

    def test_the_prompt_writer_never_shortens_copied_instructions(self):
        from core.native_builder import bundled_definitions

        writer = next(a for a in bundled_definitions()["agents"] if a["id"] == "agent_native_builder_prompt_writer")
        prompt = writer["system_prompt"]
        assert '‹instructions of "<Agent name>"›' in prompt, prompt
        limit = next(s for s in prompt.split(". ") if "250 words" in s)
        assert "copied" in limit or "pasted" in limit, limit
