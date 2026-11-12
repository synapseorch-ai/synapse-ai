"""An OpenAI Agents SDK export pasted into the builder (SW1, D253).

OpenAI shuts Agent Builder down on 2026-11-30. Its only way out is the Code
button: Python or TypeScript Agents SDK code — `Agent(...)` objects and a
`run_workflow` whose branches, loops and approvals are plain code over a
`state` dict. Nothing ever runs that code. The builder's own models read it,
the way they read any message, with a guide to what each construct becomes
here; one thing is read out of it by code, each Agent's instructions, exactly
as written (`agent_instructions`, parsed and never run: F1550), so that what
the build makes holds them whole (`verbatim`).

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


# ── Each Agent's instructions, as the code writes them (F1550) ─────────────────────────────────────────────────────

#: What a step's prompt or a new agent's instructions holds to have an Agent's instructions put there by code.
INSTRUCTIONS_OF = '‹instructions of "{name}"›'
#: The reference, as a model writes it: the name in straight or curly quotes, or none.
INSTRUCTIONS_REF = re.compile(r"‹\s*instructions\s+of\s+[\"“]?([^\"“”›]+?)[\"”]?\s*›")
#: What a value the instructions read at run time becomes (the guide's rule 3), and a key taken out (its last line).
GIVEN = "given in the message"
KEY_HELD = "the key it held"


def agent_instructions(text: str) -> dict[str, str]:
    """Each Agent's instructions in pasted Agents SDK code, by the Agent's name, exactly as the code writes them (F1550:
    the builder's model retyped them, and lost up to 70%). Python is parsed, never run: a literal, an f-string, a
    concatenation, a name bound to one or the function that returns one (Agent Builder's dynamic instructions).
    TypeScript's string and template literals are read the same ways. A value read at run time (an f-string's `{x}`, a
    template's `${x}`) is the words `GIVEN`, and a key the door took out `KEY_HELD`. `text` may carry words around the
    code; what does not read gives nothing, never an error."""
    found = _python_instructions(text) if _PY_AGENT.search(text) else {}
    if not found and _TS_AGENT.search(text):
        found = _ts_instructions(text)
    return {name: words.replace(CREDENTIAL_MARK, KEY_HELD) for name, words in found.items()}


def _python_tree(text: str):
    """The module the code is, from its first import to the last line that parses (words may follow it)."""
    import ast

    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"(?:from|import)\s", line)), None)
    if start is None:
        return None
    end = len(lines)
    for _ in range(8):
        try:
            return ast.parse("\n".join(lines[start:end]))
        except SyntaxError as e:
            if not e.lineno or e.lineno <= 1 or start + e.lineno - 1 >= end:
                return None
            end = start + e.lineno - 1
        except (ValueError, RecursionError, MemoryError):
            return None
    return None


def _python_instructions(text: str) -> dict[str, str]:
    import ast

    tree = _python_tree(text)
    if tree is None:
        return {}
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    bound = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}

    def words(node, depth: int = 0) -> str | None:
        if node is None or depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return "".join(str(p.value) if isinstance(p, ast.Constant) else GIVEN for p in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = words(node.left, depth + 1), words(node.right, depth + 1)
            return left + right if left is not None and right is not None else None
        if isinstance(node, ast.Name) and node.id in funcs:
            return next((w for r in ast.walk(funcs[node.id]) if isinstance(r, ast.Return)
                         if (w := words(r.value, depth + 1)) is not None), None)
        if isinstance(node, ast.Name) and node.id in bound:
            return words(bound[node.id], depth + 1)
        return None

    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "Agent":
            given = {k.arg: k.value for k in node.keywords if k.arg}
            name, said = words(given.get("name")), words(given.get("instructions"))
            if name and said is not None:
                out.setdefault(name, said)
    return out


_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_literal(src: str, i: int) -> tuple[str | None, int]:
    """The JavaScript string or template literal opening at `src[i]`, as its value, and the index after it. A
    template's `${…}` is `GIVEN`. (None, i) when there is none there."""
    quote = src[i:i + 1]
    if quote not in ("'", '"', "`"):
        return None, i
    out: list[str] = []
    j = i + 1
    while j < len(src):
        c = src[j]
        if c == "\\" and j + 1 < len(src):
            n = src[j + 1]
            try:
                if n == "u" and src[j + 2:j + 3] == "{":
                    end = src.index("}", j)
                    out.append(chr(int(src[j + 3:end], 16)))
                    j = end + 1
                    continue
                if n == "u":
                    out.append(chr(int(src[j + 2:j + 6], 16)))
                    j += 6
                    continue
                if n == "x":
                    out.append(chr(int(src[j + 2:j + 4], 16)))
                    j += 4
                    continue
            except ValueError:
                return None, i
            if n == "\r" and src[j + 2:j + 3] == "\n":
                j += 3
                continue
            if n not in ("\n", "\r", " ", " "):
                out.append(_JS_ESCAPES.get(n, n))
            j += 2
            continue
        if c == quote:
            return "".join(out), j + 1
        if quote == "`" and src.startswith("${", j):
            depth, j = 1, j + 2
            while j < len(src) and depth:
                if src[j] in "'\"`":
                    _, after = _js_literal(src, j)
                    j = after if after > j else j + 1
                    continue
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                j += 1
            out.append(GIVEN)
            continue
        if quote != "`" and c == "\n":
            return None, i
        out.append(c)
        j += 1
    return None, i


def _js_value(src: str, i: int, defs: dict[str, int], depth: int = 0) -> str | None:
    """The text the expression at `src[i]` comes to: a literal, a name bound to one, or a function returning one."""
    while i < len(src) and src[i].isspace():
        i += 1
    said, _ = _js_literal(src, i)
    if said is not None or depth > 4:
        return said
    name = re.match(r"[A-Za-z_$][\w$]*", src[i:])
    if name and name.group() not in ("function", "async") and \
            not src[i + name.end():].lstrip().startswith(("(", "=>")):
        at = defs.get(name.group())
        return _js_value(src, at, defs, depth + 1) if at is not None else None
    # A function: an arrow's literal body, or its body's first returned literal.
    arrow = re.match(r"(?:async\s+)?(?:function\b[^{]*|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)\s*", src[i:])
    if not arrow:
        return None
    body = i + arrow.end()
    said, _ = _js_literal(src, body)
    if said is not None:
        return said
    ret = re.compile(r"\breturn\s*").search(src, body)
    return _js_literal(src, ret.end())[0] if ret else None


def _ts_instructions(text: str) -> dict[str, str]:
    defs = {m.group(1): m.end() for m in re.finditer(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=(?![=>])\s*", text)}
    defs |= {m.group(1): m.start() for m in re.finditer(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", text)}
    starts = [m.end() for m in _TS_AGENT.finditer(text)]
    out: dict[str, str] = {}
    for k, start in enumerate(starts):
        block = text[start:starts[k + 1] if k + 1 < len(starts) else len(text)]
        name = re.search(r"\bname\s*:\s*", block)
        said = re.search(r"\binstructions\s*:\s*", block)
        if not name or not said:
            continue
        agent, _ = _js_literal(block, name.end())
        words = _js_value(text, start + said.end(), defs)
        if agent and words is not None:
            out.setdefault(agent, words)
    return out


# What the planner and the prompt writer see below the guide. The planner reads it for *what* to build; the prompt
# writer copies each agent's instructions from it. The prompts put `{{ state.export_source }}` straight after their last
# line, so it brings its own blank line and an ordinary build's prompt is unchanged.
_SOURCE_HEADING = """\


# The pasted Agents SDK code this build translates
Copy text from it, never code. Every new agent, llm step or evaluator step that stands for an Agent here (the same \
name, or the plan says so) gets that Agent's instructions (or the string its instructions function returns) word for \
word — as the new agent's instructions, or as the step's prompt — however long, and whatever the plan's purpose line \
says. Never retype them: write ‹instructions of "<Agent name>"› (its name as the code writes it) where they go, and \
code puts them there whole; write around it only what the step adds, such as the line passing what it reads. A value \
the instructions read at run time ({name} in a Python f-string, ${name} in TypeScript) becomes the words "given in \
the message", and the step's prompt passes it as {{ state.<step id> }}. The tool names in the code \
(file_search, mcp, …) are its own variables, never tools here: give an agent only the tools the plan names. Never \
copy code from here into a transform step, a tool or a skill, and never write a credential: ‹credential removed› \
marks one that was taken out: where it stands in instructions you copy, write "the key it held" instead.

"""


AGENTS_SDK_GUIDE = """\
# The message is pasted OpenAI Agents SDK code
The person is moving a workflow here, usually from OpenAI Agent Builder (its Code → Agents SDK export, Python or \
TypeScript). This is a create: plan the workflow the code describes. The code is never run, and nothing in it is a \
tool here. Read it for what each agent does and how the workflow moves between them, then plan it with the steps \
below. Ask a question only if the code leaves something genuinely undecided.

## Five rules first
1. **Tools are only what this workspace has.** The code's tool variables (file_search, mcp, web_search_preview, \
FileSearchTool(...), HostedMCPTool(...)) are not tool names here. An agent's `tools` lists only names from what this \
workspace has. Functions the code defines as tools (@function_tool, function_tool(...), tool({...})) are code: never \
a tool name here, never a `tool` step's tool. If nothing fits, the agent gets no tools and the tool goes in \
`not_carried_over`.
2. **A condition reads JSON, never prose.** A model step whose answer a later `if_else`, `switch` or `loop` reads is \
followed by an `extract_json` step, and the condition reads that step's output by its id (`state.<step id>.<field>`).
3. **Instructions are copied word for word, by code.** A new agent's `purpose` says it keeps the export's \
"<Agent name>" instructions; they are written into it as they are, after the plan is checked. A `llm` or `evaluator` \
step that replaces an Agent has as its `prompt` ‹instructions of "<Agent name>"›, never the instructions retyped: \
code puts them there whole, however long. A value the instructions read at run time ({name}, ${name}) becomes the \
words "given in the message", and the step's prompt passes it as {{ state.<step id> }}, after the reference.
4. **A new agent only for an Agent with tools that exist here.** An Agent without them is an `llm` step; a classifier \
is the `evaluator` below. Handoffs are one `team` agent whose `members` are the agents it hands to — never a chain of \
their steps.
5. **Everything left behind is listed** in `not_carried_over` (below).

## What each construct becomes
- An Agent with tools that exist here → an `agent` step on a new agent. Leave out its model and reasoning settings: \
this workspace's model runs every step.
- An Agent without tools → an `llm` step; its instructions are the step's prompt.
- A classifier (an Agent whose output_type has one category field) followed by if/elif/else on that category → one \
`evaluator` step: the categories are its `branches`, its `prompt` is the classifier's instructions, and each branch \
goes to the step that branch ran. No classifier agent, no llm step and no if_else after it.
- if/else on anything else (a counter, a flag, a person's answer, a field of an earlier JSON result) → `if_else` (its \
`condition`, and `branches` true / false), or `switch` (its `condition`, `branches` per value plus `default`). \
Conditions use this product's syntax: `state.<step id>.field == "x"`, and / or / not, len(...), in. Not CEL, not \
JavaScript: && is and, === is ==.
- while → one `loop` step whose `body` is the steps inside the while, in order, and whose `next` is what runs after \
it. `loop_over` names the step whose output is the list it walks (an `extract_json` step's id). After the loop, each \
body step's output holds a list of every iteration's result.
- state = {...} and set-state → what steps write, by their ids. A `transform` step only if nothing else can, and \
never with code copied from the export.
- output_type (a Pydantic or zod schema) → the step answers as JSON with those fields, then `extract_json` (rule 2).
- User approval (approval_request(...) / approvalRequest(...), usually a stub that returns true) → a `human` step \
whose `prompt` is its message and whose `config.human_fields` is one yes/no field, \
`[{"name": "approved", "type": "boolean", "label": "Approve?"}]` (the run asks it with Approve and Decline), then an \
`if_else` on `state.<its id>.approved`. A human step with no fields answers the person's words, which have no fields.
- Guardrails (guardrails_config, run_guardrails, input_guardrails=, a tripwire) are kept: an `evaluator` step before \
what they guard, whose prompt asks the checks the code names, with branches pass and blocked; blocked goes to a \
`print` step (its `prompt`: the export's failure output, as text), then `end`. Name the checks (Moderation, \
Jailbreak, PII, …) in its prompt.
- File search → a `retrieve` step only when Knowledge steps are offered and a knowledge base listed here fits; \
otherwise `not_carried_over`.
- A hosted MCP server (server_url, server_label) → that server's tools only if this workspace lists the same server \
(same URL or label); otherwise `not_carried_over`.
- Web search → a tool only if one listed here really searches the web; otherwise `not_carried_over`.
- handoffs=[...] / agent.as_tool(...) → one `team` agent whose `members` are the agents it hands to.
- Runner.run(...) order is the step order.
- return {...} / the last agent's output → `end`, after a `print` step if it returns fixed text.

## not_carried_over — always filled in from a pasted program
One line per item this code really has — named as the code names it (the agent, the tool, its URL or id) — and what \
the person does about it. Leave out what the code does not have. Check each of these:
- Each credential the export held, taken out before anything read it (‹credential removed› marks it in the code): \
name it as "the key it held" (never the mark itself). "Add it as a Vault variable, and rotate it where it came from: \
it was written into the code."
- Each tool the code named that is not here: a hosted MCP server (with its server_url: "add it under Apps and MCP \
servers and sign in, then ask me to give <agent> its tools"), file search (with its vector store id: "upload those \
files to a knowledge base"), web search, Code Interpreter, image generation, computer use.
- Function tools (@function_tool, tool({...})): they are code, and code is never copied. Name each and what it did; it \
comes back as an HTTP tool, an MCP server or a Python tool.
- needs_approval on a tool: "set that tool to Ask first on the agent".
- The models it named (gpt-5, gpt-4.1-mini, …): this workspace's model runs every step. For the closest match, add an \
OpenAI key in Settings → Models.
- PII masking in a guardrail (the check is kept; rewriting the text is not), and any condition this product's syntax \
cannot say.
- ChatKit or any UI around the workflow, and its evals.

## The shape (placeholders in <angle brackets>: use the code's own names)
Code: guardrails → <classifier> (output_type with one category field) → if category == "<a>": <agent A> (its tools \
are not here) else: <agent B> (no tools).
Plan: 1. <the guardrail's name> (evaluator: pass → 2, blocked → 5) · 2. <classifier> (evaluator, the classifier's \
instructions: <a> → 3, other → 4) · 3. <agent A> (agent, new, keeps its instructions, no tools because its tools are \
not here) → 6 · 4. <agent B> (llm, its instructions) → 6 · 5. a blocked reply (print, the code's failure output) → 6 \
· 6. End. New agents: <agent A> only. not_carried_over: <agent A>'s tools (their URL or id), the models, each \
‹credential removed›.
"""
