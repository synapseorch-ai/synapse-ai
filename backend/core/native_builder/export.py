"""An OpenAI Agents SDK export pasted into the builder (SW1, D253).

OpenAI shuts Agent Builder down on 2026-11-30. Its only way out is the Code
button: Python or TypeScript Agents SDK code — `Agent(...)` objects and a
`run_workflow` whose branches, loops and approvals are plain code over a
`state` dict. Nothing here parses that code, and nothing ever runs it. The
builder's own models read it, the way they read any message, with a guide to
what each construct becomes here.

What this module does is decide whether a message *is* such code, and take the
credentials out of it first. An export can hold live ones — a hosted MCP
server's `authorization` is a bearer token written into the source — and the
message would otherwise sit in the run's state, its checkpoints and the
model's context for as long as those last.

Read by both products' builder routes; the state keys it returns are what the
builder's prompts read (`orchestration.json`), the way `knowledge_steps` is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The scrubber lives in a neutral module since boards refuse card text that holds a
# credential (BOARD1a, D280); these names stay importable from here.
from core.secret_scrub import CREDENTIAL_MARK, _CREDENTIALS, scrub_credentials  # noqa: F401

#: Every Agent Builder export stamps its runs with this; hand-written SDK code
#: does not. It is how the two are told apart.
_AGENT_BUILDER_MARK = re.compile(r"""["']?__trace_source__["']?\s*:\s*["']agent-builder["']""")

_PY_IMPORT = re.compile(r"^\s*from\s+agents(?:\.[\w.]+)?\s+import\b", re.M)
_PY_AGENT = re.compile(r"\bAgent\s*\(")
_TS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*)["']@openai/agents(?:-[\w-]+)?["']""")
_TS_AGENT = re.compile(r"\bnew\s+Agent\s*\(")


@dataclass(frozen=True)
class ExportRead:
    """A pasted export, ready for the builder."""

    #: `agent-builder` when the code carries Agent Builder's own run marker,
    #: else `agents-sdk` (hand-written SDK code reads the same way).
    source: str
    #: The whole message with every credential replaced by `CREDENTIAL_MARK`.
    text: str
    #: How many credentials were replaced.
    removed: int


def read_export(message: str) -> ExportRead | None:
    """The export in `message`, or None when it holds no Agents SDK code."""
    if not message:
        return None
    python = bool(_PY_IMPORT.search(message)) and bool(_PY_AGENT.search(message))
    typescript = bool(_TS_IMPORT.search(message)) and bool(_TS_AGENT.search(message))
    if not (python or typescript):
        return None
    source = "agent-builder" if _AGENT_BUILDER_MARK.search(message) else "agents-sdk"
    text, removed = scrub_credentials(message)
    return ExportRead(source=source, text=text, removed=removed)


def export_state(message: str) -> dict[str, str]:
    """The builder run's export keys, and the message the run should carry.

    An ordinary message comes back as it was, with both keys empty, so every
    prompt that reads them renders exactly what it did before.
    """
    read = read_export(message)
    if read is None:
        return {"export_guide": "", "export_source": "", "user_message": message}
    return {
        "export_guide": AGENTS_SDK_GUIDE,
        "export_source": _SOURCE_HEADING + read.text,
        "user_message": read.text,
    }


# What the agent creator and the create saver see below the plan. They read the
# plan for *what* to build; this is where the words come from. The prompts put
# `{state.export_source}` straight after their last line, so it brings its own
# blank line and an ordinary build's prompt is unchanged.
_SOURCE_HEADING = """\


# The pasted Agents SDK code this build translates
Copy text from it, never code. Every new agent, llm step or evaluator step that stands for an `Agent` here (the \
same name, or the plan says so) gets that Agent's `instructions` (or the string its instructions function returns) \
word for word — as the new agent's system prompt, or as the step's prompt — however long, and whatever the plan's \
purpose line says. Create such agents one `create_agent` call each, so every prompt is copied whole. A system prompt is never filled in: there a value the \
instructions read at run time (`{name}` in a Python f-string, `${name}` in TypeScript) becomes the words "given in \
the message", and the step's prompt passes it as the plan's `{state.key}`. The tool names in the code (`file_search`, \
`mcp`, …) are its own variables, never tools here: give an agent only the tools the plan names. Never copy code from \
here into `transform_code`, a tool or a skill, and never write a credential: `‹credential removed›` marks one that \
was taken out.

"""


AGENTS_SDK_GUIDE = """\
# The message is pasted OpenAI Agents SDK code
The person is moving a workflow here, usually from OpenAI Agent Builder (its Code → Agents SDK export, Python or \
TypeScript). This is a CREATE: build the workflow the code describes. The code is never run, and nothing in it is a \
tool here. Read it for what each agent does and how the workflow moves between them, then plan it with the steps \
below. Ask a question only if the code leaves something genuinely undecided.

## Five rules first
1. **Tools are only what this workspace has.** The code's tool variables (`file_search`, `mcp`, `web_search_preview`, \
`FileSearchTool(...)`, `HostedMCPTool(...)`) are NOT tool names here. An agent's `tools` lists only names \
`list_tool_servers` / `list_server_tools` showed you. Functions the code defines as tools (`@function_tool`, \
`function_tool(...)`, `tool({...})`) are code: never a tool name here, never a `tool` step's `forced_tool`. If nothing \
fits, the agent gets `tools: []` and the tool goes under Not carried over.
2. **A condition reads JSON, never prose.** A model step whose answer a later `if_else`, `switch` or `loop` reads is \
followed by an `extract_json` step into a `dict` key, and the condition reads that key (`state.<dict key>.<field>`).
3. **Instructions are copied word for word.** Each new agent block says `instructions: from the export's "<Agent \
name>"`, and so does each llm or evaluator step that replaces an agent. A system prompt is never filled in: there a \
value the instructions read at run time (`{name}`, `${name}`) becomes the words "given in the message", and the step's \
prompt passes it as `{state.key}`.
4. **A new agent only for an Agent with tools that exist here.** An Agent without them is an `llm` step; a classifier \
is the `evaluator` below. Neither becomes an agent. Handoffs are ONE `delegate` agent that picks among the agents it \
hands to — never a chain of their steps.
5. **Everything left behind is listed** under `## Not carried over` (below).

## What each construct becomes
- An Agent WITH tools that exist here → an `agent` step on a NEW agent. Leave out `model`, `model_settings` and \
reasoning settings: this workspace's model runs every step.
- An Agent WITHOUT tools → an `llm` step; its instructions are the step's prompt.
- A classifier (an Agent whose `output_type` has one category field) followed by if/elif/else on that category → ONE \
`evaluator` step: the categories are its route labels, its `evaluator_prompt` is the classifier's instructions, and \
each route goes to the step that branch ran. The routes ARE the branches: no classifier agent, no llm step and \
no if_else after it.
- if/else on anything else (a counter, a flag, a person's answer, a field of an earlier JSON result) → `if_else` \
(`if_condition`, `if_true_step_id`, `if_false_step_id`), or `switch` (`switch_expression`, `switch_cases_json` \
`'{"value":"step_id"}'`, `switch_default_step_id`) for several values. Conditions use this product's syntax: \
`state.key.field == "x"`, `and`/`or`/`not`, `len(...)`, `in`. Not CEL, not JavaScript: `&&` is `and`, `===` is `==`.
- `while` → ONE `loop` step whose `loop_step_ids` are the steps inside the while, in order, and whose \
`next_step_id` is what runs after it; never an `end` step inside a loop. `loop_over` (`state.<list key>`, a list an \
`extract_json` step wrote, with `loop_item_key` named after the variable the loop indexes) when it walks a list, \
else `loop_while_condition`. A counter is `loop_index_key`, not a step. After the \
loop, each body step's output key holds a list of every iteration's result.
- `state = {...}` and set-state → `state_schema` keys with those defaults, each written by a step's `output_key`. \
Use a `transform` step only if nothing else can, and never with code copied from the export.
- `output_type` (a Pydantic or zod schema) → the step answers as JSON with those fields, then `extract_json` (rule 2).
- User approval (`approval_request(...)` / `approvalRequest(...)`, usually a stub that returns true) → a `human` step: \
its message is the `human_prompt`, one field `{"name": "approved", "type": "boolean", "label": "Approve?"}`, then an \
`if_else` on `state.<key>.approved == True`.
- Guardrails (`guardrails_config`, `run_guardrails`, `input_guardrails=`, a tripwire) are KEPT: an `evaluator` step \
before what they guard, whose prompt asks the checks the code names, with routes `pass` and `blocked`; `blocked` goes to a `print` step (`print_content`: the export's \
failure output, as text), then `end`. Name the checks (Moderation, Jailbreak, PII, …) in its prompt.
- File search → a `retrieve` step only when Knowledge steps are offered and `list_knowledge_bases` shows a base that \
fits; otherwise Not carried over.
- A hosted MCP server (`server_url`, `server_label`) → that server's tools only if `list_tool_servers` shows the same \
server here (same URL or label); otherwise Not carried over.
- Web search → a tool only if `list_server_tools` shows one that really searches the web; otherwise Not carried over.
- `handoffs=[...]` / `agent.as_tool(...)` → one `delegate` agent with `delegate_agent_ids` naming the agents it \
hands to.
- `Runner.run(...)` order is the step order. Each step reads `{state.user_input}` and the earlier results it needs.
- `return {...}` / the last agent's output → `end`, after a `print` step (`print_content`) if it returns fixed text.

## Not carried over — always the plan's last section
After Open questions, a section `## Not carried over`: one bullet per item this code really has — named as the \
code names it (the agent, the tool, its URL or id) — and what the person does about it. Leave out what the code does \
not have; "Nothing" only when nothing was left. Check each of these:
- Each `‹credential removed›`: a credential the export held, taken out before anything read it. "Add it as a Vault \
variable, and rotate it where it came from: it was written into the code."
- Each tool the code named that is not here: a hosted MCP server (with its `server_url`: "add it in Settings → MCP \
Servers and sign in, then ask me to give <agent> its tools"), file search (with its vector store id: "upload those \
files to a knowledge base"), web search, Code Interpreter, image generation, computer use.
- Function tools (`@function_tool`, `tool({...})`): they are code, and code is never copied. Name each and what it \
did; it comes back as an HTTP tool, an MCP server or a Python tool.
- `needs_approval` on a tool: "set that tool to Ask first on the agent".
- The models it named (gpt-5, gpt-4.1-mini, …): this workspace's model runs every step. For the closest match, add an \
OpenAI key in Settings → Models.
- PII masking in a guardrail (the check is kept; rewriting the text is not), and any condition this product's \
syntax cannot say.
- ChatKit or any UI around the workflow, and its evals.

## The shape (placeholders in <angle brackets>: use the code's own names)
Code: guardrails → <classifier> (output_type with one category field) → if category == "<a>": <agent A> (its tools \
are not here) else: <agent B> (no tools).
Plan: 1. <the guardrail's name> (evaluator: pass → 2, blocked → 5) · 2. <classifier> (evaluator, the classifier's \
instructions: <a> → 3, other → 4) · 3. <agent A> (agent, NEW, its instructions, tools: [] because its tools are not \
here) → 6 · 4. <agent B> (llm, its instructions) → 6 · 5. a blocked reply (print, the code's failure output) → 6 · \
6. End. New agents: <agent A> only. Not carried over: <agent A>'s tools (their URL or id), the models, each \
‹credential removed›.

Last: whatever the code is, the plan's final section is `## Not carried over`.
"""
