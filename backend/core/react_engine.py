"""
Shared ReAct loop engine used by both /chat and /chat/stream endpoints.
Yields structured event dicts that callers can handle differently
(collect for sync response, or stream as SSE).
"""
import json
import sys
import time

import httpx

from core.config import load_settings
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
from datetime import timedelta

MAX_TURNS = 30


def parse_all_tool_calls(llm_output: str) -> list[dict]:
    """Extract ALL tool-call JSON objects from one LLM response, in order.

    Unlike parse_tool_call, non-tool JSON objects are skipped (not an early-exit
    trigger), so every tool-call found is returned.  The caller executes them
    sequentially before making the next LLM call.
    """
    cleaned = llm_output.replace("```json", "").replace("```", "").strip()

    def _is_tool_call(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        name = obj.get("tool")
        return isinstance(name, str) and bool(name.strip())

    import re as _re

    # XML <tool_call> wrappers (CLI providers) — collect ALL matches in order
    xml_calls: list[dict] = []
    for _m in _re.finditer(r"<tool_call>(.*?)</tool_call>", cleaned, _re.DOTALL):
        try:
            obj = json.loads(_m.group(1).strip())
            if _is_tool_call(obj):
                xml_calls.append(obj)
        except json.JSONDecodeError:
            pass
    if xml_calls:
        return xml_calls

    if "{" not in cleaned:
        return []

    decoder = json.JSONDecoder()
    tool_calls: list[dict] = []
    used_end = -1  # end byte of the last decoded JSON blob (skip nested braces inside it)

    for pos in [i for i, ch in enumerate(cleaned) if ch == "{"]:
        if pos < used_end:
            continue  # position is inside a previously consumed JSON object
        try:
            obj, end_offset = decoder.raw_decode(cleaned[pos:])
            used_end = pos + end_offset
        except json.JSONDecodeError:
            continue
        if _is_tool_call(obj):
            tool_calls.append(obj)
        # Non-tool JSON: advance used_end but keep scanning (do NOT stop early)

    return tool_calls


def parse_tool_call(llm_output: str) -> tuple[dict | None, str | None]:
    """Extract a tool call JSON from LLM text output.

    Searches the entire output for a JSON object containing a 'tool' key.
    This tolerates LLM outputs that include a reasoning preamble before the
    actual tool-call JSON (common in orchestration agents that plan before
    acting).  JSON objects that appear at or near the start of the output are
    tried first so the fast path is preserved for well-behaved models.

    Also handles <tool_call>...</tool_call> XML wrappers emitted by CLI providers
    (claude, gemini, codex) that are instructed to use this format via system prompt.

    Note: 'name' is deliberately NOT treated as an alias for 'tool' here — it
    collides with domain JSON (e.g. an orchestration plan has a top-level `name`
    field) and used to cause the planner's final output to be mis-parsed as a
    tool call.  All LLM providers normalise their native tool-call shapes to
    {"tool": ..., "arguments": ...} at the provider boundary before reaching
    this parser.
    """
    cleaned = llm_output.replace("```json", "").replace("```", "").strip()

    def _is_tool_call(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        name = obj.get("tool")
        return isinstance(name, str) and bool(name.strip())

    # ── Fast path: <tool_call> XML wrapper (CLI providers) ──────────────────────
    import re as _re
    _tc_match = _re.search(r"<tool_call>(.*?)</tool_call>", cleaned, _re.DOTALL)
    if _tc_match:
        try:
            obj = json.loads(_tc_match.group(1).strip())
            if _is_tool_call(obj):
                return obj, None
        except json.JSONDecodeError:
            pass  # Fall through to bare-JSON detection

    if "{" not in cleaned:
        return None, None

    decoder = json.JSONDecoder()

    # Collect all '{' positions so we can try each candidate in order.
    brace_positions = [i for i, ch in enumerate(cleaned) if ch == "{"]

    # Try the earliest position first (fast path for well-behaved models),
    # then fall back to later positions when there is a preamble.
    for pos in brace_positions:
        try:
            obj, _ = decoder.raw_decode(cleaned[pos:])
        except json.JSONDecodeError:
            continue
        if _is_tool_call(obj):
            if pos > 0:
                # LLM prefixed the JSON with preamble text — log it so we
                # can monitor how often this happens.
                preamble_preview = cleaned[:min(pos, 120)].replace("\n", " ")
                print(
                    f"DEBUG parse_tool_call: ⚠️  JSON tool call found after "
                    f"{pos} chars of preamble: «{preamble_preview}…»",
                    flush=True,
                )
            return obj, None
        if isinstance(obj, dict):
            # Well-formed top-level JSON object but not a tool call (e.g. the
            # LLM echoed a previous tool *result* as text). Stop scanning —
            # later '{' positions are nested fields of this same object and
            # matching them risks treating `{"agents":[{"tool":"..."}]}` as a
            # real call. Treat this turn as a final text response.
            print(f"DEBUG parse_tool_call: ✅ non-tool JSON at pos={pos}, keys={list(obj.keys())[:5]} → returning (None, None)", flush=True)
            return None, None

    return None, None







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

    # Resolve agent
    active_agent = agent_override if agent_override is not None else _resolve_agent_by_id(agent_id)
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
        custom_tools = load_custom_tools()
        print(f"DEBUG RUN_AGENT: start agent_id={agent_id_for_session}, sessions={list(server_module.agent_sessions.keys())}", flush=True)
        all_tools, tool_schema_map, ollama_tools, tools_json = await aggregate_all_tools(
            server_module.agent_sessions, active_agent, custom_tools
        )
        print(f"DEBUG RUN_AGENT: aggregate_all_tools done, tool_count={len(all_tools)}", flush=True)
    allowed_tools = list(allowed_tools_override) if allowed_tools_override else active_agent.get("tools", ["all"])

    system_prompt_text = build_system_prompt(
        agent_system_template, tools_json, session_id,
        _get_session_state, server_module.memory_store, agent_id=agent_id_for_session,
        turns_remaining=max_turns, max_turns=max_turns,
        inject_tools=inject_tools_in_prompt,
    )

    # Inject orchestration-awareness block when called from an orchestration step
    if system_prompt_extra:
        system_prompt_text = system_prompt_text + "\n\n" + system_prompt_extra

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
    agent_type = active_agent.get("type", "conversational")
    # Orchestrator and builder agents always start fresh — no history carryover between runs
    is_orchestrator = agent_type in ("orchestrator", "builder")
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

            # Emit LLM thought for frontend (before tool parsing so UI can show reasoning)
            if llm_output.strip():
                yield {"type": "llm_thought", "thought": llm_output, "turn": turn + 1}

            # Parse every tool call the LLM emitted in this response and execute
            # them sequentially before the next LLM turn.  This prevents partial
            # saves when a provider batches multiple calls into one response.
            tool_calls = parse_all_tool_calls(llm_output)

            if not tool_calls:
                final_response = llm_output
                break

            # Append the LLM's full reasoning to context once, before any execution,
            # so the next turn has the chain-of-thought (including the tool-call JSON).
            if llm_output.strip():
                current_context_text += f"\nAssistant Thought: {llm_output}\n"

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

                # ===== BUILT-IN EXECUTOR OVERRIDE (e.g. builder) =====
                # Checked before MCP/custom routing; returns None to fall through.

                if tool_executor is not None:
                    _exec_result = await tool_executor(tool_name, tool_args)
                    if _exec_result is not None:
                        raw_output = maybe_vault(tool_name, _exec_result)
                        current_context_text += f"\nTool '{tool_name}' Output: {raw_output}\n"
                        tools_used_summary.append(f"{tool_name}: {raw_output}")
                        preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                        print(f"DEBUG: 🔧 Executor result ({tool_name}): {preview}")
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                        if post_tool_hook is not None:
                            async for _extra in post_tool_hook(tool_name, raw_output):
                                yield _extra
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
                    current_context_text += f"\nTool '{tool_name}' Output: {raw_output}\n"
                    tools_used_summary.append(f"{tool_name}: {raw_output}")
                    preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                    print(f"DEBUG: 🏗 Builder tool ({tool_name}): {preview}")
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                    if post_tool_hook is not None:
                        async for _extra in post_tool_hook(tool_name, raw_output):
                            yield _extra
                    continue

                # ===== CUSTOM TOOLS (Webhook + Python) =====

                if tool_name not in server_module.tool_router:
                    custom_tools_list = load_custom_tools()
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
                                        "--network", "none",
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
                                current_context_text += f"\nTool '{tool_name}' Output: {raw_output}\n"
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
                            method = target_tool.get("method", "POST")
                            url = target_tool.get("url")
                            headers = target_tool.get("headers", {})
                            if not url:
                                raise ValueError("No URL configured for this tool.")

                            resp = await client.request(method, url, json=tool_args, headers=headers, timeout=30.0)

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

                            current_context_text += f"\nTool '{tool_name}' Output: {raw_output}\n"
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
                        yield {"type": "tool_result", "tool_name": tool_name, "preview": f"Error: session unresponsive"}
                        continue

                    print(f"DEBUG MCP: ✓ ping OK [{round(time.time()-_mcp_t0,2)}s] — call_tool starting", flush=True)

                    _timeout = timedelta(seconds=45) if actual_tool_name.startswith("browser_") else timedelta(seconds=30)

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
                        current_context_text += f"\nTool '{tool_name}' Output: {raw_output}\n"
                        print(f"DEBUG: 📤 Tool Result ({tool_name}): {raw_output}")

                    # MCP tool results are no longer embedded in ChromaDB
                    preview = raw_output[:500] + "..." if len(raw_output) > 500 else raw_output
                    yield {"type": "tool_result", "tool_name": tool_name, "preview": preview}
                    if post_tool_hook is not None:
                        async for _extra in post_tool_hook(tool_name, raw_output):
                            yield _extra
                    tools_used_summary.append(f"{tool_name}: {raw_output}")
                except Exception as e:
                    error_msg = str(e)
                    print(f"DEBUG: ❌ Tool '{tool_name}' failed: {error_msg}", flush=True)
                    current_context_text += f"\nSystem: Error executing tool {tool_name}: {error_msg}\n"
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

    # Resolve active agent
    active_agent = _resolve_agent_by_id(request.agent_id)

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
