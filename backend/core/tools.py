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


# Tools auto-injected per agent type. These bypass the agent's tools[] array.
# "all_types" applies to every agent regardless of type.
DEFAULT_TOOLS_BY_TYPE = {
    "all_types": {
        "sequentialthinking",         # multi-step tool execution
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


# Module-level cache of MCP session tools — populated on first successful list_tools() call.
# Avoids re-querying flaky sessions (e.g. mcp-remote after OAuth state changes) on every agent call.
_session_tools_cache: dict = {}


class VirtualTool:
    """A lightweight tool descriptor that mimics the shape of an MCP tool."""
    def __init__(self, name, description, inputSchema):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema


def build_virtual_tools():
    return []


async def aggregate_all_tools(agent_sessions, active_agent, custom_tools_list):
    """
    Aggregate all available tools: MCP tools + virtual tools + custom tools.
    
    Returns:
        tuple: (all_tools, tool_schema_map, ollama_tools, tools_json)
    """
    all_tools = []
    tool_schema_map = {}  # name -> inputSchema

    # tool_router is the single source of truth for dispatch (name -> session).
    # A tool name can be exposed by multiple native MCP servers (e.g.
    # read_file_by_lines in both file_reader and code_search); we use this map
    # to declare only the copy from the server that will actually service the
    # call. Deferred import avoids a circular import at module load — aggregation
    # always runs per-request, long after startup has populated the map.
    from core import server as _server
    tool_router = getattr(_server, "tool_router", {}) or {}

    allowed_tools = active_agent.get("tools", ["all"])

    # Auto-inject default tools based on agent type.
    # Agents with `skip_default_tools: true` opt out entirely — their tool list
    # is exactly what they declared. Used by tightly scoped agents (e.g. builder
    # savers) where extra tools add Gemini function-selection noise.
    if not active_agent.get("skip_default_tools", False):
        agent_type = active_agent.get("type", "conversational")
        for category in ["all_types", agent_type]:
            for tool_name in DEFAULT_TOOLS_BY_TYPE.get(category, set()):
                if tool_name not in allowed_tools:
                    allowed_tools.append(tool_name)

    # Remove embedding tools if embed_code is disabled
    settings = load_settings()
    if not settings.get("embed_code", False):
        _embed_tools = {"search_codebase", "multi_repo_search", "find_similar_code",
                        "list_indexed_files", "get_file_chunks"}
        allowed_tools = [t for t in allowed_tools if t not in _embed_tools]

    # Standard MCP Tools
    for session_name, session in agent_sessions.items():
        # Use cached tools from previous successful call — avoids hanging on flaky sessions
        if session_name in _session_tools_cache:
            session_tools = _session_tools_cache[session_name]
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
                _session_tools_cache[session_name] = session_tools
                print(f"DEBUG: ✅ Fetched+cached tools for '{session_name}' ({len(session_tools)} tools)", flush=True)
            except TimeoutError:
                print(f"DEBUG: ⏱ Skipping session '{session_name}' — list_tools timed out after 15s", flush=True)
                continue
            except Exception as e:
                print(f"DEBUG: Skipping session '{session_name}' — list_tools failed: {e}", flush=True)
                continue

        is_external = session_name.startswith("ext_mcp_")
        server_name = session_name[len("ext_mcp_"):] if is_external else None

        if is_external:
            for t in session_tools:
                prefixed = f"{server_name}__{t.name}"
                if "all" in allowed_tools or prefixed in allowed_tools:
                    all_tools.append(VirtualTool(prefixed, t.description, t.inputSchema))
        else:
            candidate = session_tools if "all" in allowed_tools \
                else [t for t in session_tools if t.name in allowed_tools]
            for t in candidate:
                # A tool name can be exposed by multiple native MCP servers
                # (e.g. read_file_by_lines in BOTH file_reader and code_search).
                # tool_router decides which server services a call (last to
                # register at startup wins). Declare only the copy from that
                # server, so the LLM sees the schema of the impl that runs — and
                # Gemini never receives a duplicate function declaration.
                routed = tool_router.get(t.name)
                if routed and routed[0] != session_name:
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
    for ct in custom_tools_list:
        if "all" in allowed_tools or ct['name'] in allowed_tools:
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
        if "all" in allowed_tools or bt_name in allowed_tools:
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
    """
    Construct the final system prompt with tool info, date/time, session context, 
    and recent tool outputs injected.
    
    Args:
        agent_system_template: The base system prompt (tools/datetime are appended automatically)
        tools_json: String representation of available tools
        session_id: Current session ID
        session_state_getter: Function that returns session state dict for a session_id
        memory_store: Memory store instance (or None)
        agent_id: Optional agent ID for scoping memory queries
    
    Returns:
        str: The fully constructed system prompt
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

    # Get current date/time for context injection
    now = datetime.datetime.now(zoneinfo.ZoneInfo("UTC"))
    current_date = now.strftime("%B %d, %Y")
    current_time = now.strftime("%I:%M %p")
    timezone = "UTC"

    # ── Resolve Google Workspace email (used later in the stable section) ──
    google_email = None
    try:
        from core.config import TOKEN_FILE
        import os
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                token_data = json.load(f)
                google_email = token_data.get("email")
                if not google_email and token_data.get("id_token"):
                    import base64
                    id_token = token_data["id_token"]
                    payload_b64 = id_token.split(".")[1]
                    payload_b64 += "=" * (4 - len(payload_b64) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    google_email = payload.get("email")
    except Exception as e:
        print(f"DEBUG: Error reading Google Workspace email: {e}")

    # ── Resolve RAG context (volatile — appended after the separator) ──
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

    # ── Resolve turn-budget block (volatile — appended after the separator) ──
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

Rules:
- Emit a `[REASONING]` block before EVERY tool call. Skipping it produces an empty "thinking" panel for the user.
- Never wrap a final response in `[REASONING]` blocks.
- Never put commentary between the closing `[/REASONING]` and the JSON tool call.
- One tool call per turn unless you genuinely need multiple independent calls (then emit them sequentially in one response, no prose between).
"""
        + tools_section
        + """
**VAULT (LARGE OUTPUT HANDLING):**
When a tool's output exceeds the character limit, it is automatically saved to a vault file. You will receive a JSON reference containing the vault file path, file type, size, and total line count — instead of the raw output. To access the data: use `grep` to search for specific values within the vault file, or use `read_file_by_lines` with `start_line`/`end_line` to read a slice. Do NOT use `read_file` on vault files — they are too large and will be re-vaulted.

**SEQUENTIALTHINKING (OPTIONAL — USE SPARINGLY):**
`sequentialthinking` is a lightweight planning aid. If the request is complex, you MAY call it to outline your plan or refine your thinking — up to **5 times** per task. After each call you MUST make progress with real action tools (browser, search, data tool, etc.). Never call `sequentialthinking` more than 5 times per task, and never call it in place of a real tool — it cannot fetch data, browse the web, or do anything productive by itself.

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

    # ──────────────────────────────────────────────────────────────────────────
    # VOLATILE SECTION — changes every turn (datetime, turn budget) or every
    # ~10 minutes (RAG context). Placed after a separator so the cache prefix
    # boundary is unambiguous.
    # ──────────────────────────────────────────────────────────────────────────
    volatile_section = "\n---\n"
    if rag_context_block:
        volatile_section += rag_context_block
    volatile_section += f"""
### CURRENT DATE & TIME CONTEXT
**Current Date:** {current_date}
**Current Time:** {current_time}
**Timezone:** {timezone}
"""
    if turns_block:
        volatile_section += turns_block

    return stable_section + volatile_section
