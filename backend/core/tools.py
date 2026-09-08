"""
Tool definitions, aggregation, and system prompt construction.
Extracted from server.py to eliminate duplication between chat() and chat_stream().
"""
import json
import time
import datetime
import zoneinfo

import anyio

from core.config import load_settings, MCP_LIST_TOOLS_TIMEOUT
from core.tool_router import SEP, ToolRouter


# Tools auto-injected per agent type. These bypass the agent's tools[] array.
# "all_types" applies to every agent regardless of type.
DEFAULT_TOOLS_BY_TYPE = {
    "all_types": {
        "update_tasks",               # native task list — see core/task_list.py
        # Filesystem MCP — always available for reading repo + vault files
        "read_file",                  # read any allowed file
        "read_multiple_files",        # batch read
        "search_files",               # recursive pattern search
        "list_directory",             # directory listing
        "get_file_info",              # file metadata
        "grep",
        "glob",
        "read_file_by_lines",         # read specific line range from any file
    },
    "code": {
        "search_codebase",
        "multi_repo_search",
        "find_similar_code",
        "list_indexed_files",
        "get_file_chunks",
    },
    "orchestrator": set(),  # orchestrator agents delegate to sub-agents; no extra tools needed
    "delegate": set(),      # delegate agents route to sub-agents via synthetic delegate_to_agent tool
}


# System Prompt for Native Tool Calling (Personal Assistant)
NATIVE_TOOL_SYSTEM_PROMPT = """You are a highly capable Personal Intelligent Assistant.
Your mission is to assist the user with everyday tasks, retrieving personal information, and utilizing provided tools to make their life easier.

### CORE OPERATING RULES
1.  **Think Step-by-Step:** Before calling a tool, briefly analyze the user's request.
2.  **Accuracy First:** Never guess IDs. Always use `list_` or `search_` tools to find the real ID first.
3.  **Privacy and Security:** You are operating in a personal environment. Handle personal data with care.

### RESPONSE STYLE
*   **Friendly & Helpful:** Be conversational but concise.
*   **Action-Oriented:** If a task is done, let the user know. If data is retrieved, present it clearly.
"""


class VirtualTool:
    """A lightweight tool descriptor that mimics the shape of an MCP tool."""
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


#: Servers whose tools `"all"` does not cover — an agent has to name one of them
#: to get any of them.
#:
#: `"all"` means "whatever this deployment makes available", and that is the
#: right default for a tool set that reads a PDF or fetches a page: the worst
#: case of a wrong guess is a wasted call. `sql` is different in kind. It opens a
#: connection to a system somebody configured separately, and the reason it was
#: configured separately is that somebody decided which agents should reach it.
#: An agent created by ticking nothing should not arrive holding that.
#:
#: A floor rather than a policy: naming the tool still turns it on, and a
#: deployment that wants finer control layers it on top. The set is expected to
#: grow, and the test for joining it is whether a surprised owner would call the
#: call a breach rather than a waste.
OPT_IN_SERVERS: frozenset[str] = frozenset({"sql"})


def permits(allowed_tools, name: str, *, opt_in: bool = False) -> bool:
    """Whether an agent whose list is `allowed_tools` may call `name`.

    One function rather than the six `"all" in allowed_tools or …` sites it
    replaces. Those were identical by convention, and a rule applied in five
    places and forgotten in the sixth is exactly what this exists to prevent.

    `opt_in` marks a tool from a server in `OPT_IN_SERVERS`, which `"all"`
    deliberately does not reach. Naming it explicitly always works — that is
    what opting in is.
    """
    if name in allowed_tools:
        return True
    return "all" in allowed_tools and not opt_in


def build_virtual_tools():
    return []


_EMBED_TOOLS = frozenset({"search_codebase", "multi_repo_search", "find_similar_code",
                          "list_indexed_files", "get_file_chunks"})


def effective_allowed_tools(active_agent: dict) -> list[str]:
    """The agent's tool allowlist with the type defaults added and the embedding
    tools removed when embedding is off.

    Always a new list. This used to be `active_agent["tools"]` itself with the
    defaults appended in place, so every aggregation grew the agent's own list
    by the default set — harmless the first time and wrong every time after.
    """
    allowed = list(active_agent.get("tools", ["all"]))

    # Auto-inject default tools based on agent type.
    # Agents with `skip_default_tools: true` opt out entirely — their tool list
    # is exactly what they declared. Used by tightly scoped agents (e.g. builder
    # savers) where extra tools add Gemini function-selection noise.
    if not active_agent.get("skip_default_tools", False):
        agent_type = active_agent.get("type", "conversational")
        for category in ["all_types", agent_type]:
            for tool_name in DEFAULT_TOOLS_BY_TYPE.get(category, set()):
                if tool_name not in allowed:
                    allowed.append(tool_name)

    if not load_settings().get("embed_code", False):
        allowed = [t for t in allowed if t not in _EMBED_TOOLS]
    return allowed


async def _session_tools_for(server_module, session_key: str):
    """A session's advertised tools, from the per-module cache or one bounded
    `list_tools()`. None when the session is unknown or does not answer."""
    sessions = getattr(server_module, "agent_sessions", None) or {}
    session = sessions.get(session_key)
    cache = getattr(server_module, "_session_tools", None)
    if cache is None:
        cache = {}
        try:
            server_module._session_tools = cache
        except (AttributeError, TypeError):
            pass
    if session_key in cache:
        return cache[session_key]
    if session is None:
        return None
    try:
        with anyio.fail_after(MCP_LIST_TOOLS_TIMEOUT):
            result = await session.list_tools()
    except Exception as e:  # noqa: BLE001 — a session that does not answer has no tools
        print(f"DEBUG: Skipping session '{session_key}' — list_tools failed: {e}", flush=True)
        return None
    cache[session_key] = result.tools
    return result.tools


async def resolve_one_tool(server_module, name: str, active_agent: dict, custom_tools_list: list[dict]):
    """One tool's declaration by name, or None.

    The forced-tool step used to aggregate every server's tools — list, filter,
    schema-map, serialise — to find the one it was told to call. The router
    already knows which session answers a name, and that session's tool list is
    cached; the rest of the sources are small lists. Same admission rules as
    `aggregate_all_tools`, so a tool this returns is one the agent could call.
    """
    from core.tool_router import ToolRouter

    allowed = effective_allowed_tools(active_agent)
    router = getattr(server_module, "tool_router", None)
    if not isinstance(router, ToolRouter):
        router = ToolRouter(router or {})

    key = router.resolve(name)
    if key is not None:
        session_key, actual = router[key]
        for t in (await _session_tools_for(server_module, session_key)) or []:
            if t.name != actual:
                continue
            opt_in = session_key in OPT_IN_SERVERS
            if key in allowed or permits(allowed, name, opt_in=opt_in):
                return VirtualTool(name, t.description, t.inputSchema)
            return None

    for vt in build_virtual_tools():
        if vt.name == name:
            return vt

    group_ids = {entry[len("group:"):] for entry in allowed
                 if isinstance(entry, str) and entry.startswith("group:")}
    for ct in custom_tools_list or []:
        if ct.get("name") != name:
            continue
        if permits(allowed, name) or (group_ids and ct.get("group") in group_ids):
            return VirtualTool(ct["name"], ct.get("description", ""), ct.get("inputSchema") or {"type": "object"})
        return None

    from core.builder_tools import BUILDER_TOOL_SCHEMAS
    for bt in BUILDER_TOOL_SCHEMAS:
        fn = bt["function"]
        if fn["name"] == name and permits(allowed, name):
            return VirtualTool(name, fn.get("description", ""), fn.get("parameters", {"type": "object"}))
    return None


async def aggregate_all_tools(server_module, active_agent, custom_tools_list):
    """
    Aggregate all available tools: MCP tools + virtual tools + custom tools.

    Takes the server module rather than its `agent_sessions`, because the
    sessions are not the only thing here that belongs to it: so do the tool
    router that decides which of them services a call, and the cache of what each
    one advertises. Reading those from `core.server` while the sessions came from
    somewhere else is what made this function wrong inside a worker.

    Returns:
        tuple: (all_tools, tool_schema_map, ollama_tools, tools_json)
    """
    all_tools = []
    tool_schema_map = {}  # name -> inputSchema

    agent_sessions = getattr(server_module, "agent_sessions", None) or {}

    # tool_router is the single source of truth for dispatch (name -> session).
    # A tool name can be exposed by multiple native MCP servers (e.g.
    # read_file_by_lines in both file_reader and code_search); we use this map
    # to declare only the copy from the server that will actually service the
    # call.
    #
    # It comes off `server_module`, not `core.server`. It used to be imported
    # from that module directly, which meant a worker — holding a
    # `WorkerServerModule` with its own populated router — read the API server's
    # empty module global instead, and the collision handling below quietly did
    # nothing there.
    tool_router = getattr(server_module, "tool_router", None)
    # Test doubles and embedders hand over a plain dict; wrapping gives it the
    # alias table without making every caller construct one.
    if not isinstance(tool_router, ToolRouter):
        tool_router = ToolRouter(tool_router or {})

    # Tools each session advertised, cached after the first successful
    # `list_tools()` — this avoids re-querying flaky sessions (mcp-remote after
    # an OAuth state change, say) on every agent call. Keyed by session name, and
    # therefore only safe on the object that owns those sessions: as a module
    # global it was shared by every tenant a process served, so two tenants with
    # a server of the same name saw one tool list, whichever answered first.
    session_tools_cache = getattr(server_module, "_session_tools", None)
    if session_tools_cache is None:
        session_tools_cache = {}
        try:
            server_module._session_tools = session_tools_cache
        except (AttributeError, TypeError):
            pass  # a read-only stand-in still works, just uncached

    allowed_tools = effective_allowed_tools(active_agent)

    # Standard MCP Tools
    for session_name, session in agent_sessions.items():
        # Use cached tools from previous successful call — avoids hanging on flaky sessions
        if session_name in session_tools_cache:
            session_tools = session_tools_cache[session_name]
            print(f"DEBUG: 📦 Using cached tools for '{session_name}' ({len(session_tools)} tools)", flush=True)
        else:
            try:
                # Bounded list_tools() — timeout (default 15s, override via
                # SYNAPSE_MCP_LIST_TOOLS_TIMEOUT) prevents a single flaky
                # session from blocking all subsequent tool aggregation.
                print(f"DEBUG: 🔄 Fetching tools for '{session_name}'...", flush=True)
                with anyio.fail_after(MCP_LIST_TOOLS_TIMEOUT):
                    result = await session.list_tools()
                session_tools = result.tools
                session_tools_cache[session_name] = session_tools
                print(f"DEBUG: ✅ Fetched+cached tools for '{session_name}' ({len(session_tools)} tools)", flush=True)
            except TimeoutError:
                print(f"DEBUG: ⏱ Skipping session '{session_name}' — list_tools timed out after 15s", flush=True)
                continue
            except Exception as e:
                print(f"DEBUG: Skipping session '{session_name}' — list_tools failed: {e}", flush=True)
                continue

        is_external = session_name.startswith("ext_mcp_")
        # Whether `"all"` reaches this server's tools at all. Decided per
        # session rather than per tool, because it is a property of what the
        # server connects to, not of any one call.
        opt_in = session_name in OPT_IN_SERVERS

        for t in session_tools:
            key = tool_router.key_for(session_name, t.name)

            if is_external:
                # External servers have always been declared to the model as
                # `{server}__{tool}`, and that is what an agent's stored tools[]
                # array holds. Unchanged deliberately: renaming them would make
                # every existing agent's list silently stop matching.
                name = key or f"{session_name[len('ext_mcp_'):]}{SEP}{t.name}"
                if permits(allowed_tools, name, opt_in=opt_in):
                    all_tools.append(VirtualTool(name, t.description, t.inputSchema))
                continue

            # A native tool an agent asked for *by its namespaced key*. This is
            # the escape hatch that makes the loser of a collision reachable —
            # e.g. code_vault_search__read_file_by_lines, whose repo-aware
            # implementation file_reader has always shadowed.
            if key is not None and key in allowed_tools:
                all_tools.append(VirtualTool(key, t.description, t.inputSchema))
                continue

            # Otherwise a native tool is declared under its bare name, but only
            # by the server a bare call actually reaches, so the LLM sees the
            # schema of the implementation that runs — and Gemini never receives
            # a duplicate function declaration.
            if permits(allowed_tools, t.name, opt_in=opt_in):
                if key is not None and not tool_router.declares(session_name, t.name):
                    continue
                all_tools.append(t)

    # Populate schema map for MCP tools
    for t in all_tools:
        tool_schema_map[t.name] = t.inputSchema

    # Virtual infrastructure tools (filtered by agent type)
    virtual_tools = build_virtual_tools()
    for vt in virtual_tools:
        all_tools.append(vt)
        tool_schema_map[vt.name] = vt.inputSchema
    
    # Dynamic Custom Tools (n8n/Webhook)
    #
    # An agent's list may name a whole group — `group:<id>` — and every tool
    # carrying that group is admitted, including one added to the group after
    # the agent was saved. That is what makes a group a unit rather than a
    # picker heading (the OpenAPI import makes one group per import).
    group_ids = {entry[len("group:"):] for entry in allowed_tools if isinstance(entry, str) and entry.startswith("group:")}
    for ct in custom_tools_list:
        in_group = bool(group_ids) and ct.get("group") in group_ids
        if permits(allowed_tools, ct['name']) or in_group:
            vt = VirtualTool(ct['name'], ct['description'], ct['inputSchema'])
            all_tools.append(vt)
            tool_schema_map[vt.name] = vt.inputSchema

    # Native Builder tools — first-class so any agent that declares them in its
    # `tools` list can call create_orchestration / list_agents / etc. Dispatch
    # to execute_builder_tool is handled in react_engine and ToolStepExecutor.
    from core.builder_tools import BUILDER_TOOL_SCHEMAS
    for bt in BUILDER_TOOL_SCHEMAS:
        fn = bt["function"]
        bt_name = fn["name"]
        if permits(allowed_tools, bt_name):
            vt = VirtualTool(bt_name, fn.get("description", ""), fn.get("parameters", {"type": "object"}))
            all_tools.append(vt)
            tool_schema_map[vt.name] = vt.inputSchema

    # Safety net: guarantee at most one declaration per tool name across ALL
    # sources (MCP + virtual + custom + builder) before sending to the LLM.
    # Gemini rejects duplicate function declarations outright; the tool_router
    # gate above handles MCP-vs-MCP collisions, this catches cross-source ones.
    seen_names = set()
    deduped = []
    for t in all_tools:
        if t.name in seen_names:
            print(f"DEBUG: ⚠️ Skipping duplicate tool declaration '{t.name}'", flush=True)
            continue
        seen_names.add(t.name)
        deduped.append(t)
    all_tools = deduped

    # Build Ollama-formatted tools list
    ollama_tools = [
        {
            'type': 'function',
            'function': {
                'name': t.name,
                'description': t.description,
                'parameters': t.inputSchema
            }
        }
        for t in all_tools
    ]
    
    # String version for cloud models (system prompt injection)
    tools_json = str([
        {'tool': t.name, 'description': t.description, 'schema': t.inputSchema}
        for t in all_tools
    ])

    # Debug: Log exactly which tools will be visible to the LLM
    tool_names_for_llm = [t.name for t in all_tools]
    print(f"DEBUG: 🔧 Tools sent to LLM ({len(tool_names_for_llm)}): {tool_names_for_llm}")

    return all_tools, tool_schema_map, ollama_tools, tools_json


def build_system_prompt(agent_system_template, tools_json, session_id, session_state_getter, memory_store, agent_id=None, turns_remaining=None, max_turns=None, inject_tools=True):
    """The STABLE system prompt: identical on every turn of a step.

    Identity, reasoning protocol, tools (for providers without native tool
    calling), capabilities, example, workspace context — nothing that changes
    per turn. What used to follow a ``\n---\n`` separator (date/time, turn
    budget, RAG context) is built by `build_volatile_tail` and sent at the end
    of the user turn instead. Splitting one string on a separator was the
    previous design; it broke the moment a linked DB or repo — or a user's own
    markdown rule — put ``---`` inside the stable text, which is why the two
    now never share a string.

    `turns_remaining` and `max_turns` are accepted for callers that still pass
    them and ignored; `memory_store` and `agent_id` were never read.
    """
    # Determine if code embedding is enabled (for conditional tool description)
    _embed_code = load_settings().get("embed_code", False)
    _embed_tools_desc = (
        "- **`search_codebase`** - semantic search within specific repos. Requires `repo_ids`."
        " Add `file_filter` (e.g. `.py`, `components`) to narrow results.\n"
        "- **`multi_repo_search`** - like search_codebase but `repo_ids` is optional;"
        " omit to search ALL indexed repos at once.\n"
        "- **`find_similar_code`** - pass a code snippet to find similar patterns across repos"
        " (vs. natural language in search_codebase).\n"
        "- **`list_indexed_files`** - list every file in the embedding index with chunk counts."
        " Use before searching to understand coverage.\n"
        "- **`get_file_chunks`** - retrieve all indexed chunks for a specific file"
        " (the semantic outline). Use after finding a relevant file."
    ) if _embed_code else ""

    # ── Resolve Google Workspace email (used later in the stable section) ──
    # A plain setting, read off the synchronous snapshot. This function is sync
    # and on the per-turn path, and all it has ever wanted from the Google
    # credentials is the address to tell the model to pass to the Workspace
    # tools — so the account email is stored as its own non-secret setting and
    # the refresh token stays out of the widely-read settings dict entirely.
    google_email = None
    try:
        from services.google import EMAIL_SETTING

        google_email = load_settings().get(EMAIL_SETTING) or None
    except Exception as e:
        print(f"DEBUG: Error reading Google Workspace email: {e}")

    # ── Tools section (only for providers without native tool calling) ──
    if inject_tools:
        tools_section = f"""
### TOOLS
You have access to the following tools:
{tools_json}

**CODE & FILE NAVIGATION:**
{_embed_tools_desc}
- **`grep`** — search for a pattern inside a file or across all files in a folder. Pass a file path to search that file, or a folder path to search all files within it. Use `file_pattern` to filter by extension (e.g. `*.py`, `*.ts`).
- **`glob`** — discover file paths by pattern (e.g. `**/*.py`, `src/**/*.ts`).
- **`read_file`** — read an entire file. Use when you already know the path and the file is small. For large files, prefer `read_file_by_lines` or `grep`.
- **`read_file_by_lines`** — read a specific line range from a file (1-indexed, inclusive). Ideal when you know approximately where the relevant content is.

If you already know the file path, read it directly — no need to search first. Prefer `grep` or `read_file_by_lines` over reading entire files when you only need a specific section.

### RESPONSE FORMAT INSTRUCTIONS
If you need to use a specific tool from the list above, you MUST respond with **ONLY** a valid JSON object in the following format:
{{ "tool": "tool_name", "arguments": {{ "key": "value" }} }}

Do NOT output any other text or markdown when calling a tool.
If you do not need to use a tool, reply in plain text.
"""
    else:
        tools_section = ""

    google_block = ""
    if google_email:
        google_block = (
            f"\n### GOOGLE WORKSPACE CONTEXT\n"
            f"You are authenticated with Google Workspace as **{google_email}**. "
            f"Whenever a tool requires `user_google_email` or similar, ALWAYS use {google_email}.\n"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # STABLE SECTION — identical across every turn of the same conversation,
    # so Anthropic prompt caching and Gemini implicit caching can hit on it.
    # Order: identity → reasoning protocol → tools → capabilities → example
    #        → semi-stable workspace context.
    # ──────────────────────────────────────────────────────────────────────────
    stable_section = (
        agent_system_template.strip()
        + """

### REASONING & ACTION FORMAT
You operate in a Reason–Act loop. Each turn produces ONE of:
  (a) A tool call **preceded by a `[REASONING]...[/REASONING]` block** explaining why you are making that specific call. The reasoning block is REQUIRED before every tool call — it is what the user sees as your "thinking" panel, and it is what lets you (next turn) recover the intent behind the call.
  (b) A final response in plain markdown for the user (no `[REASONING]` block, no JSON).

The `[REASONING]` block is private scratch space — it is stripped from your final response and from the context you see next turn, but it is surfaced live to the user as a "thinking" panel and recorded in the run log.

What to put in `[REASONING]`:
- WHY you are calling this tool right now (what you concluded from the previous observation).
- WHAT you expect the tool to return.
- HOW the result will move you toward the user's goal.
2–4 sentences is plenty. Do not restate the user's question; do not narrate obvious facts.

Example tool-calling turn:
[REASONING]
The previous result gave a high-level overview but not the specific field the user asked for. I need to drill into the most relevant source from those results — that should yield the exact value, after which I can answer directly.
[/REASONING]
{"tool": "<tool_name>", "arguments": {"<key>": "<value>"}}

Example final-response turn (after gathering enough info):
Here is the requested information, with the key points the user asked about and any caveats worth flagging.

**If your tool calls are made through a native function-calling interface** — a
structured call rather than JSON you write into your reply — you cannot wrap it
in `[REASONING]` markers, and you do not need to. Put the same two or three
sentences in the **text of the same reply, alongside the call**. The markers are
optional there; the sentences are not. A reply that is a bare function call with
no text leaves the user watching a spinner with nothing to read, which is the
one outcome this section exists to prevent.

Rules:
- Emit a `[REASONING]` block before EVERY tool call. Skipping it produces an empty "thinking" panel for the user.
- Never wrap a final response in `[REASONING]` blocks.
- Never put commentary between the closing `[/REASONING]` and the JSON tool call.
- One tool call per turn unless you genuinely need multiple independent calls (then emit them sequentially in one response, no prose between).
- Say WHY, not WHAT. "Searching DuckDuckGo for India GDP 2025" restates the call the user can already see; "the official statistics office release is the primary source, so I will find it before quoting a figure" is the part only you know.
- Never describe your tool call in prose *instead of* making it, and never repeat the call's JSON back as commentary — the user sees the call itself.
"""
        + tools_section
        + """
**VAULT (LARGE OUTPUT HANDLING):**
When a tool's output exceeds the character limit, it is automatically saved to a vault file. You will receive a JSON reference containing the vault file path, file type, size, and total line count — instead of the raw output. To access the data: use `grep` to search for specific values within the vault file, or use `read_file_by_lines` with `start_line`/`end_line` to read a slice. Do NOT use `read_file` on vault files — they are too large and will be re-vaulted.

**TASK LIST (`update_tasks`):**
Your task list is shown to the user live — it is how they watch you work. For any multi-step job (3+ distinct actions), call `update_tasks` FIRST to write the plan, then keep it true: exactly one task `in_progress` at a time, mark a task `completed` the moment it is done (never batch updates), and mark a task `cancelled` if it turns out not to apply. Every call must contain the FULL list — it replaces the previous one. Skip the task list for trivial single-step requests. You will not be able to finish while tasks are still open: complete them or cancel them before giving your final response. `update_tasks` only records the plan — it cannot fetch data or act; make progress with real tools between updates.

### LINKS & REFERENCES
Whenever a tool returns URLs, source links, documentation references, or any other hyperlinks — **always include them in your response**. Present them clearly so the user can visit them directly. If you know of relevant official documentation, articles, or resources that would help the user, proactively include those links even if not explicitly returned by a tool.

### EXAMPLE INTERACTION
User: <a question that requires looking something up>

Turn 1 (yours):
[REASONING]
I don't have this in context. I'll use the most relevant available tool with the narrowest arguments that satisfy the question, so the result is precise rather than a broad dump.
[/REASONING]
{"tool": "<tool_name>", "arguments": {"<key>": "<value>"}}

Tool result: <some structured payload>

Turn 2 (yours):
A direct answer in plain markdown, citing the specific value(s) from the tool result.
"""
        + google_block
    )

    return stable_section


def build_volatile_tail(session_id, session_state_getter, turns_remaining=None, max_turns=None) -> str:
    """Everything the model must see fresh each turn: current date/time, the
    turn budget, and any active RAG context.

    Sent at the END of the user turn rather than in the system prompt. Every
    provider's prompt cache is a prefix cache — tools, then system, then the
    conversation — so per-turn text anywhere before the transcript invalidates
    the transcript's cache entry on every call. Placed last, it is the only
    thing that misses.
    """
    now = datetime.datetime.now(zoneinfo.ZoneInfo("UTC"))
    current_date = now.strftime("%B %d, %Y")
    current_time = now.strftime("%I:%M %p")
    timezone = "UTC"

    # ── Resolve RAG context (volatile) ──
    rag_context_block = ""
    try:
        ss = session_state_getter(session_id)
        last_report = ss.get("last_report_context")
        if last_report and (time.time() - last_report.get("timestamp", 0) < 600):
            rag_context_block = f"""
### ACTIVE RAG CONTEXT (AUTOMATICALLY INJECTED)
You have {last_report.get('row_count', 'some')} items of '{last_report.get('type', 'data')}' embedded in memory (generated {int(time.time() - last_report.get('timestamp', 0))}s ago).

**HOW TO ANSWER QUESTIONS ABOUT THIS DATA:**
1. **AGGREGATION QUESTIONS** (totals, averages, counts, min/max): If a SUMMARY with `numeric_aggregations` is in the tool output above, use those pre-computed values directly. They are accurate.
2. **SPECIFIC LOOKUPS** (e.g., "email from John", "meeting tomorrow", "flight details"): Call `search_embedded_report` with a descriptive query. The full data is embedded in RAG memory.
3. **PATTERN/TREND QUESTIONS** (e.g., "frequent topics", "common contacts"): Call `search_embedded_report` with the pattern description.
4. **DO NOT RE-RUN TOOL FOR EXISTING DATA:** The data is already here. Only call tools if the user explicitly asks for NEW/DIFFERENT data (e.g., "refresh", "different date", "different query").
"""
            print(f"DEBUG: 💉 Injected RAG context into system prompt")
    except Exception as e:
        print(f"DEBUG: Error resolving RAG context: {e}")

    # ── Resolve turn-budget block (volatile) ──
    turns_block = ""
    if turns_remaining is not None and max_turns is not None:
        if turns_remaining <= 0:
            turns_block = f"""
### ⚠️ TURN LIMIT REACHED — FINAL RESPONSE REQUIRED
You have used all {max_turns} available turns. You MUST stop calling tools and provide your **final answer now** based on everything gathered so far.
- Summarize what you did and what you found.
- If the task is incomplete, clearly state what could not be completed and why.
- Do NOT call any more tools.
"""
        elif turns_remaining == 1:
            turns_block = f"""
### ⚠️ LAST TURN — RESPOND NOW
This is your **final turn** (Turn {max_turns}/{max_turns}). You MUST provide your final answer now. Do NOT call any more tools.
- If you have enough information, give the complete answer.
- If you don't have enough information, say so clearly and summarize what you were able to find.
- Provide a brief summary of all steps taken so far.
"""
        else:
            turns_block = f"""
### TURN BUDGET
You have **{turns_remaining} turn(s) remaining** out of {max_turns} total.
- Plan your tool calls efficiently — prioritize the most impactful steps first.
- If you cannot complete the task within the remaining turns, provide a partial answer and summarize what was accomplished so far.
- On the last turn you MUST answer in plain text (no tool calls), even if the task is not fully complete.
"""

    tail = ""
    if rag_context_block:
        tail += rag_context_block
    tail += f"""
### CURRENT DATE & TIME CONTEXT
**Current Date:** {current_date}
**Current Time:** {current_time}
**Timezone:** {timezone}
"""
    if turns_block:
        tail += turns_block
    return tail.strip("\n")
