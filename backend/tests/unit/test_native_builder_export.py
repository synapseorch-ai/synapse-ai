"""Pasted OpenAI Agents SDK code in the builder (SW1, D253).

OpenAI shuts Agent Builder down on 2026-11-30 and its export is Agents SDK
code, so a person moving here pastes that code into Build with AI. The builder
reads it with a guide to what each construct becomes; nothing parses it and
nothing runs it. These are the guarantees around that:

- the paste is recognised (Python and TypeScript; Agent Builder's own export
  told apart from hand-written SDK code), and nothing else is;
- its credentials are gone before the run, its history or any model sees it;
- the builder's prompts read the guide and the code only while there is one,
  so every other build renders exactly what it did;
- no builder agent holds a tool that runs code, so nothing pasted can be run.

The exports below are written for these tests in the export's shape. Every
credential is assembled at run time: a literal one in a source file trips
secret scanners on both repositories, which is the point of this file.
"""
from __future__ import annotations

import pytest

from core.native_builder import (
    AGENTS_SDK_GUIDE,
    CREDENTIAL_MARK,
    bundled_definitions,
    export_state,
    read_export,
    scrub_credentials,
)
from core.orchestration.context import _fill_task_template

# Assembled, never written whole (see the module docstring).
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiJ0ZXN0LXVzZXIifQ" + "." + "c2lnbmF0dXJlLWZvci10ZXN0cw"
OPENAI_KEY = "sk-" + "proj-" + "T3stK3yF0rTh3Bu1ld3rSw1tch"
AWS_KEY = "AK" + "IA" + "Q" * 16
GITHUB_TOKEN = "gh" + "p_" + "a1" * 18
BEARER = "Zm9vYmFyYmF6cXV4" + "c2VjcmV0dG9rZW4"

PY_EXPORT = f'''from agents import Agent, HostedMCPTool, FileSearchTool, Runner, RunConfig, trace
from pydantic import BaseModel

file_search = FileSearchTool(vector_store_ids=["vs_0123456789abcdef0123456789abcdef"])
crm = HostedMCPTool(tool_config={{
  "type": "mcp",
  "server_label": "crm",
  "server_url": "https://crm.invalid/mcp",
  "authorization": "{JWT}",
  "require_approval": "never"
}})

class TriageSchema(BaseModel):
  category: str

triage = Agent(
  name="Triage",
  instructions="""Sort the request into billing or other. Answer with JSON.""",
  model="gpt-4.1-mini",
  output_type=TriageSchema,
)

billing = Agent(name="Billing", instructions="Answer billing questions.", model="gpt-5", tools=[file_search, crm])

async def run_workflow(workflow_input):
  with trace("Support"):
    triage_result = await Runner.run(triage, input=[], run_config=RunConfig(trace_metadata={{
      "__trace_source__": "agent-builder",
      "workflow_id": "wf_0123456789abcdef"
    }}))
    if triage_result.final_output.category == "billing":
      await Runner.run(billing, input=[])
'''

TS_EXPORT = f'''import {{ Agent, AgentInputItem, Runner, webSearchTool }} from "@openai/agents";
import {{ z }} from "zod";

const researcher = new Agent({{
  name: "Researcher",
  instructions: "Research the topic.",
  model: "gpt-5",
  tools: [webSearchTool()],
}});

const approvalRequest = (message: string) => {{
  // TODO: Implement
  return true;
}}

export const runWorkflow = async (workflow: {{ input_as_text: string }}) => {{
  const state = {{ iterations: 0 }};
  const headers = {{ Authorization: "Bearer {BEARER}" }};
  const runner = new Runner({{ traceMetadata: {{ __trace_source__: "agent-builder", workflow_id: "wf_abc" }} }});
  if (approvalRequest("Go on?")) {{
    while (state.iterations < 3) {{
      await runner.run(researcher, []);
      state.iterations = state.iterations + 1;
    }}
  }}
}}
'''

SDK_HANDOFFS = '''from agents import Agent, Runner

spanish = Agent(name="Spanish", instructions="Reply in Spanish.")
triage = Agent(name="Triage", instructions="Hand off by the language asked in.", handoffs=[spanish])
result = Runner.run_sync(triage, "Hola")
'''


class TestItRecognisesAnExport:
    def test_agent_builders_python_export(self):
        read = read_export(PY_EXPORT)
        assert read is not None and read.source == "agent-builder"

    def test_agent_builders_typescript_export(self):
        read = read_export(TS_EXPORT)
        assert read is not None and read.source == "agent-builder"

    def test_hand_written_sdk_code_reads_the_same_way(self):
        read = read_export(SDK_HANDOFFS)
        assert read is not None and read.source == "agents-sdk"

    def test_a_lead_line_above_the_code_is_still_an_export(self):
        assert read_export("Rebuild this OpenAI Agent Builder workflow here:\n\n" + PY_EXPORT) is not None

    @pytest.mark.parametrize("message", [
        "",
        "Build me a workflow where an agent summarises my inbox every Monday",
        "Add an approval step before the agent sends the email",
        # Another framework's agents are not this SDK's.
        "from langchain.agents import AgentExecutor\nexecutor = AgentExecutor(agent=Agent(llm), tools=[])",
        # Importing the SDK without defining an agent is not a workflow.
        "from agents import Agent\nprint('hello')",
        'import { z } from "zod";\nconst agent = new Agent({ name: "x" });',
    ])
    def test_anything_else_is_not(self, message):
        assert read_export(message) is None


class TestItTakesTheCredentialsOut:
    def test_a_hosted_mcp_servers_token(self):
        text, removed = scrub_credentials(PY_EXPORT)
        assert JWT not in text and removed == 1
        assert f'"authorization": "{CREDENTIAL_MARK}"' in text
        # What says what the server was stays, so the builder can name it.
        assert '"server_url": "https://crm.invalid/mcp"' in text and '"server_label": "crm"' in text

    def test_ids_that_are_not_secrets_stay(self):
        text, _ = scrub_credentials(PY_EXPORT)
        assert "vs_0123456789abcdef0123456789abcdef" in text and "wf_0123456789abcdef" in text

    def test_a_bearer_header_keeps_its_scheme(self):
        text, removed = scrub_credentials(TS_EXPORT)
        assert BEARER not in text and removed == 1
        assert f"Bearer {CREDENTIAL_MARK}" in text

    @pytest.mark.parametrize("secret", [OPENAI_KEY, AWS_KEY, GITHUB_TOKEN, "xox" + "b-1234567890-abcdefghij"])
    def test_provider_keys_written_into_code(self, secret):
        text, removed = scrub_credentials(f'client = Client("{secret}")\n')
        assert secret not in text and removed == 1

    def test_a_quoted_value_named_as_a_secret(self):
        text, removed = scrub_credentials('api_key = "q8w7e6r5t4y3u2i1o0p9"\nCLIENT_SECRET: "a-b-c-d-e-f-g-h-i-j"')
        assert removed == 2
        assert text == f'api_key = "{CREDENTIAL_MARK}"\nCLIENT_SECRET: "{CREDENTIAL_MARK}"'

    def test_a_private_key_block(self):
        block = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        text, removed = scrub_credentials(f"KEY = '''{block}'''")
        assert "MIIEow" not in text and removed == 1

    @pytest.mark.parametrize("reference", [
        'Authorization: "Bearer ${process.env.CRM_TOKEN}"',
        "Authorization: Bearer {{ global.CRM_TOKEN }}",
        'api_key=os.environ["OPENAI_API_KEY"]',
        "apiKey: process.env.OPENAI_API_KEY",
        'token = "your token here"',
        "max_tokens=2048",
    ])
    def test_a_reference_to_a_secret_is_not_one(self, reference):
        assert scrub_credentials(reference) == (reference, 0)

    def test_scrubbing_twice_takes_nothing_more(self):
        once, _ = scrub_credentials(PY_EXPORT + TS_EXPORT)
        assert scrub_credentials(once) == (once, 0)


class TestTheRunsState:
    def test_an_ordinary_message_is_untouched(self):
        message = f"Use the key {OPENAI_KEY} in an HTTP tool"
        assert export_state(message) == {"export_guide": "", "export_source": "", "user_message": message}

    def test_an_export_brings_the_guide_and_its_code_without_credentials(self):
        state = export_state(PY_EXPORT)
        assert state["export_guide"] == AGENTS_SDK_GUIDE
        assert state["export_source"].startswith("\n\n# The pasted Agents SDK code")
        assert state["user_message"] in state["export_source"]
        for value in state.values():
            assert JWT not in value
        assert CREDENTIAL_MARK in state["user_message"]


class TestTheBuildersPromptsReadIt:
    """The four steps that plan and build read the keys, and while they are
    empty — every build but a paste — each prompt is what it was before."""

    READERS = {
        "step_understand": "export_guide",
        "step_draft_plan": "export_guide",
        "step_create_agents": "export_source",
        "step_save_create": "export_source",
    }

    @staticmethod
    def _steps():
        return {s["id"]: s for s in bundled_definitions()["orchestration"]["steps"]}

    def test_both_keys_are_declared_empty(self):
        schema = bundled_definitions()["orchestration"]["state_schema"]
        for key in ("export_guide", "export_source"):
            assert schema[key]["type"] == "str" and schema[key]["default"] == ""

    @pytest.mark.parametrize("step_id,key", READERS.items())
    def test_each_step_reads_its_key(self, step_id, key):
        assert "{state.%s}" % key in self._steps()[step_id]["prompt_template"]

    @pytest.mark.parametrize("step_id,key", READERS.items())
    def test_empty_keys_leave_the_prompt_as_it_was(self, step_id, key):
        template = self._steps()[step_id]["prompt_template"]
        state = {"export_guide": "", "export_source": "", "knowledge_steps": "", "user_message": "hi"}
        before = _fill_task_template(template.replace("{state.%s}" % key, ""), state, {})
        assert _fill_task_template(template, state, {}) == before

    def test_the_code_reaches_the_prompt_whole_and_unrendered(self):
        """The creator copies an agent's instructions from it, so it must not be
        cut; and a `{state.x}` or `{{ … }}` inside it is the code's, not ours."""
        state = export_state(TS_EXPORT + "\n// {state.user_message} {{ global.SECRET }}\n")
        rendered = _fill_task_template(self._steps()["step_create_agents"]["prompt_template"],
                                       {**state, "plan_draft": "p", "selected_agent_ids": [],
                                        "can_create_agents": True, "user_message": "x"},
                                       {"SECRET": "s3cr3t"})
        assert state["user_message"] in rendered
        assert "{state.user_message} {{ global.SECRET }}" in rendered and "s3cr3t" not in rendered

    def test_a_bare_paste_is_routed_to_create(self):
        create = self._steps()["step_intent"]["route_descriptions"]["create"]
        assert "Pasted OpenAI Agents SDK code" in create

    def test_the_agent_creator_copies_exported_instructions_as_written(self):
        creator = next(a for a in bundled_definitions()["agents"]
                       if a["id"] == "agent_native_builder_agent_creator")
        assert "Instructions from pasted code are copied, not written." in creator["system_prompt"]
        assert "one `create_agent` call each" in creator["system_prompt"]


class TestTheGuide:
    def test_it_asks_for_what_was_left_behind(self):
        assert "## Not carried over" in AGENTS_SDK_GUIDE
        assert CREDENTIAL_MARK in AGENTS_SDK_GUIDE

    def test_it_never_turns_the_code_into_code_here(self):
        assert "never with code copied from the export" in AGENTS_SDK_GUIDE
        assert "code is never copied" in AGENTS_SDK_GUIDE

    @pytest.mark.parametrize("step_type", ["if_else", "switch", "loop", "human", "evaluator", "extract_json",
                                           "retrieve", "delegate", "llm", "print"])
    def test_it_names_the_steps_the_constructs_become(self, step_type):
        assert f"`{step_type}`" in AGENTS_SDK_GUIDE


class TestNothingPastedCanBeRun:
    """The builder never runs code while it builds, so nothing in a paste can
    be run by pasting it. That holds while every builder agent keeps to the
    builder's own tools — none of which runs anything — and takes no defaults."""

    RUNS_CODE = {"execute_python", "tool_code", "run_bash", "run_sql_query", "all"}

    def test_no_builder_tool_runs_code(self):
        from core.builder_tools import BUILDER_TOOL_NAMES

        assert not (BUILDER_TOOL_NAMES & self.RUNS_CODE)

    def test_every_builder_agent_holds_only_builder_tools(self):
        from core.builder_tools import BUILDER_TOOL_NAMES

        for agent in bundled_definitions()["agents"]:
            if agent.get("type") == "orchestrator":
                # The shell that runs the builder orchestration; it calls no model itself.
                assert not agent.get("tools"), agent["id"]
                continue
            assert agent.get("skip_default_tools") is True, agent["id"]
            assert set(agent.get("tools") or []) <= BUILDER_TOOL_NAMES, agent["id"]
