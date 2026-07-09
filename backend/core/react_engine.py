"""
Shared ReAct loop engine used by both /chat and /chat/stream endpoints.
Yields structured event dicts that callers can handle differently
(collect for sync response, or stream as SSE).
"""
import asyncio
import json
import sys
import time

import httpx

from core.config import load_settings, HTTP_TOOL_TIMEOUT, MCP_TOOL_CALL_TIMEOUT
from core.vault import maybe_vault, expand_vault_mentions
from core.compaction import maybe_compact
from core.session import (
    _get_session_id, _get_session_state,
    _apply_sticky_args, _clear_session_embeddings,
    get_recent_history_messages, _get_conversation_history,
    _save_conversation_turn,
)
from core.llm_providers import generate_response as llm_generate_response
from core.tools import aggregate_all_tools, build_system_prompt, DEFAULT_TOOLS_BY_TYPE
from core.routes.agents import load_user_agents, get_active_agent_data
from core.routes.tools import load_custom_tools

import anyio as _anyio
import re as _re
from datetime import timedelta

MAX_TURNS = 30


# ──────────────────────────────────────────────────────────────────────────────
# SSE keepalive
#
# Long-running streaming endpoints (chat / orchestration) can sit silent for
# many seconds while waiting on an LLM call. A silent SSE stream looks idle to
# browsers, proxies, and OS network stacks, any of which may reset the
# connection — surfacing as "Failed to proxy ... aborted / ECONNRESET" and a
# hung UI.
#
# `iter_with_heartbeat` wraps any event async-generator and emits a periodic
# SSE comment line whenever no real event has arrived within `interval` seconds.
# The comment is ignored by EventSource and our `data:`-only client parser, but
# the bytes keep the connection alive and reset the client's stall watchdog.
# Heartbeats are yielded as the raw SSE string, so endpoints can pass them
# straight through; real events remain dicts (`isinstance(item, str)`
# distinguishes them).
# ──────────────────────────────────────────────────────────────────────────────
SSE_HEARTBEAT = ": ping\n\n"


async def iter_with_heartbeat(agen, interval: float = 10.0):
    """Yield items from `agen`, injecting `SSE_HEARTBEAT` whenever no item
    arrives within `interval` seconds.

    The underlying generator is never cancelled on a heartbeat timeout — only
    when the consumer stops early (e.g. a client disconnect closes this
    generator), in which case the pending pull is cancelled and the source is
    closed in the finally block.
    """
    ait = agen.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(ait.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                # No event within the interval — keep the stream warm.
                yield SSE_HEARTBEAT
                continue
            task = pending
            pending = None
            try:
                item = task.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            try:
                await pending
            except BaseException:
                pass
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except BaseException:
                pass


async def drain_queue_with_heartbeat(queue, sentinel, interval: float = 10.0):
    """Yield formatted SSE `data:` strings from an ``asyncio.Queue`` of event
    dicts, emitting ``SSE_HEARTBEAT`` whenever the queue stays empty for
    `interval` seconds, and returning once `sentinel` is dequeued.

    This is the queue-based counterpart to ``iter_with_heartbeat`` for the
    background-task streaming pattern (the orchestration endpoints run the
    engine in a task that pushes events into a queue while the SSE generator
    drains it). Without the heartbeat, a long LLM/compaction step leaves the
    queue empty and the stream silent — which a client stall watchdog would
    wrongly read as a dropped connection.
    """
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=interval)
        except asyncio.TimeoutError:
            yield SSE_HEARTBEAT
            continue
        if event is sentinel:
            return
        yield f"data: {json.dumps(event, default=str)}\n\n"


# ──────────────────────────────────────────────────────────────────────────────
# Reasoning blocks
#
# Models wrap their private chain-of-thought in [REASONING]...[/REASONING].
# The server projects these into a separate `llm_reasoning` event for the UI
# and orchestration log, then strips them from:
#   - the user-visible final response
#   - the accumulated `current_context_text` re-injected next turn
#
# Markers are case-insensitive ASCII (no XML) so small/open models handle them
# as reliably as frontier models.
# ──────────────────────────────────────────────────────────────────────────────
_REASONING_BLOCK_RE = _re.compile(
    r"\[REASONING\](.*?)\[/REASONING\]", _re.DOTALL | _re.IGNORECASE
)


def strip_reasoning(text: str) -> str:
    """Return text with all [REASONING]...[/REASONING] blocks removed."""
    if not text or "[REASONING]" not in text.upper():
        return text
    return _REASONING_BLOCK_RE.sub("", text).strip()


def extract_reasoning(text: str) -> list[str]:
    """Return the contents of every [REASONING] block, in order."""
    if not text:
        return []
    return [m.group(1).strip() for m in _REASONING_BLOCK_RE.finditer(text)]


# ──────────────────────────────────────────────────────────────────────────────
# Tool-call parsing
#
# A single scanner backs both `parse_tool_call` (first call only, early-exits
# on non-tool JSON) and `parse_all_tool_calls` (every call, no early exit).
# The divergence is intentional — see the docstring on _scan_tool_calls.
# ──────────────────────────────────────────────────────────────────────────────


def _is_tool_call(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    name = obj.get("tool")
    return isinstance(name, str) and bool(name.strip())


def _scan_tool_calls(llm_output: str, *, stop_on_non_tool_json: bool) -> list[dict]:
    """Single source of truth for tool-call extraction.

    Strategy (in order):
      1. If the output contains <tool_call>...</tool_call> XML wrappers (CLI
         providers like cli.claude/cli.gemini/cli.codex), return every wrapped
         JSON that looks like a tool call. XML mode is all-or-nothing — when
         any XML wrapper is found we ignore bare JSON in the same response.
      2. Otherwise scan for bare JSON objects with a `tool` key. Try every '{'
         position so that a reasoning preamble before the JSON doesn't break
         parsing.

    The `stop_on_non_tool_json` flag controls the bare-JSON scan:

      - True  (parse_tool_call semantics): on the first well-formed top-level
              JSON object that is NOT a tool call, stop and return whatever
              tool calls were collected so far. This prevents misreading an
              echoed *tool result* (a dict with no `tool` key) as a new call.
              Used by callers that only want the first tool call.

      - False (parse_all_tool_calls semantics): non-tool JSON is skipped but
              scanning continues. The caller executes every tool call in order
              before the next LLM turn.

    Reasoning blocks are stripped *before* scanning so a `tool` keyword inside
    [REASONING] prose can never produce a false positive.
    """
    cleaned = strip_reasoning(llm_output or "")
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    # ── 1. XML <tool_call> wrappers (CLI providers) ─────────────────────────
    xml_calls: list[dict] = []
    for m in _re.finditer(r"<tool_call>(.*?)</tool_call>", cleaned, _re.DOTALL):
        try:
            obj = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if _is_tool_call(obj):
            xml_calls.append(obj)
            if stop_on_non_tool_json:
                return [xml_calls[0]]
    if xml_calls:
        return xml_calls

    # ── 2. Bare JSON object scan ─────────────────────────────────────────────
    if "{" not in cleaned:
        return []

    decoder = json.JSONDecoder()
    tool_calls: list[dict] = []
    used_end = -1  # byte end of the last decoded blob (skip nested braces)

    for pos in [i for i, ch in enumerate(cleaned) if ch == "{"]:
        if pos < used_end:
            continue
        try:
            obj, end_offset = decoder.raw_decode(cleaned[pos:])
            used_end = pos + end_offset
        except json.JSONDecodeError:
            continue
        if _is_tool_call(obj):
            if pos > 0 and not tool_calls:
                preamble = cleaned[:min(pos, 120)].replace("\n", " ")
                print(
                    f"DEBUG _scan_tool_calls: ⚠️  JSON tool call found after "
                    f"{pos} chars of preamble: «{preamble}…»",
                    flush=True,
                )
            tool_calls.append(obj)
            if stop_on_non_tool_json:
                return [tool_calls[0]]
        elif isinstance(obj, dict) and stop_on_non_tool_json:
            print(
                f"DEBUG _scan_tool_calls: ✅ non-tool JSON at pos={pos}, "
                f"keys={list(obj.keys())[:5]} → halting scan",
                flush=True,
            )
            return tool_calls
        # else: not a dict or non-tool dict in all-calls mode → keep scanning

    return tool_calls


def parse_all_tool_calls(llm_output: str) -> list[dict]:
    """Extract ALL tool-call JSON objects from one LLM response, in order."""
    return _scan_tool_calls(llm_output, stop_on_non_tool_json=False)


def parse_tool_call(llm_output: str) -> tuple[dict | None, str | None]:
    """Extract the first tool-call JSON from one LLM response.

    Returns (tool_call_dict, None) when a tool call is found, otherwise
    (None, None). Halts on the first well-formed non-tool JSON to avoid
    re-parsing echoed tool results as new calls.
    """
    calls = _scan_tool_calls(llm_output, stop_on_non_tool_json=True)
    return (calls[0] if calls else None), None







def _resolve_agent_by_id(agent_id):
    """Load an agent dict by ID, falling back to the active agent."""
    if agent_id:
        agents = load_user_agents()
        agent = next((a for a in agents if a["id"] == agent_id), None)
        if agent:
            return agent
    return get_active_agent_data()  # raises RuntimeError if no agents configured


def _inject_db_context(agent_data, system_template):
    """Inject linked DB schema context into system prompt for code agents. Returns updated template."""
    if agent_data.get("type") != "code":
        return system_template
    db_configs_list = agent_data.get("db_configs", [])
    if not db_configs_list:
        return system_template
    try:
        from core.routes.db_configs import load_db_configs
        all_configs = load_db_configs()
        linked_configs = [c for c in all_configs if c.get("id") in db_configs_list]
        if not linked_configs:
            return system_template

        allow_db_write = load_settings().get("allow_db_write", False)

        db_context = (
            "\n\n### LINKED DATABASES ###\n"
            "The following databases are associated with this codebase. "
            "When calling `list_tables`, `get_table_schema`, or `run_sql_query`, "
            "you MUST pass the `db_id` field matching the database you want to query.\n\n"
        )
        for c in linked_configs:
            db_context += f"**DB Name:** {c.get('name')}\n"
            db_context += f"**DB ID:** `{c.get('id')}`  ← pass this as db_id in SQL tool calls\n"
            db_context += f"**Type:** {c.get('db_type')}\n"
            if c.get("description"):
                db_context += f"**Description:** {c.get('description')}\n"
            if c.get("schema_info"):
                db_context += f"**Schema:**\n{c.get('schema_info')}\n"
            db_context += "---\n"

        if allow_db_write:
            db_context += (
                "\n**DB WRITE RULES (MANDATORY):**\n"
                "- Write queries (INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE, etc.) ARE permitted.\n"
                "- You MUST explicitly state the exact query you intend to run and ask the user for confirmation BEFORE calling `run_sql_query` with any write query.\n"
                "- Never assume consent. Even for seemingly safe updates, always confirm first.\n"
            )
        else:
            db_context += (
                "\n**DB READ-ONLY MODE (MANDATORY):**\n"
                "- You are STRICTLY limited to SELECT, SHOW, and DESCRIBE queries.\n"
                "- NEVER attempt INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, or any other write operation.\n"
                "- If the user asks you to modify data, inform them that DB write access is disabled in General Settings.\n"
            )

        return system_template + db_context
    except Exception as e:
        print(f"DEBUG: Failed to load db context: {e}")
        return system_template


def _inject_repo_context(agent_data, system_template):
    """Inject repo context into system prompt for code agents. Returns updated template."""
    if agent_data.get("type") != "code":
        return system_template
    if not load_settings().get("embed_code", False):
        return system_template
    repos_list = agent_data.get("repos", [])
    if not repos_list:
        return system_template
    try:
        from core.routes.repos import load_repos
        all_repos = load_repos()
        linked_repos = [r for r in all_repos if r.get("id") in repos_list]
        if not linked_repos:
            return system_template
        repo_context = (
            "\n\n### LINKED CODE REPOSITORIES ###\n"
            "You have access to search the following indexed code repositories "
            "using the `search_codebase` tool. When searching, you MUST provide "
            "the `repo_ids` parameter as an array of IDs (e.g. [\"<repo_id>\"]) from the list below, "
            "along with your natural language query.\n\n"
        )
        for r in linked_repos:
            repo_context += f"**Repo Name:** {r.get('name')}\n"
            repo_context += f"**Repo ID:** `{r.get('id')}`  ← use as: repo_ids: [\"{r.get('id')}\"]\n"
            repo_context += f"**Path:** {r.get('path')}\n"
            if r.get("description"):
                repo_context += f"**Description & Interconnections:** {r.get('description')}\n"
            repo_context += "---\n"
        repo_context += (
            "\n**Tip:** `search_codebase` uses semantic vector search — it works well for "
            "understanding questions (\"how does X work?\", \"where is Y handled?\") where "
            "exact keyword matches are unreliable.\n"
        )
        return system_template + repo_context
    except Exception as e:
        print(f"DEBUG: Failed to load repo context: {e}")
        return system_template


def _build_delegate_context(
    active_agent: dict,
    all_tools: list,
    tool_schema_map: dict,
    ollama_tools: list,
) -> dict:
    """Build delegate agent context: load eligible sub-agents and inject the
    synthetic delegate_to_agent tool into the tool lists.

    Returns a dict of agent_id -> agent_data for all eligible sub-agents.
    """
    from core.tools import VirtualTool

    agents = load_user_agents()
    own_id = active_agent.get("id", "")
    delegate_agent_ids = active_agent.get("delegate_agent_ids") or []

    # Build the eligible agents map
    eligible: dict = {}
    for a in agents:
        aid = a.get("id", "")
        atype = a.get("type", "")
        # Skip self, builder agents, and other delegate agents (no infinite loops)
        if aid == own_id or atype in ("builder",):
            continue
        # If specific agents are selected, filter to those
        if delegate_agent_ids and aid not in delegate_agent_ids:
            continue
        eligible[aid] = a

    if not eligible:
        print(f"DEBUG: ⚠ Delegate agent '{own_id}' has no eligible sub-agents", flush=True)
        return {}

    # Build agent descriptions for the tool's description
    agent_summaries = []
    for aid, a in eligible.items():
        agent_summaries.append(f"  - {a.get('name', aid)} (id: {aid}): {a.get('description', 'No description')}")
    agents_list_str = "\n".join(agent_summaries)

    # Create the synthetic delegate_to_agent tool
    delegate_tool = VirtualTool(
        name="delegate_to_agent",
        description=(
            "Delegate a task to a specific sub-agent. The agent will run its full "
            "ReAct loop autonomously and return its result. Use this to route tasks "
            "to the most appropriate agent based on their capabilities.\n\n"
            f"Available agents:\n{agents_list_str}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "ID of the agent to delegate to",
                    "enum": list(eligible.keys()),
                },
                "task": {
                    "type": "string",
                    "description": "Clear, specific instructions for the agent to execute",
                },
            },
            "required": ["agent_id", "task"],
        },
    )

    # Inject into tool collections
    all_tools.append(delegate_tool)
    tool_schema_map["delegate_to_agent"] = delegate_tool.inputSchema
    ollama_tools.append({
        "type": "function",
        "function": {
            "name": delegate_tool.name,
            "description": delegate_tool.description,
            "parameters": delegate_tool.inputSchema,
        },
    })

    print(f"DEBUG: 🤝 Delegate agent '{own_id}' can route to {len(eligible)} agents: {list(eligible.keys())}", flush=True)
    return eligible


def _inject_delegate_roster(system_template: str, agents_map: dict) -> str:
    """Inject a section into the system prompt listing available sub-agents
    with their names, descriptions, and tool capabilities."""
    if not agents_map:
        return system_template

    roster_lines = ["### AVAILABLE SUB-AGENTS", ""]
    roster_lines.append(
        "You are a **delegate agent**. Your job is to analyze the user's request and route "
        "tasks to the most appropriate sub-agent using the `delegate_to_agent` tool. "
        "Review each sub-agent's description and tools to make the best routing decision.\n"
    )
    roster_lines.append(
        "After a sub-agent completes its task, review the result. You can either:\n"
        "1. Delegate to another agent if more work is needed\n"
        "2. Provide your final synthesized response if all tasks are complete\n"
    )

    for aid, agent in agents_map.items():
        name = agent.get("name", aid)
        desc = agent.get("description", "No description")
        tools = agent.get("tools", [])
        agent_type = agent.get("type", "conversational")

        roster_lines.append(f"**{name}** (`{aid}`) — {agent_type}")
        roster_lines.append(f"  Description: {desc}")
        if tools and tools != ["all"]:
            # Show first 15 tool names as a capability summary
            tool_preview = ", ".join(tools[:15])
            if len(tools) > 15:
                tool_preview += f", ... (+{len(tools) - 15} more)"
            roster_lines.append(f"  Tools: {tool_preview}")
        elif tools == ["all"]:
            roster_lines.append("  Tools: All available tools")
        roster_lines.append("")

    roster_lines.append("### WHEN TO DELEGATE")
    roster_lines.append(
        "- Delegate when the request requires tools, knowledge, or specialized "
        "prompts you don't have yourself."
    )
    roster_lines.append("- Do NOT delegate when:")
    roster_lines.append(
        "  - You can answer from the conversation context already in front of you."
    )
    roster_lines.append(
        "  - The request is conversational (greeting, clarification, recap of prior work)."
    )
    roster_lines.append(
        "  - You just received output from the only agent that could handle this "
        "(would cause a loop)."
    )
    roster_lines.append("")
    roster_lines.append("### HOW TO CHOOSE")
    roster_lines.append(
        "- Pick the SINGLE most specific agent. If multiple match, prefer the one "
        "with the most relevant tools listed."
    )
    roster_lines.append(
        "- After a sub-agent returns, synthesize a final response directly unless "
        "meaningful additional work is needed — chained delegation is expensive."
    )
    roster_lines.append(
        "- Never delegate back to the agent you received output from in the "
        "immediately previous step."
    )
    roster_lines.append("")

    return system_template + "\n\n" + "\n".join(roster_lines)


async def _run_spawn_subtask_batch(
    subtask_calls: list[dict],
    parent_agent_id: str,
    available_tools: list[str],
    session_id: str,
    server_module,
    source: str,
    run_id: str | None,
) -> list[tuple[str, str]]:
    """Run a batch of spawn_subtask calls in parallel.

    Each call gets an ephemeral agent config with only the requested tools
    (validated as a subset of the parent's available tools). Returns a list of
    (name, result_text) tuples in the same order as subtask_calls.
    """
    import uuid
    import asyncio

    async def _run_one(args: dict) -> tuple[str, str]:
        task = args.get("task", "")
        requested_tools = args.get("tools") or []
        name = args.get("name") or task[:40]
        # Enforce subset — sub-agent can only use tools the parent actually has
        valid_tools = [t for t in requested_tools if t in available_tools]
        if not valid_tools:
            valid_tools = available_tools[:5]

        ephemeral_agent = {
            "id": f"subtask_{uuid.uuid4().hex[:8]}",
            "name": name,
            "type": "conversational",
            "model": None,  # inherit global default
            "system_prompt": (
                "You are a focused sub-agent. Complete the given task using only "
                "the provided tools. Return a concise, structured result."
            ),
            "tools": valid_tools,
            "max_turns": 10,
        }

        sub_final = ""
        try:
            async for sub_event in run_agent_step(
                message=task,
                agent_id=ephemeral_agent["id"],
                session_id=session_id,
                server_module=server_module,
                max_turns=10,
                source=source,
                run_id=run_id,
                agent_override=ephemeral_agent,
            ):
                if sub_event.get("type") == "final":
                    sub_final = sub_event.get("response", "")
        except Exception as exc:
            sub_final = f"Subtask error: {exc}"
            print(f"DEBUG: ❌ spawn_subtask '{name}' failed: {exc}", flush=True)

        print(f"DEBUG: ✅ spawn_subtask '{name}' completed ({len(sub_final)} chars)", flush=True)
        return (name, sub_final)

    return list(await asyncio.gather(*[_run_one(args) for args in subtask_calls]))


async def run_agent_step(
    message,
    agent_id,
    session_id,
    server_module,
    max_turns=None,
    allowed_tools_override=None,
    source: str = "chat",
    run_id: str | None = None,
    images: list[str] | None = None,
    system_prompt_extra: str | None = None,
    system_prompt_prefix: str | None = None,
    # ── optional extension params (used by builder wrapper) ───────────────────
    agent_override: dict | None = None,   # skip _resolve_agent_by_id
    tools_override: list | None = None,   # skip aggregate_all_tools; list of OpenAI-format tool dicts
    tool_executor=None,                   # async (name, args) -> str | None; None = fall through
    post_tool_hook=None,                  # async gen (name, raw_output) -> yields extra events
    history_override: list | None = None, # use instead of get_recent_history_messages
    model_override: str | None = None,    # per-step model override (from StepConfig.model)
):
    """Lower-level single-agent ReAct execution.

    Used by both run_react_loop (chat) and the orchestration engine (per-step).
    Yields the same structured events as run_react_loop.
    """
    if max_turns is None:
        max_turns = MAX_TURNS

    # Resolve agent — Postgres first (worker mode), JSON fallback
    if agent_override is not None:
        active_agent = agent_override
    else:
        from core.scale.context import resolve_agent as _ctx_resolve_agent
        resolved = await _ctx_resolve_agent(agent_id) if agent_id else None
        active_agent = resolved if resolved is not None else _resolve_agent_by_id(agent_id)
    agent_id_for_session = active_agent.get("id", agent_id or "unknown")

    # Build system prompt with repo and DB context injection
    agent_system_template = active_agent.get("system_prompt", "")
    agent_system_template = _inject_repo_context(active_agent, agent_system_template)
    agent_system_template = _inject_db_context(active_agent, agent_system_template)

    current_settings = load_settings()
    # Per-agent model override: use agent's model if set, else fall back to default.
    # Treat None, empty string, or "default" as "use the global default model".
    agent_model = active_agent.get("model")
    if agent_model and agent_model.strip().lower() in ("", "default"):
        agent_model = None
    # Precedence: per-step model (from orchestration) > per-agent model > global default
    step_model = model_override.strip() if (model_override and model_override.strip().lower() not in ("", "default")) else None
    current_model = step_model or agent_model or current_settings.get("model", "mistral")
    # Auto-detect mode from model name instead of relying on global mode
    from core.llm_providers import detect_mode_from_model
    mode = detect_mode_from_model(current_model)
    # Only inject tools into the system prompt for providers without native tool calling.
    # Cloud (Anthropic/OpenAI/Gemini/Grok/DeepSeek) and local Ollama receive tools via
    # the API tools= param, so injecting them into the system prompt is redundant.
    inject_tools_in_prompt = mode in ("cli", "bedrock")

    # Aggregate tools & build system prompt
    if tools_override is not None:
        # Caller provided a fixed tool set (e.g. builder) — skip MCP aggregation
        tool_schema_map = {t["function"]["name"]: t for t in tools_override}
        all_tools = list(tool_schema_map.values())
        ollama_tools = tools_override
        tools_json = json.dumps(tools_override)
        print(f"DEBUG RUN_AGENT: start agent_id={agent_id_for_session}, tools_override={len(tools_override)} tools", flush=True)
    else:
        from core.scale.context import resolve_custom_tools as _ctx_resolve_tools
        custom_tools = await _ctx_resolve_tools()
        print(f"DEBUG RUN_AGENT: start agent_id={agent_id_for_session}, sessions={list(server_module.agent_sessions.keys())}", flush=True)
        all_tools, tool_schema_map, ollama_tools, tools_json = await aggregate_all_tools(
            server_module.agent_sessions, active_agent, custom_tools
        )
        print(f"DEBUG RUN_AGENT: aggregate_all_tools done, tool_count={len(all_tools)}", flush=True)
    allowed_tools = list(allowed_tools_override) if allowed_tools_override else active_agent.get("tools", ["all"])
    agent_type = active_agent.get("type", "conversational")

    # ── DELEGATE AGENT: inject synthetic delegate_to_agent tool + agent context ──
    _delegate_agents_map: dict = {}  # agent_id -> agent dict (populated only for delegates)
    if agent_type == "delegate" and tools_override is None:
        _delegate_agents_map = _build_delegate_context(
            active_agent, all_tools, tool_schema_map, ollama_tools
        )
        # Rebuild tools_json after injection so system prompt includes the new tool
        tools_json = str([
            {'tool': t.name, 'description': t.description, 'schema': t.inputSchema}
            for t in all_tools
        ])
        # Ensure delegate_to_agent is always allowed
        if "all" not in allowed_tools and "delegate_to_agent" not in allowed_tools:
            allowed_tools.append("delegate_to_agent")
        # Inject agent roster into system prompt
        agent_system_template = _inject_delegate_roster(agent_system_template, _delegate_agents_map)

    # ── SPAWN_SUBTASK: inject dynamic tool when agent has opted in ──
    from core.spawn_subtask import SPAWN_SUBTASK_TOOL_NAME, build_dynamic_spawn_subtask_tool
    _spawn_subtask_enabled = (
        tools_override is None
        and SPAWN_SUBTASK_TOOL_NAME in (active_agent.get("tools") or [])
    )
    _spawn_subtask_available_tools: list[str] = []
    if _spawn_subtask_enabled:
        # Collect all tool names currently available to this agent (excluding spawn_subtask itself)
        _spawn_subtask_available_tools = [
            t.name if hasattr(t, "name") else t.get("function", {}).get("name", "")
            for t in all_tools
        ]
        _spawn_subtask_available_tools = [
            n for n in _spawn_subtask_available_tools
            if n and n != SPAWN_SUBTASK_TOOL_NAME
        ]
        if _spawn_subtask_available_tools:
            spawn_tool = build_dynamic_spawn_subtask_tool(_spawn_subtask_available_tools)
            # Remove any static placeholder that may have been loaded via aggregate_all_tools
            all_tools = [t for t in all_tools if (t.name if hasattr(t, "name") else t.get("function", {}).get("name", "")) != SPAWN_SUBTASK_TOOL_NAME]
            all_tools.append(spawn_tool)
            tool_schema_map[SPAWN_SUBTASK_TOOL_NAME] = spawn_tool.inputSchema
            ollama_tools = [t for t in ollama_tools if t.get("function", {}).get("name") != SPAWN_SUBTASK_TOOL_NAME]
            ollama_tools.append({"type": "function", "function": {
                "name": spawn_tool.name,
                "description": spawn_tool.description,
                "parameters": spawn_tool.inputSchema,
            }})
            tools_json = str([
                {"tool": t.name if hasattr(t, "name") else t.get("function", {}).get("name"),
                 "description": t.description if hasattr(t, "description") else t.get("function", {}).get("description"),
                 "schema": t.inputSchema if hasattr(t, "inputSchema") else t.get("function", {}).get("parameters")}
                for t in all_tools
            ])
            print(f"DEBUG: 🤖 spawn_subtask injected for agent '{agent_id_for_session}' "
                  f"with {len(_spawn_subtask_available_tools)} delegatable tools", flush=True)

    system_prompt_text = build_system_prompt(
        agent_system_template, tools_json, session_id,
        _get_session_state, server_module.memory_store, agent_id=agent_id_for_session,
        turns_remaining=max_turns, max_turns=max_turns,
        inject_tools=inject_tools_in_prompt,
    )

    # Inject orchestration-awareness block when called from an orchestration step
    if system_prompt_extra:
        system_prompt_text = system_prompt_text + "\n\n" + system_prompt_extra
    # Iteration banner: prepended so it can't be drowned out by a long agent
    # system prompt. Only set on re-runs (execution_number > 1).
    if system_prompt_prefix:
        system_prompt_text = system_prompt_prefix + "\n\n" + system_prompt_text

    async def generate_response(prompt_msg, sys_prompt, tools=None, history_messages=None, memory_context_text="", images_for_turn=None, tool_name_for_log=None):
        return await llm_generate_response(
            prompt_msg=prompt_msg,
            sys_prompt=sys_prompt,
            mode=mode,
            current_model=current_model,
            current_settings=current_settings,
            tools=tools,
            history_messages=history_messages,
            memory_context_text=memory_context_text,
            session_id=session_id,
            agent_id=agent_id_for_session,
            source=source,
            run_id=run_id,
            tool_name=tool_name_for_log,
            images=images_for_turn,
        )

    # ReAct loop state
    user_message = message
    memory_context = ""
    # Orchestrator, builder, and delegate agents always start fresh — no history carryover between runs
    is_orchestrator = agent_type in ("orchestrator", "builder", "delegate")
    if history_override is not None:
        recent_history_messages = history_override
    elif is_orchestrator:
        recent_history_messages = []
    else:
        recent_history_messages = get_recent_history_messages(session_id, agent_id=agent_id_for_session)
    current_context_text = f"User Request: {user_message}\n"
    final_response = ""
    last_intent = "chat"
    last_data = None
    tool_name = None
    last_tool_logged = None  # track tool from previous turn for usage log
    tools_used_summary = []
    tool_repetition_counts = {}
    # Compact log of browser actions taken this run — prepended before the live
    # <<BROWSER_STATE>> block so the agent retains full action history even
    # though the DOM snapshot is replaced on each navigation/click.
    browser_action_history: list[str] = []
    _has_compacted = False  # anti-thrash: only compact once per run

    # Build type-aware set of always-allowed tools
    always_allowed = set(DEFAULT_TOOLS_BY_TYPE.get("all_types", set()))
    always_allowed |= set(DEFAULT_TOOLS_BY_TYPE.get(agent_type, set()))

    MAX_PROMPT_CHARS = 400000

    # Browser tools produce DOM snapshots that are only useful for the current turn.
    # Previous snapshots become stale the moment the page changes, so we skip
    # appending them to the accumulated context and only keep a short summary.
    BROWSER_TOOL_PREFIXES = ("browser_",)

    def _truncate_tool_output(text: str, limit: int = 0) -> str:
        """No-op: returns the full tool output without any truncation."""
        return text

    def _is_browser_tool(name: str) -> bool:
        return name.startswith(BROWSER_TOOL_PREFIXES)

    async with httpx.AsyncClient() as client:
        for turn in range(max_turns):
            print(f"\n{'#'*60}\n### TURN {turn + 1}/{max_turns} ###\n{'#'*60}\n")

            yield {"type": "thinking", "message": "Analyzing your request..."}

            # Rebuild system prompt each turn so turns_remaining is always accurate
            turns_remaining = max_turns - turn  # after this turn starts, turns_remaining decreases
            active_sys_prompt = build_system_prompt(
                agent_system_template, tools_json, session_id,
                _get_session_state, server_module.memory_store, agent_id=agent_id_for_session,
                turns_remaining=turns_remaining, max_turns=max_turns,
                inject_tools=inject_tools_in_prompt,
            )
            # Re-inject orchestration context (system_prompt_extra) on every turn.
            # This block is built once before the loop and must survive every
            # rebuild of active_sys_prompt so the agent never loses orchestration
            # step context, shared state, or turn-budget instructions.
            if system_prompt_extra:
                active_sys_prompt = active_sys_prompt + "\n\n" + system_prompt_extra
            if system_prompt_prefix:
                active_sys_prompt = system_prompt_prefix + "\n\n" + active_sys_prompt

            # Determine prompt
            if turn == 0:
                active_prompt = user_message
                active_history = recent_history_messages
            else:
                active_prompt = current_context_text
                active_history = []

            # Auto-compact: summarise accumulated context when it exceeds the threshold.
            # Gated on turn > 0 (nothing to compact on the first turn) and
            # _has_compacted (anti-thrash: skip if we already compacted this run so we
            # don't loop calling the LLM when a single output fills the window).
            if turn > 0 and not _has_compacted:
                _compact_ctx, _compact_hist, _compact_path, _compact_stats = await maybe_compact(
                    current_context_text,
                    recent_history_messages,
                    current_settings,
                    current_model,
                    mode,
                    current_settings,
                    session_id,
                    agent_id_for_session,
                    run_id=run_id,
                )
                if _compact_ctx is not current_context_text:
                    current_context_text = _compact_ctx
                    recent_history_messages = _compact_hist
                    active_prompt = current_context_text
                    active_history = []
                    _has_compacted = True
                    yield {
                        "type": "context_compact",
                        "stage": (_compact_stats or {}).get("stage", "unknown"),
                        "chars_before": (_compact_stats or {}).get("chars_before", 0),
                        "chars_after": (_compact_stats or {}).get("chars_after", 0),
                        "reduction_pct": (_compact_stats or {}).get("reduction_pct", 0),
                        "archive_path": _compact_path,
                    }

            # Safety guard: truncate if too long
            total_prompt_chars = len(active_prompt) + len(active_sys_prompt) + len(memory_context)
            print(f"DEBUG: 📊 Context size — prompt: {len(active_prompt)} | system: {len(active_sys_prompt)} | memory: {len(memory_context)} | total: {total_prompt_chars} chars")
            if total_prompt_chars > MAX_PROMPT_CHARS:
                overflow = total_prompt_chars - MAX_PROMPT_CHARS
                active_prompt = active_prompt[: len(active_prompt) - overflow]
                print(f"DEBUG: ⚠️ Truncated prompt by {overflow} chars")

            # Emit per-turn LLM call context to loggers before invoking the model
            yield {
                "type": "_log_llm_call",
                "turn": turn + 1,
                "model": current_model,
                "system_chars": len(active_sys_prompt),
                "prompt_chars": len(active_prompt),
                "memory_chars": len(memory_context),
                "history_turns": len(active_history),
                "total_chars": total_prompt_chars,
                "prompt": active_prompt,
                "system_prompt": active_sys_prompt,
            }

            # Ask LLM
            print(f"DEBUG: 🔄 Calling LLM...", flush=True)
            _llm_start = time.time()
            try:
                llm_output = await generate_response(
                    active_prompt, active_sys_prompt,
                    tools=ollama_tools, history_messages=active_history,
                    memory_context_text=memory_context,
                    images_for_turn=images if turn == 0 else None,  # images only on first turn
                    tool_name_for_log=last_tool_logged,
                )
            except Exception as llm_err:
                _llm_duration = round(time.time() - _llm_start, 1)
                error_msg = f"LLM Error ({_llm_duration}s): {llm_err}"
                print(f"DEBUG: ❌ {error_msg}", flush=True)
                final_response = str(llm_err)
                yield {"type": "error", "message": str(llm_err)}
                break
            _llm_duration = round(time.time() - _llm_start, 1)
            print(f"DEBUG: 🤖 LLM Response ({_llm_duration}s): {llm_output[:500]}{'...(truncated)' if len(llm_output) > 500 else ''}")

            # ── Project [REASONING] blocks into a dedicated event ────────────
            # The LLM emits its private chain-of-thought wrapped in
            # [REASONING]...[/REASONING]. We extract those blocks, ship them
            # to the UI / orchestration log via a structured `llm_reasoning`
            # event, and feed the *cleaned* action text into both the visible
            # thought event and the running context. This:
            #   - gives users readable "thinking" panels above each tool call
            #   - prevents planning text from compounding across turns
            #   - leaves the model's wire format unchanged (it still emits
            #     [REASONING] markers; the projection is purely server-side)
            reasoning_blocks = extract_reasoning(llm_output)
            action_text = strip_reasoning(llm_output)

            if reasoning_blocks:
                yield {
                    "type": "llm_reasoning",
                    "reasoning": "\n\n".join(reasoning_blocks),
                    "turn": turn + 1,
                }

            if action_text.strip():
                yield {"type": "llm_thought", "thought": action_text, "turn": turn + 1}

            # Parse every tool call the LLM emitted in this response and execute
            # them sequentially before the next LLM turn.  This prevents partial
            # saves when a provider batches multiple calls into one response.
            tool_calls = parse_all_tool_calls(action_text)

            if not tool_calls:
                # Final response: strip any leftover reasoning so it never
                # reaches the user or the session history.
                final_response = action_text
                break

            # Append the LLM's action text (tool-call JSON + any prose) to the
            # running context, tagged with the turn number for temporal clarity.
            # Reasoning blocks are intentionally NOT re-injected — they were
            # private scratch space for that single turn.
            if action_text.strip():
                current_context_text += f"\n[Turn {turn + 1} — Action]\n{action_text}\n"

            # ── SPAWN_SUBTASK batch pre-run ──────────────────────────────────────
            # Collect all spawn_subtask calls from this turn and run them in parallel
            # BEFORE the sequential tool loop. Results are consumed in order below.
            _spawn_subtask_result_queue: list[tuple[str, str]] = []
            if _spawn_subtask_enabled:
                _spawn_calls_this_turn = [
                    tc.get("arguments", {})
                    for tc in tool_calls
                    if tc.get("tool") == SPAWN_SUBTASK_TOOL_NAME
                ]
                if _spawn_calls_this_turn:
                    n = len(_spawn_calls_this_turn)
                    yield {"type": "thinking", "message": f"Spawning {n} sub-agent{'s' if n > 1 else ''}..."}
                    _spawn_subtask_result_queue = await _run_spawn_subtask_batch(
                        subtask_calls=_spawn_calls_this_turn,
                        parent_agent_id=agent_id_for_session,
                        available_tools=_spawn_subtask_available_tools,
                        session_id=session_id,
                        server_module=server_module,
                        source=source,
                        run_id=run_id,
                    )

            for tool_call in tool_calls:
                tool_name = tool_call.get("tool", "")
                if not isinstance(tool_name, str) or not tool_name.strip():
                    continue
                tool_args = tool_call.get("arguments", {})
                last_tool_logged = tool_name  # record for next turn's log entry

                # Apply sticky args
                tool_schema = tool_schema_map.get(tool_name)
                tool_args = _apply_sticky_args(session_id, tool_name, tool_args, tool_schema)

                # ── browser_snapshot filename guard ────────────────────────────
                # Ensure the `filename` arg always lives under data/vault/ so
                # snapshots are persisted in the correct location regardless of
                # what the LLM provides.
                if tool_name == "browser_snapshot" and "filename" in tool_args:
                    _snap_fname = tool_args["filename"]
                    _vault_prefix = "data/vault/"
                    if not _snap_fname.startswith(_vault_prefix):
                        # Strip any leading slashes / accidental partial paths
                        # then prepend the canonical prefix.
                        _snap_fname = _snap_fname.lstrip("/")
                        # If it already contains "data/" or "vault/" as a fragment,
                        # strip that too so we never end up with double prefixes.
                        for _bad_prefix in ("data/vault/", "vault/", "data/"):
                            if _snap_fname.startswith(_bad_prefix):
                                _snap_fname = _snap_fname[len(_bad_prefix):]
                                break
                        tool_args = dict(tool_args)  # don't mutate original
                        tool_args["filename"] = _vault_prefix + _snap_fname
                        print(
                            f"DEBUG: 📸 browser_snapshot filename normalised → {tool_args['filename']}",
                            flush=True,
                        )

                yield {"type": "tool_execution", "tool_name": tool_name, "args": tool_args}
                print(f"DEBUG: 🔧 Tool Call: {tool_name}")
                print(f"DEBUG: 📥 Args: {json.dumps(tool_args, indent=2, default=str)[:1000]}")

                # ── sequentialthinking hard cap (5 calls per task) ─────────────────
                # sequentialthinking changes `thoughtNumber` on every call so the
                # identical-args guard below never fires.  Limit to 5 calls so the
                # LLM cannot spin in an infinite think loop.
                if tool_name == "sequentialthinking":
                    _seq_count = tool_repetition_counts.get("__sequentialthinking__", 0) + 1
                    tool_repetition_counts["__sequentialthinking__"] = _seq_count
                    if _seq_count > 5:
                        _seq_msg = (
                            "sequentialthinking has already been called 5 times this task. "
                            "You MUST now call a real action tool (browser, search, data tool, etc.) "
                            "to make progress. Do NOT call sequentialthinking again."
                        )
                        print(f"DEBUG: 🔁 sequentialthinking cap hit (call #{_seq_count}) — blocked", flush=True)
                        current_context_text += f"\nSystem: {_seq_msg}\n"
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": "Blocked: sequentialthinking already used 5 times (call a real tool now)"}
                        continue

                # Execution guard
                if "all" not in allowed_tools and tool_name not in allowed_tools and tool_name not in always_allowed:
                    block_msg = f"Tool '{tool_name}' is not available for this agent. Available tools: {', '.join(allowed_tools)}."
                    current_context_text += f"\nSystem: {block_msg}\n"
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": "Blocked: Tool not available for this agent"}
                    continue

                if tool_name == "query_past_conversations":
                    # Tool has been removed — inform the LLM
                    current_context_text += f"\nSystem: The 'query_past_conversations' tool is no longer available. Use the current conversation context instead.\n"
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": "Tool removed"}
                    continue

                # ===== DETERMINISTIC TOOL CACHE LOOKUP =====
                # Short-circuit before dispatching when this tool is in the
                # DETERMINISTIC_TOOLS registry and we have a live cache entry.
                # Side-effectful tools (bash, sql_agent, web_scraper, sandbox)
                # are never cached — see core.cache.tool_cache.DETERMINISTIC_TOOLS.
                from core.cache import tool_cache as _tool_cache
                if _tool_cache.is_cacheable(tool_name):
                    _cached = _tool_cache.get(tool_name, tool_args, session_id=session_id)
                    if _cached is not None:
                        raw_output = _cached if isinstance(_cached, str) else json.dumps(_cached)
                        current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned (cached):\n{raw_output}\n"
                        tools_used_summary.append(f"{tool_name}: {raw_output}")
                        preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                        print(f"DEBUG: ⚡ tool_cache hit ({tool_name})")
                        yield {"type": "tool_cache_hit", "tool_name": tool_name, "preview": preview}
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                        if post_tool_hook is not None:
                            async for _extra in post_tool_hook(tool_name, raw_output):
                                yield _extra
                        continue

                # ===== BUILT-IN EXECUTOR OVERRIDE (e.g. builder) =====
                # Checked before MCP/custom routing; returns None to fall through.

                if tool_executor is not None:
                    _exec_result = await tool_executor(tool_name, tool_args)
                    if _exec_result is not None:
                        raw_output = maybe_vault(tool_name, _exec_result)
                        current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned:\n{raw_output}\n"
                        tools_used_summary.append(f"{tool_name}: {raw_output}")
                        preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                        print(f"DEBUG: 🔧 Executor result ({tool_name}): {preview}")
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                        if post_tool_hook is not None:
                            async for _extra in post_tool_hook(tool_name, raw_output):
                                yield _extra
                        continue

                # ===== SPAWN_SUBTASK =====
                if tool_name == SPAWN_SUBTASK_TOOL_NAME and _spawn_subtask_enabled:
                    if _spawn_subtask_result_queue:
                        sub_name, result_text = _spawn_subtask_result_queue.pop(0)
                    else:
                        # Fallback: run synchronously if queue is unexpectedly empty
                        sub_name = tool_args.get("name") or tool_args.get("task", "")[:40]
                        results = await _run_spawn_subtask_batch(
                            subtask_calls=[tool_args],
                            parent_agent_id=agent_id_for_session,
                            available_tools=_spawn_subtask_available_tools,
                            session_id=session_id,
                            server_module=server_module,
                            source=source,
                            run_id=run_id,
                        )
                        sub_name, result_text = results[0] if results else (sub_name, "No result")

                    current_context_text += f"\n[Turn {turn + 1} — Observation] Subtask '{sub_name}' result:\n{result_text}\n"
                    tools_used_summary.append(f"spawn_subtask({sub_name}): {result_text[:200]}")
                    preview = result_text[:500] + "..." if len(result_text) > 500 else result_text
                    yield {"type": "tool_result", "tool_name": SPAWN_SUBTASK_TOOL_NAME,
                           "preview": preview, "subtask_name": sub_name}
                    continue

                # ===== DELEGATE_TO_AGENT (delegate agents) =====
                if tool_name == "delegate_to_agent" and _delegate_agents_map:
                    target_agent_id = tool_args.get("agent_id", "")
                    task_message = tool_args.get("task", "")
                    target_agent = _delegate_agents_map.get(target_agent_id)
                    if not target_agent:
                        block_msg = f"Agent '{target_agent_id}' not found or not available for delegation."
                        current_context_text += f"\nSystem: {block_msg}\n"
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": block_msg}
                        continue

                    agent_name = target_agent.get("name", target_agent_id)
                    print(f"DEBUG: 🤝 Delegate → agent '{agent_name}' ({target_agent_id})", flush=True)
                    yield {"type": "thinking", "message": f"Delegating to {agent_name}..."}

                    # Run the sub-agent's full ReAct loop
                    sub_final = ""
                    try:
                        async for sub_event in run_agent_step(
                            message=task_message,
                            agent_id=target_agent_id,
                            session_id=session_id,
                            server_module=server_module,
                            max_turns=target_agent.get("max_turns") or 15,
                            source=source,
                            run_id=run_id,
                        ):
                            if sub_event.get("type") == "final":
                                # Capture only — do NOT propagate. A delegate sub-agent's
                                # final has no orch_step_id, so chat.py would treat it as the
                                # top-level final and emit `done`, closing the stream before
                                # the delegate can reason / re-delegate. The sub-agent's answer
                                # still surfaces via the tool_result preview below.
                                sub_final = sub_event.get("response", "")
                                continue
                            # Yield non-final sub-agent events so the UI shows progress
                            yield {**sub_event, "delegate_from": agent_id_for_session, "delegate_agent_name": agent_name}
                    except Exception as e:
                        sub_final = f"Error: Sub-agent '{agent_name}' failed: {e}"
                        print(f"DEBUG: ❌ Delegate sub-agent failed: {e}", flush=True)

                    raw_output = maybe_vault(tool_name, sub_final)
                    current_context_text += f"\n[Turn {turn + 1} — Observation] Agent '{agent_name}' response:\n{raw_output}\n"
                    tools_used_summary.append(f"delegate_to_agent({agent_name}): {raw_output}")
                    preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                    print(f"DEBUG: 🤝 Delegate result from '{agent_name}': {preview}")
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                    continue

                # ===== NATIVE BUILDER TOOLS =====
                # Registered as first-class tools in aggregate_all_tools. Any
                # agent that declares these in its `tools` list lands here.
                from core.builder_tools import BUILDER_TOOL_NAMES, execute_builder_tool
                if tool_name in BUILDER_TOOL_NAMES:
                    try:
                        raw_output = await execute_builder_tool(tool_name, tool_args, server_module)
                    except Exception as e:
                        raw_output = json.dumps({"error": str(e)})
                    current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned:\n{raw_output}\n"
                    tools_used_summary.append(f"{tool_name}: {raw_output}")
                    preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                    print(f"DEBUG: 🏗 Builder tool ({tool_name}): {preview}")
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                    if _tool_cache.is_cacheable(tool_name) and not raw_output.startswith('{"error"'):
                        _tool_cache.set(tool_name, tool_args, raw_output, session_id=session_id)
                    if post_tool_hook is not None:
                        async for _extra in post_tool_hook(tool_name, raw_output):
                            yield _extra
                    continue

                # ===== CUSTOM TOOLS (Webhook + Python) =====

                if tool_name not in server_module.tool_router:
                    from core.scale.context import resolve_custom_tools as _ctx_resolve_tools2
                    custom_tools_list = await _ctx_resolve_tools2()
                    target_tool = next((t for t in custom_tools_list if t["name"] == tool_name), None)

                    if target_tool:
                        tool_type = target_tool.get("tool_type", "http")

                        # ── Python Tool ──────────────────────────────────────────
                        if tool_type == "python":
                            try:
                                import asyncio as _asyncio
                                import shutil as _shutil
                                import tempfile as _tempfile
                                from pathlib import Path as _Path

                                python_code = target_tool.get("code", "")
                                if not python_code.strip():
                                    raise ValueError("Python tool has no code defined.")

                                if not _shutil.which("docker"):
                                    raise RuntimeError("Docker is not available. Cannot execute Python tool.")

                                # Inject _args at the top of user's code
                                args_json = json.dumps(tool_args)
                                escaped = args_json.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
                                injected_code = (
                                    f'import json\n_args = json.loads("""{escaped}""")\n\n'
                                    + python_code
                                )

                                DATA_DIR_PATH = _Path(__file__).resolve().parent.parent / "data"
                                vault_root = DATA_DIR_PATH / "vault"
                                docker_image = "sandbox-python:latest"

                                tmp_dir = _tempfile.mkdtemp(prefix="pytool_")
                                script_path = f"{tmp_dir}/script.py"
                                try:
                                    with open(script_path, "w") as _f:
                                        _f.write(injected_code)

                                    docker_cmd = [
                                        "docker", "run", "--rm",
                                        "--memory", "512m",
                                        "--cpus", "1.0",
                                        "--pids-limit", "64",
                                        "--read-only",
                                        "--tmpfs", "/tmp:rw,size=256m",
                                        "--tmpfs", "/root:rw,size=256m",
                                        "-v", f"{script_path}:/sandbox/script.py:ro",
                                    ]
                                    if vault_root.exists():
                                        docker_cmd += ["-v", f"{vault_root}:/data:ro"]
                                    docker_cmd += [docker_image, "python", "/sandbox/script.py"]

                                    proc = await _asyncio.create_subprocess_exec(
                                        *docker_cmd,
                                        stdout=_asyncio.subprocess.PIPE,
                                        stderr=_asyncio.subprocess.PIPE,
                                    )
                                    try:
                                        stdout_b, stderr_b = await _asyncio.wait_for(
                                            proc.communicate(), timeout=35
                                        )
                                    except _asyncio.TimeoutError:
                                        proc.kill()
                                        await proc.wait()
                                        raise RuntimeError("Python tool execution timed out after 30s")

                                    stdout_text = stdout_b.decode("utf-8", errors="replace")[:20000]
                                    stderr_text = stderr_b.decode("utf-8", errors="replace")[:5000]

                                    if proc.returncode != 0:
                                        raw_output = json.dumps({
                                            "error": f"Python tool exited with code {proc.returncode}",
                                            "stderr": stderr_text,
                                            "stdout": stdout_text,
                                        })
                                    else:
                                        # Try to parse stdout as JSON, otherwise wrap as string output
                                        try:
                                            parsed_out = json.loads(stdout_text.strip())
                                            raw_output = json.dumps(parsed_out)
                                        except Exception:
                                            raw_output = json.dumps({"output": stdout_text})
                                        if stderr_text:
                                            print(f"DEBUG: Python tool stderr: {stderr_text[:200]}")

                                finally:
                                    _shutil.rmtree(tmp_dir, ignore_errors=True)

                                raw_output = maybe_vault(tool_name, raw_output)
                                current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned:\n{raw_output}\n"
                                tools_used_summary.append(f"{tool_name}: {raw_output}")
                                print(f"DEBUG: 🐍 Python Tool Result ({tool_name}): {raw_output}")
                                preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                                yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                                last_intent = "custom_tool"
                                last_data = json.loads(raw_output) if raw_output else {}
                                continue
                            except Exception as e:
                                current_context_text += f"\nSystem: Error executing Python tool '{tool_name}': {str(e)}\n"
                                yield {"type": "tool_result", "tool_name": tool_name, "preview": f"Error: {e}"}
                                continue

                        # ── HTTP / Webhook Tool ───────────────────────────────────
                        try:
                            method = target_tool.get("method", "POST").upper()
                            url = target_tool.get("url")
                            headers = target_tool.get("headers", {})
                            if not url:
                                raise ValueError("No URL configured for this tool.")

                            # ── URL template interpolation ────────────────────────
                            # Resolve {data} → full JSON payload, {data.key} → specific
                            # field (dot-notation supported), consumed keys are tracked
                            # so they are not redundantly sent in query params / body.
                            import re as _re

                            consumed_keys: set = set()

                            def _resolve_path(obj: dict, path: str):
                                """Walk dot-separated path into obj; return str or None."""
                                parts = path.split(".")
                                cur = obj
                                for p in parts:
                                    if not isinstance(cur, dict) or p not in cur:
                                        return None
                                    cur = cur[p]
                                return str(cur) if not isinstance(cur, (dict, list)) else json.dumps(cur)

                            def _replace_placeholder(m: "_re.Match") -> str:
                                expr = m.group(1).strip()
                                # {data} → full JSON payload (legacy)
                                if expr == "data":
                                    return json.dumps(tool_args)
                                # {data.key} → nested dot-notation (legacy)
                                if expr.startswith("data."):
                                    path = expr[len("data."):]
                                    top_key = path.split(".")[0]
                                    val = _resolve_path(tool_args, path)
                                    if val is not None:
                                        consumed_keys.add(top_key)
                                        return val
                                # {key} → bare input schema field name (preferred)
                                if expr in tool_args:
                                    consumed_keys.add(expr)
                                    v = tool_args[expr]
                                    return str(v) if not isinstance(v, (dict, list)) else json.dumps(v)
                                return m.group(0)  # unrecognised – leave as-is

                            url = _re.sub(r"\{([^}]+)\}", _replace_placeholder, url)

                            # Route args based on HTTP method:
                            # GET/DELETE → non-consumed args as query params (avoid double-sending path vars)
                            # POST/PUT   → full args always go in the body (path vars also stay in URL)
                            if method in ("GET", "DELETE"):
                                remaining_args = {k: v for k, v in tool_args.items() if k not in consumed_keys}
                                params = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
                                          for k, v in remaining_args.items()}
                                resp = await client.request(method, url, params=params, headers=headers, timeout=HTTP_TOOL_TIMEOUT)
                            else:
                                # POST / PUT – all args in body even if some were used in the URL
                                resp = await client.request(method, url, json=tool_args, headers=headers, timeout=HTTP_TOOL_TIMEOUT)

                            json_resp = None
                            try:
                                json_resp = resp.json()
                                output_schema = target_tool.get("outputSchema", {})
                                if output_schema and "properties" in output_schema and isinstance(json_resp, dict):
                                    filtered = {k: json_resp[k] for k in output_schema["properties"] if k in json_resp}
                                    if filtered:
                                        json_resp = filtered
                                raw_output = json.dumps(json_resp)
                            except Exception:
                                raw_output = resp.text or json.dumps({"error": f"Empty response (Status: {resp.status_code})"})

                            # Vault large outputs
                            raw_output = maybe_vault(tool_name, raw_output)

                            current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned:\n{raw_output}\n"
                            tools_used_summary.append(f"{tool_name}: {raw_output}")
                            print(f"DEBUG: 📤 Tool Result ({tool_name}): {raw_output}")
                            preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                            yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}

                            last_intent = "custom_tool"
                            last_data = json_resp if json_resp is not None else {"output": raw_output}
                            continue
                        except Exception as e:
                            current_context_text += f"\nSystem: Error executing custom tool {tool_name}: {str(e)}\n"
                            continue

                    # Hallucinated / unknown tool
                    available_tool_names = [t.name for t in all_tools]
                    names_str = ", ".join(available_tool_names[:30])
                    if len(available_tool_names) > 30:
                        names_str += f" ... ({len(available_tool_names)} total)"
                    print(f"DEBUG: ❓ Tool '{tool_name}' not found in any registered source (hallucinated or unregistered)", flush=True)
                    current_context_text += (
                        f"\nSystem: Tool '{tool_name}' does not exist and cannot be called. "
                        f"Do not attempt to call it again. "
                        f"Available tools: {names_str}. "
                        f"Use one of these tools, or respond with plain text if no tool is needed.\n"
                    )
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": "Error: tool not found"}
                    continue

                # ===== MCP TOOLS =====

                agent_name, actual_tool_name = server_module.tool_router[tool_name]
                session = server_module.agent_sessions[agent_name]

                try:
                    # Force event-loop checkpoint before MCP operations.
                    # The MCP stdio transport uses 0-buffer memory streams
                    # (rendezvous channels) — background tasks (_receive_loop,
                    # stdout_reader, stdin_writer) need event-loop ticks to
                    # drain pending data.  In orchestration mode the deeply
                    # nested async-generator chain can starve these tasks.
                    # An explicit checkpoint here ensures they get a turn
                    # before we send a new request.
                    await _anyio.sleep(0)

                    _mcp_t0 = time.time()
                    print(f"DEBUG MCP: ▶ session='{agent_name}' tool='{tool_name}' — ping starting", flush=True)

                    # Health check: ping the session before committing to a blocking
                    # call_tool().
                    try:
                        with _anyio.fail_after(5):
                            await session.send_ping()
                    except (TimeoutError, Exception) as _ping_err:
                        _ping_msg = f"MCP session '{agent_name}' unresponsive (ping: {_ping_err}). Skipping '{tool_name}'."
                        print(f"DEBUG MCP: ⚠️ {_ping_msg} [{round(time.time()-_mcp_t0,2)}s]", flush=True)
                        current_context_text += f"\nSystem: Error — {_ping_msg}\n"
                        try:
                            _mcp_srv_name = agent_name.removeprefix("ext_mcp_")
                            server_module.mcp_manager._set_status(_mcp_srv_name, "disconnected")
                        except Exception:
                            pass
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": f"Error: session unresponsive"}
                        continue

                    print(f"DEBUG MCP: ✓ ping OK [{round(time.time()-_mcp_t0,2)}s] — call_tool starting", flush=True)

                    _timeout = timedelta(seconds=MCP_TOOL_CALL_TIMEOUT)

                    # Checkpoint again right before the actual call_tool —
                    # gives background transport tasks one more scheduling
                    # opportunity after the ping round-trip.
                    await _anyio.sleep(0)

                    with _anyio.fail_after(_timeout.total_seconds() + 5):
                        result = await session.call_tool(actual_tool_name, tool_args, read_timeout_seconds=_timeout)
                        raw_output = result.content[0].text

                    print(f"DEBUG MCP: ✓ call_tool OK [{round(time.time()-_mcp_t0,2)}s] tool='{tool_name}'", flush=True)

                    # Vault large outputs before any further processing
                    raw_output = maybe_vault(tool_name, raw_output)

                    try:
                        parsed = json.loads(raw_output)
                        if "error" in parsed and parsed["error"] == "auth_required":
                            try:
                                _mcp_srv_name = agent_name.removeprefix("ext_mcp_")
                                server_module.mcp_manager._set_status(_mcp_srv_name, "reauth_needed")
                            except Exception:
                                pass
                            yield {"type": "final", "response": "Authentication required.", "intent": "request_auth", "data": parsed, "tool_name": tool_name}
                            return

                        # Set intent for frontend UI rendering.
                        # Map MCP tool names to the intents the frontend expects.
                        TOOL_INTENT_MAP = {
                            "list_gmail_messages": "list_emails",
                            "search_gmail_messages": "list_emails",
                            "get_message": "read_email",
                            "list_drive_files": "list_files",
                            "get_drive_file": "read_file",
                            "list_calendar_events": "list_events",
                            "create_calendar_event": "create_event",
                            "list_directory": "list_local_files",
                        }
                        intent = TOOL_INTENT_MAP.get(tool_name, tool_name)
                        if intent.startswith(("list_", "read_", "create_")):
                            last_intent = intent
                            last_data = parsed
                        if tool_name == "collect_data":
                            last_intent = "collect_data"
                            last_data = parsed
                    except (json.JSONDecodeError, KeyError, TypeError):
                        pass

                    if _is_browser_tool(tool_name):
                        # Browser snapshots go stale on every navigation/click.
                        # Keep a compact action history log + only the latest DOM snapshot.
                        BROWSER_MARKER = "\n<<BROWSER_STATE>>\n"
                        BROWSER_END = "\n<</BROWSER_STATE>>\n"
                        BROWSER_HISTORY_MARKER = "\n<<BROWSER_HISTORY>>\n"
                        BROWSER_HISTORY_END = "\n<</BROWSER_HISTORY>>\n"

                        # Record a one-line summary of this browser action in the history log
                        args_summary = ", ".join(f"{k}={str(v)[:80]}" for k, v in tool_args.items())
                        snapshot_preview = raw_output[:200].replace("\n", " ")
                        browser_action_history.append(
                            f"[Step {turn + 1}] {tool_name}({args_summary}) → {snapshot_preview}..."
                        )

                        # Remove previous browser history block if present
                        bhstart = current_context_text.find(BROWSER_HISTORY_MARKER)
                        if bhstart != -1:
                            bhend = current_context_text.find(BROWSER_HISTORY_END, bhstart)
                            if bhend != -1:
                                current_context_text = current_context_text[:bhstart] + current_context_text[bhend + len(BROWSER_HISTORY_END):]

                        # Remove previous live browser snapshot block if present
                        bstart = current_context_text.find(BROWSER_MARKER)
                        if bstart != -1:
                            bend = current_context_text.find(BROWSER_END, bstart)
                            if bend != -1:
                                current_context_text = current_context_text[:bstart] + current_context_text[bend + len(BROWSER_END):]

                        # Append updated history log + latest snapshot
                        history_text = "\n".join(browser_action_history)
                        current_context_text += (
                            f"{BROWSER_HISTORY_MARKER}Previous browser actions (oldest→newest):\n"
                            f"{history_text}"
                            f"{BROWSER_HISTORY_END}"
                        )
                        current_context_text += f"{BROWSER_MARKER}Tool '{tool_name}' Output (current page state): {raw_output}{BROWSER_END}"
                        print(f"DEBUG: 📤 Tool Result ({tool_name}): [browser, {len(raw_output)} chars in context, history={len(browser_action_history)} actions]")
                    else:
                        current_context_text += f"\n[Turn {turn + 1} — Observation] Tool '{tool_name}' returned:\n{raw_output}\n"
                        print(f"DEBUG: 📤 Tool Result ({tool_name}): {raw_output}")

                    # MCP tool results are no longer embedded in ChromaDB
                    preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                    if _tool_cache.is_cacheable(tool_name):
                        _tool_cache.set(tool_name, tool_args, raw_output, session_id=session_id)
                    if post_tool_hook is not None:
                        async for _extra in post_tool_hook(tool_name, raw_output):
                            yield _extra
                    tools_used_summary.append(f"{tool_name}: {raw_output}")
                except Exception as e:
                    error_msg = str(e)
                    print(f"DEBUG: ❌ Tool '{tool_name}' failed: {error_msg}", flush=True)
                    current_context_text += f"\nSystem: Error executing tool {tool_name}: {error_msg}\n"
                    try:
                        if agent_name and agent_name.startswith("ext_mcp_"):
                            _mcp_srv_name = agent_name.removeprefix("ext_mcp_")
                            server_module.mcp_manager._set_status(_mcp_srv_name, "disconnected")
                    except Exception:
                        pass
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": f"Error: {error_msg}"}

    if not final_response:
        final_response = "I completed the requested actions."

    # Persist conversation turn to JSON session file (skip for orchestrator agents)
    if not is_orchestrator:
        try:
            _save_conversation_turn(
                session_id=session_id,
                agent_id=agent_id_for_session,
                user=user_message,
                assistant=final_response,
                tools=tools_used_summary,
            )
        except Exception as mem_err:
            print(f"WARNING: Session save failed (non-fatal): {mem_err}")

    yield {"type": "final", "response": final_response, "intent": last_intent, "data": last_data, "tool_name": tool_name}


async def run_react_loop(request, server_module):
    """Async generator that runs the ReAct loop and yields structured events.

    This is the top-level entry point called by /chat and /chat/stream.
    It resolves the active agent, checks for orchestrator type, and delegates
    to either the orchestration engine or run_agent_step().

    Event types:
        {"type": "status", "message": str}
        {"type": "thinking", "message": str}
        {"type": "tool_execution", "tool_name": str, "args": dict}
        {"type": "tool_result", "tool_name": str, "preview": str}
        {"type": "final", "response": str, "intent": str, "data": Any, "tool_name": str|None}
        {"type": "error", "message": str}
    """
    if not server_module.agent_sessions:
        yield {"type": "error", "message": "No agents connected"}
        return

    session_id = _get_session_id(request)
    user_message = expand_vault_mentions(request.message)

    # Merge client state
    ss = _get_session_state(session_id)
    if request.client_state and isinstance(request.client_state, dict):
        for key, value in request.client_state.items():
            if value:
                ss[key] = str(value)

    yield {"type": "status", "message": "Processing your request..."}

    # Resolve active agent — Postgres first (worker mode), JSON fallback
    from core.scale.context import resolve_agent as _ctx_resolve_agent
    _resolved = await _ctx_resolve_agent(request.agent_id) if request.agent_id else None
    active_agent = _resolved if _resolved is not None else _resolve_agent_by_id(request.agent_id)

    # --- Orchestrator delegation ---
    if active_agent.get("type") == "orchestrator":
        orch_id = active_agent.get("orchestration_id")
        if orch_id:
            try:
                from core.routes.orchestrations import load_orchestrations
                from core.models_orchestration import Orchestration
                from core.orchestration.engine import OrchestrationEngine

                orchs = load_orchestrations()
                orch_data = next((o for o in orchs if o["id"] == orch_id), None)
                if orch_data:
                    orch = Orchestration.model_validate(orch_data)
                    engine = OrchestrationEngine(orch, server_module)
                    run_id = f"run_{orch_id}_{int(time.time() * 1000)}"
                    async for event in engine.run(user_message, run_id, session_id=session_id):
                        yield event
                    return
                else:
                    yield {"type": "error", "message": f"Orchestration '{orch_id}' not found"}
                    return
            except Exception as e:
                yield {"type": "error", "message": f"Orchestration error: {e}"}
                return
        else:
            yield {"type": "error", "message": "Orchestrator agent has no orchestration_id configured"}
            return

    # --- Standard single-agent ReAct loop ---
    from core.agent_logger import AgentLogger
    _agent_log = AgentLogger(
        agent_id=active_agent.get("id", "default"),
        agent_name=active_agent.get("name", "Unknown Agent"),
        session_id=session_id,
        source="chat",
        user_message=user_message,
    )
    _log_status = "completed"
    try:
        agent_max_turns = active_agent.get("max_turns") or None
        async for event in run_agent_step(
            message=user_message,
            agent_id=active_agent.get("id"),
            session_id=session_id,
            server_module=server_module,
            max_turns=agent_max_turns,
            images=getattr(request, 'images', None) or None,
        ):
            _agent_log.log_event(event)
            yield event
    except Exception:
        _log_status = "error"
        raise
    finally:
        _agent_log.run_end(_log_status)
        _agent_log.close()
