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

#: What a credential is replaced with. The guide tells the builder that each
#: one is a credential the export held, to be listed under "Not carried over".
CREDENTIAL_MARK = "‹credential removed›"

#: Every Agent Builder export stamps its runs with this; hand-written SDK code
#: does not. It is how the two are told apart.
_AGENT_BUILDER_MARK = re.compile(r"""["']?__trace_source__["']?\s*:\s*["']agent-builder["']""")

_PY_IMPORT = re.compile(r"^\s*from\s+agents(?:\.[\w.]+)?\s+import\b", re.M)
_PY_AGENT = re.compile(r"\bAgent\s*\(")
_TS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*)["']@openai/agents(?:-[\w-]+)?["']""")
_TS_AGENT = re.compile(r"\bnew\s+Agent\s*\(")

#: Credential shapes, most specific first. Each pattern's last group (or the
#: whole match, when it has none) is what is replaced. Ids that are not
#: secrets — `vs_…` vector stores, `wf_…` workflows — match none of them.
_CREDENTIALS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # A JWT: what a hosted MCP server's `authorization` usually holds.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    # OpenAI's and Anthropic's keys (`sk-…`, `sk-proj-…`, `sk-ant-…`); Stripe's.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    # `Bearer <token>`: the token only. A reference — `Bearer ${process.env.X}`,
    # `Bearer {{ global.X }}` — starts with a character the class leaves out.
    re.compile(r"(?i)(\bBearer\s+)([A-Za-z0-9._~+/=-]{16,})"),
    # A quoted literal assigned to a name that says what it is. A value with a
    # space in it is prose or a placeholder, not a key; so is one already marked.
    re.compile(
        r"""(?i)(\b[\w-]*(?:api[_-]?key|secret|token|password|passwd|authorization)["']?\s*[:=]\s*)"""
        r"""(["'])([^"'\s]{12,})\2"""
    ),
)


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


def scrub_credentials(text: str) -> tuple[str, int]:
    """`text` with each credential replaced by `CREDENTIAL_MARK`, and how many."""
    removed = 0

    def _mark(match: re.Match[str]) -> str:
        nonlocal removed
        removed += 1
        groups = match.groups()
        if not groups:
            return CREDENTIAL_MARK
        if len(groups) == 2:  # (prefix, secret)
            return f"{groups[0]}{CREDENTIAL_MARK}"
        prefix, quote, _ = groups  # (prefix, quote, secret)
        return f"{prefix}{quote}{CREDENTIAL_MARK}{quote}"

    for pattern in _CREDENTIALS:
        text = pattern.sub(_mark, text)
    return text, removed


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
Copy text from it, never code. Where the plan says a role's instructions come from the export, that agent's \
`instructions` (or the string its instructions function returns) are copied word for word — into the new agent's \
system prompt, or into an llm or evaluator step's prompt. A value the instructions read at run time (`{name}` in a \
Python f-string, `${name}` in TypeScript) becomes the plan's `{state.key}` in the step's prompt. Never copy code from \
here into `transform_code`, a tool or a skill, and never write a credential: `‹credential removed›` marks one that \
was taken out.

"""


AGENTS_SDK_GUIDE = """\
# The message is pasted OpenAI Agents SDK code
The person is moving a workflow here, usually from OpenAI Agent Builder (its Code → Agents SDK export, Python or \
TypeScript). This is a CREATE: build the workflow that code describes. The code is never run, and nothing in it is a \
tool you can call. Read it for what each agent does and how the workflow moves between them, then plan it with the \
steps below. Ask a question only if the code leaves something genuinely undecided.

## What each construct becomes
- `Agent(...)` / `new Agent({...})` WITH tools → an `agent` step on a NEW agent. Its instructions are copied word for \
word (say so: "instructions: from the export's <Agent name>"). Leave out `model`, `model_settings` and reasoning \
settings: they run on this workspace's model.
- An Agent WITHOUT tools → an `llm` step, not a new agent; its instructions are the step's prompt, word for word.
- A classifier agent (an `output_type` with one category field) followed by if/elif/else on that category → ONE \
`evaluator` step: the categories are its route labels, the classifier's instructions its prompt.
- if/else on anything else (a counter, a flag, a person's answer, a field of an earlier result) → `if_else` \
(`if_condition`, `if_true_step_id`, `if_false_step_id`), or `switch` (`switch_expression`, `switch_cases_json` \
`'{"value":"step_id"}'`, `switch_default_step_id`) for several values. Rewrite the condition in this product's \
expression syntax: `state.key.field == "x"`, `and`/`or`/`not`, `len(...)`, `in`. Not CEL, not JavaScript: `&&` is \
`and`, `===` is `==`, `output_parsed["intent"]` is `state.<key>.intent`.
- `while` → a `loop` step: `loop_over` (`state.<list key>`, with `loop_item_key` named after the variable the loop \
indexes) when it walks a list, else `loop_while_condition`. A counter the loop keeps is `loop_index_key`, not a \
step. Each body step's output becomes a list of every iteration's result after the loop.
- `state = {...}` and set-state → `state_schema` keys with those defaults, written by a step's `output_key`. Use a \
`transform` step only if nothing else can, and never with code copied from the export.
- `output_type` (a Pydantic or zod schema) → tell the step to answer as JSON with those fields, then an \
`extract_json` step into a `dict` key that later conditions read (`state.<key>.<field>`).
- User approval (`approval_request(...)` / `approvalRequest(...)`, often a stub returning true) → a `human` step: its \
message is the `human_prompt`, one field `{"name": "approved", "type": "boolean", "label": "Approve?"}`, then an \
`if_else` on `state.<key>.approved == True`.
- Guardrails (`guardrails_config`, `run_guardrails`, `input_guardrails=`, a tripwire) → an `evaluator` step before \
what they guard, with routes `pass` and `blocked`; `blocked` goes to a `print` step (`print_content`: the export's \
failure output, as text), then `end`. Name the checks it ran (Moderation, Jailbreak, PII, …) in the evaluator's prompt.
- `FileSearchTool` / `fileSearchTool` → a `retrieve` step, only when Knowledge steps are offered and \
`list_knowledge_bases` shows a base that fits. Otherwise it is not carried over.
- `HostedMCPTool` / `hostedMcpTool` (`server_url`, `server_label`, `allowed_tools`) → that server's tools, only if \
`list_tool_servers` shows the same server here (same URL or label). Otherwise it is not carried over.
- `WebSearchTool` / `webSearchTool` → a web-search tool only if `list_server_tools` shows one that really searches \
the web.
- `handoffs=[...]` / `agent.as_tool(...)` → one `delegate` agent with `delegate_agent_ids` naming the agents it \
hands to.
- `Runner.run(...)` order is the step order. Each Agent Builder agent reads the whole conversation so far: give each \
step `{state.user_input}` and the earlier results it needs.
- `return {...}` / the last agent's output → `end`, after a `print` step (`print_content`) if it returns fixed text.

## Not carried over — always say what was left behind
The plan MUST end with a section `## Not carried over`, after Open questions: one bullet per item, what it was and \
what the person does about it. "Nothing" only when nothing was left. Include, when the code has them:
- Each `‹credential removed›`: a credential the export held — taken out before anything read it. Add it here as a \
Vault variable, and rotate it where it came from: it was written into the code.
- A hosted MCP server not found here: its URL, and "add it in Settings → MCP Servers and sign in, then ask me to give \
<agent> its tools".
- Function tools (`@function_tool`, `tool({...})`): they are code, and code is never copied. Name each and what it \
did; it comes back as an HTTP tool, an MCP server or a Python tool.
- File search with no knowledge base here: "upload those files to a knowledge base" (the vector store's id is \
fine to show).
- Code Interpreter, image generation, computer use, a web search with no tool here, and `needs_approval` on a tool \
("set that tool to Ask first on the agent").
- The models it named (gpt-5, gpt-4.1-mini, …): this workspace's model runs every step. For the closest match, add an \
OpenAI key in Settings → Models.
- PII masking in a guardrail (the check is kept; rewriting the text is not), and any condition this product's \
expressions cannot say.
- ChatKit or any UI around the workflow, and its evals.
"""
