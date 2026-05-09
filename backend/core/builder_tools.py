"""
Builder tools for the Synapse AI Builder agent.

Provides CRUD operations on agents and orchestrations that the builder
meta-agent can call to design and create multi-agent workflows.
"""
import json
import os
import random
import string
import time
import datetime
from typing import Any

from core.config import DATA_DIR
from core.json_store import JsonStore

_repos_store = JsonStore(os.path.join(DATA_DIR, "repos.json"), cache_ttl=5.0)
_db_store = JsonStore(os.path.join(DATA_DIR, "db_configs.json"), cache_ttl=5.0)
_mcp_store = JsonStore(os.path.join(DATA_DIR, "mcp_servers.json"), cache_ttl=5.0)


def _random_id(prefix: str, length: int = 7) -> str:
    chars = string.ascii_lowercase + string.digits
    return prefix + ''.join(random.choices(chars, k=length))


# ─── JSON-string boundary ────────────────────────────────────────────────────
# Gemini's function-calling layer hangs on deeply-nested object schemas (even
# typed ones). The saver's step objects have ~25 optional fields and appear as
# array items of `steps`/`patch` — this consistently stalls turn 1. The fix is
# to pass steps AND state_schema across the tool boundary as JSON-encoded
# STRINGS, not nested objects. Gemini generates strings natively and reliably;
# Python parses them server-side before persistence. All complexity lives in
# the system prompt's examples, not the schema itself.

STEPS_JSON_DESCRIPTION = (
    "JSON-encoded array of step objects. Each step is an object with "
    "{id, name, type, ...type-specific fields}. See the step-type palette "
    "in your system prompt for the full field list per type. Keep each step "
    "lean — include only fields that differ from zero-defaults.\n"
    "Sub-structures MUST be JSON strings inside each step: route_map_json, "
    "route_descriptions_json, parallel_branches_json, human_fields_json, "
    "switch_cases_json.\n"
    "Minimal example (2 agent steps):\n"
    "'[{\"id\":\"step_abc1234\",\"name\":\"Analyse\",\"type\":\"agent\","
    "\"agent_id\":\"agent_123\",\"prompt_template\":\"Summarise {state.user_input}\","
    "\"input_keys\":[\"user_input\"],\"output_key\":\"summary\","
    "\"next_step_id\":\"step_def5678\"},"
    "{\"id\":\"step_def5678\",\"name\":\"Done\",\"type\":\"end\"}]'"
)

PATCH_JSON_DESCRIPTION = (
    "JSON-encoded object with the step fields you want to change. Omit any "
    "field you don't want touched. Use *_json string keys for sub-structures "
    "(route_map_json, parallel_branches_json, human_fields_json, "
    "route_descriptions_json). "
    "Example: '{\"prompt_template\":\"New prompt\",\"next_step_id\":\"step_xyz7890\"}'"
)

STATE_SCHEMA_JSON_DESCRIPTION = (
    "JSON-encoded state-schema object mapping each state key to "
    "{type, default, description}. Allowed types: str, int, float, bool, list, dict. "
    "Example: '{\"user_input\":{\"type\":\"str\",\"default\":\"\",\"description\":\"Initial user message\"}}'. "
    "Pass '{}' (empty object JSON) when the orchestration has no upfront state keys."
)


# ─── Tool Schemas ─────────────────────────────────────────────────────────────

BUILDER_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_agents",
            "description": "List all available agents with their id, name, type, and tool count. Use this to understand what agents exist before building an orchestration.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent",
            "description": "Get the full configuration of a specific agent by ID, including its system prompt, tools, and model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The agent ID (e.g. agent_1774089682630)"}
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_agent",
            "description": (
                "Create a new agent and save it. Returns the created agent with its new ID. "
                "Use type='conversational' for general-purpose agents, 'code' for agents that work with repos/files, "
                "'orchestrator' for agents that run orchestrations. "
                "Set tools=['all'] to give access to all tools, or list specific tool names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name for the agent"},
                    "description": {"type": "string", "description": "What this agent does"},
                    "type": {
                        "type": "string",
                        "enum": ["conversational", "code", "orchestrator"],
                        "description": "Agent type",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tool names, or ['all'] for all tools",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "Detailed system instructions for the agent. Be thorough.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional LLM model override (e.g. claude-opus-4-6, gemini-2.5-pro). Leave null for system default.",
                    },
                    "repos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of repo IDs (for code agents). Use list_repos to find IDs.",
                    },
                    "db_configs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of DB config IDs (for agents needing database access).",
                    },
                },
                "required": ["name", "description", "type", "tools", "system_prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_agents",
            "description": (
                "Create multiple agents at once and save them. Returns the created agents with their new IDs. "
                "Prefer this over calling `create_agent` repeatedly when the plan needs more than one new agent — "
                "one tool call instead of many avoids context bloat and timeout risk. "
                "Each input item supports the same fields as `create_agent` plus a `role` string that is echoed "
                "back in the response so callers can key results by role."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "description": "Array of agent specs to create. Each item must include name, description, type, tools, system_prompt. Optional: role, model, repos, db_configs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "description": "Role label from the plan (echoed back; not persisted on the agent).",
                                },
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "type": {
                                    "type": "string",
                                    "enum": ["conversational", "code", "orchestrator"],
                                },
                                "tools": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "system_prompt": {"type": "string"},
                                "model": {"type": "string"},
                                "repos": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "List of repo IDs (use list_repos to find IDs).",
                                },
                                "db_configs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["name", "description", "type", "tools", "system_prompt"],
                        },
                    },
                },
                "required": ["agents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_agent",
            "description": "Update specific fields of an existing agent. Only the fields you provide will be changed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "fields": {
                        "type": "object",
                        "description": "Fields to update (name, description, tools, system_prompt, model, etc.)",
                    },
                },
                "required": ["agent_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_all_tools",
            "description": (
                "List available tools across MCP servers and custom tools. "
                "Pass server_name to scope the results to a single server (e.g. 'Google Workspace', 'ext_mcp_github') — "
                "use list_tool_servers first to find exact server names. "
                "Pass limit to cap the number of results (default 50). "
                "Prefer scoped calls (server_name + small limit) over unscoped calls to avoid flooding context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Optional: return only tools from this server. Use names from list_tool_servers.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max tools to return (default 50). Use 20-30 for browsing a single server.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tools_detail",
            "description": (
                "Get the full details (name, description, and input schema) for a list of tool names. "
                "Use this after list_all_tools to inspect exact parameter schemas before assigning tools to agents or tool steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tool names to fetch full details for (e.g. [\"brave_search\", \"read_file\"])",
                    }
                },
                "required": ["tool_names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tool_servers",
            "description": "List all MCP tool servers (native and externally configured) with their names, types, connection status, and tool count. Call this first to discover what integrations are available before using list_server_tools.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_server_tools",
            "description": (
                "List all tools provided by a specific MCP server by name (use list_tool_servers first to find names). "
                "Returns tool names and short descriptions. Use this for targeted tool discovery before calling "
                "get_tools_detail for exact parameter schemas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_name": {
                        "type": "string",
                        "description": "Server name as returned by list_tool_servers (e.g. 'Google Workspace')",
                    }
                },
                "required": ["server_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_repos",
            "description": "List all configured code repositories that can be assigned to code agents.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_db_configs",
            "description": "List all configured database connections that can be assigned to agents.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_orchestrations",
            "description": "List all existing orchestrations with their id, name, description, and step count.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orchestration",
            "description": "Get the full configuration of an orchestration including all steps and their connections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string", "description": "Orchestration ID"}
                },
                "required": ["orch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_orchestration",
            "description": (
                "Create a complete orchestration workflow in ONE call. Returns the saved orchestration with its id. "
                "Prefer create_orchestration_skeleton + add_steps for plans with more than 3 steps — "
                "splitting the build avoids giant tool-call payloads. Use this single-shot form only for "
                "trivial orchestrations (≤3 steps). Steps are passed as a JSON-encoded string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "steps_json": {
                        "type": "string",
                        "description": STEPS_JSON_DESCRIPTION,
                    },
                    "entry_step_id": {"type": "string", "description": "ID of the first step to execute"},
                    "state_schema_json": {
                        "type": "string",
                        "description": STATE_SCHEMA_JSON_DESCRIPTION,
                    },
                    "max_total_turns": {"type": "integer", "description": "Global turn limit (default 100)"},
                    "timeout_minutes": {"type": "integer", "description": "Overall timeout (default 30)"},
                },
                "required": ["name", "steps_json", "entry_step_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_orchestration",
            "description": (
                "Replace an existing orchestration's fields. Prefer set_orchestration_meta / "
                "add_steps / update_step / remove_step for targeted diffs — this full-replace form is the "
                "fallback for sweeping rewrites only. steps_json REPLACES the entire step list when supplied."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "entry_step_id": {"type": "string"},
                    "steps_json": {
                        "type": "string",
                        "description": "Optional. " + STEPS_JSON_DESCRIPTION,
                    },
                    "state_schema_json": {
                        "type": "string",
                        "description": STATE_SCHEMA_JSON_DESCRIPTION,
                    },
                    "max_total_turns": {"type": "integer"},
                    "timeout_minutes": {"type": "integer"},
                },
                "required": ["orch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_orchestration_skeleton",
            "description": (
                "Create an empty orchestration (no steps yet) and return its orch_id. "
                "Follow up with add_steps calls to populate steps incrementally — this is the preferred "
                "pattern for any orchestration with more than ~3 steps because each tool call stays small "
                "and well-typed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "entry_step_id": {
                        "type": "string",
                        "description": "Step id of the first step that will be added. May be set before the step itself exists.",
                    },
                    "state_schema_json": {
                        "type": "string",
                        "description": STATE_SCHEMA_JSON_DESCRIPTION,
                    },
                    "max_total_turns": {"type": "integer"},
                    "timeout_minutes": {"type": "integer"},
                },
                "required": ["name", "entry_step_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_steps",
            "description": (
                "Append a batch of steps to an existing orchestration. Keep batches small (≤5 steps) so "
                "each tool-call payload stays lean. Existing step ids in the orchestration are preserved "
                "— this is additive only. Duplicate step ids are rejected. Steps are passed as a "
                "JSON-encoded string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "steps_json": {
                        "type": "string",
                        "description": STEPS_JSON_DESCRIPTION,
                    },
                },
                "required": ["orch_id", "steps_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_orchestration_meta",
            "description": (
                "Patch top-level orchestration metadata only. Does NOT touch the steps list. Use this for "
                "renames, description edits, entry_step_id changes, state_schema updates, or turn/timeout "
                "budget changes. Pass only the fields you want to change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "entry_step_id": {"type": "string"},
                    "state_schema_json": {
                        "type": "string",
                        "description": STATE_SCHEMA_JSON_DESCRIPTION,
                    },
                    "max_total_turns": {"type": "integer"},
                    "timeout_minutes": {"type": "integer"},
                },
                "required": ["orch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_step",
            "description": (
                "Merge a partial step patch into an existing step. Only fields present in patch_json are "
                "changed; all other step fields are preserved. Use this for small edits like changing a "
                "prompt_template, adjusting an evaluator's route_map, or retargeting next_step_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "patch_json": {
                        "type": "string",
                        "description": PATCH_JSON_DESCRIPTION,
                    },
                },
                "required": ["orch_id", "step_id", "patch_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_step",
            "description": "Delete a single step from an orchestration by its step_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "step_id": {"type": "string"},
                },
                "required": ["orch_id", "step_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_orchestration",
            "description": (
                "Run wiring checks on an orchestration and return {ok, issues}. Checks: entry_step_id "
                "resolves, no duplicate ids, every route_map target + next_step_id + loop_step_ids + "
                "parallel_branches element resolves to an existing step (empty string = end), every "
                "reachable path terminates at an `end` step, and every step's input_keys are either "
                "`user_input` / `user_query` or the `output_key` of some step in the orchestration. Call "
                "this after adding/updating steps; fix any reported issues with update_step/add_steps/"
                "remove_step, then re-validate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                },
                "required": ["orch_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_single_step",
            "description": (
                "Add ONE step to an orchestration using flat parameters — no JSON encoding needed "
                "for most fields. Only route_map_json, route_descriptions_json, parallel_branches_json, "
                "and human_fields_json are JSON strings. Preferred over add_steps for reliability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string", "description": "Orchestration ID from create_orchestration_skeleton"},
                    "step_id": {"type": "string", "description": "Unique step ID (step_ + 7 alphanumeric chars)"},
                    "name": {"type": "string", "description": "Human-readable step name"},
                    "type": {
                        "type": "string",
                        "enum": ["agent", "llm", "evaluator", "parallel", "merge", "human", "tool", "loop", "transform", "extract_json", "if_else", "switch", "print", "end"],
                        "description": "Step type",
                    },
                    "agent_id": {"type": "string", "description": "Agent ID (required for agent/tool steps)"},
                    "prompt_template": {"type": "string", "description": "Prompt with {state.key} placeholders (for agent/llm steps)"},
                    "input_keys": {
                        "type": "array", "items": {"type": "string"},
                        "description": "State keys this step reads",
                    },
                    "output_key": {"type": "string", "description": "State key this step writes to"},
                    "next_step_id": {"type": "string", "description": "Next step ID. NOT used for evaluator/if_else/switch steps."},
                    "evaluator_prompt": {"type": "string", "description": "Routing instructions for evaluator steps"},
                    "route_map_json": {
                        "type": "string",
                        "description": "JSON string mapping route labels to step IDs. Example: '{\"approved\":\"step_abc\",\"rejected\":\"step_def\"}'",
                    },
                    "route_descriptions_json": {
                        "type": "string",
                        "description": "JSON string mapping route labels to descriptions. Example: '{\"approved\":\"Ready to go\",\"rejected\":\"Needs work\"}'",
                    },
                    "parallel_branches_json": {
                        "type": "string",
                        "description": "JSON string of branch arrays. Example: '[[\"step_a\"],[\"step_b\"]]'",
                    },
                    "human_prompt": {"type": "string", "description": "Prompt shown to user (human steps)"},
                    "human_fields_json": {
                        "type": "string",
                        "description": "JSON string of field definitions. Example: '[{\"name\":\"feedback\",\"type\":\"textarea\",\"label\":\"Your feedback\"}]'",
                    },
                    "forced_tool": {"type": "string", "description": "Tool name for tool steps"},
                    "merge_strategy": {
                        "type": "string", "enum": ["concat", "list", "dict"],
                        "description": "How to combine inputs (merge steps)",
                    },
                    "loop_step_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Step IDs in loop body (loop steps)",
                    },
                    "loop_count": {"type": "integer", "description": "Number of loop iterations"},
                    "transform_code": {"type": "string", "description": "Python code: reads `state`, assigns `result` (transform steps)"},
                    # ── New deterministic step fields ──────────────────────────
                    "print_content": {
                        "type": "string",
                        "description": (
                            "Markdown or plain text to store in output_key (print steps). "
                            "Use {state.key} or {state.key.nested} to embed shared state values."
                        ),
                    },
                    "if_condition": {
                        "type": "string",
                        "description": (
                            "Python expression evaluated against shared state (if_else steps). "
                            "Use dot-notation: e.g. `state.result.flag == True` or `state.score > 5`. "
                            "Missing keys are treated as None."
                        ),
                    },
                    "if_true_step_id": {"type": "string", "description": "Step to go to when if_condition is True (if_else steps)"},
                    "if_false_step_id": {"type": "string", "description": "Step to go to when if_condition is False (if_else steps)"},
                    "switch_expression": {
                        "type": "string",
                        "description": (
                            "Python expression whose str() result is matched against switch_cases (switch steps). "
                            "Example: `state.category` or `state.result.status.lower()`."
                        ),
                    },
                    "switch_cases_json": {
                        "type": "string",
                        "description": (
                            "JSON string mapping string values to step IDs (switch steps). "
                            "Example: '{\"approved\":\"step_abc\",\"rejected\":\"step_def\"}'. "
                            "Unmatched values fall through to switch_default_step_id."
                        ),
                    },
                    "switch_default_step_id": {"type": "string", "description": "Fallback step when no switch case matches"},
                    # ──────────────────────────────────────────────────────────
                    "max_turns": {"type": "integer", "description": "Max agent turns (default 15)"},
                    "timeout_seconds": {"type": "integer", "description": "Step timeout (default 300)"},
                    "model": {"type": "string", "description": "LLM model override for this step"},
                },
                "required": ["orch_id", "step_id", "name", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_multiple_steps",
            "description": (
                "Add multiple steps to an orchestration in one call. Each step uses flat fields "
                "(same as add_single_step). Batch up to 5 plain steps (agent, llm, end, merge) per "
                "call. For evaluator/parallel/human steps, prefer add_single_step (one at a time)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string", "description": "Orchestration ID"},
                    "steps": {
                        "type": "array",
                        "description": "Array of step objects. Each has the same fields as add_single_step.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_id": {"type": "string", "description": "Unique step ID (step_ + 7 alphanumeric)"},
                                "name": {"type": "string", "description": "Step name"},
                                "type": {
                                    "type": "string",
                                    "enum": ["agent", "llm", "evaluator", "parallel", "merge", "human", "tool", "loop", "transform", "extract_json", "if_else", "switch", "print", "end"],
                                },
                                "agent_id": {"type": "string"},
                                "prompt_template": {"type": "string"},
                                "input_keys": {"type": "array", "items": {"type": "string"}},
                                "output_key": {"type": "string"},
                                "next_step_id": {"type": "string"},
                                "evaluator_prompt": {"type": "string"},
                                "route_map_json": {"type": "string", "description": "JSON string: '{\"label\":\"step_id\"}'"},
                                "route_descriptions_json": {"type": "string", "description": "JSON string: '{\"label\":\"desc\"}'"},
                                "parallel_branches_json": {"type": "string", "description": "JSON string: '[[\"step_a\"],[\"step_b\"]]'"},
                                "human_prompt": {"type": "string"},
                                "human_fields_json": {"type": "string"},
                                "forced_tool": {"type": "string"},
                                "merge_strategy": {"type": "string", "enum": ["concat", "list", "dict"]},
                                "loop_step_ids": {"type": "array", "items": {"type": "string"}},
                                "loop_count": {"type": "integer"},
                                "transform_code": {"type": "string"},
                                "print_content": {"type": "string", "description": "Markdown/text for print steps. Supports {state.key} interpolation."},
                                "if_condition": {"type": "string", "description": "Python condition for if_else steps (e.g. `state.flag == True`)"},
                                "if_true_step_id": {"type": "string", "description": "Step ID when condition is True"},
                                "if_false_step_id": {"type": "string", "description": "Step ID when condition is False"},
                                "switch_expression": {"type": "string", "description": "Expression to evaluate for switch steps"},
                                "switch_cases_json": {"type": "string", "description": "JSON string: '{\"value\":\"step_id\"}'"},
                                "switch_default_step_id": {"type": "string", "description": "Fallback step when no case matches"},
                                "max_turns": {"type": "integer"},
                                "timeout_seconds": {"type": "integer"},
                                "model": {"type": "string"},
                            },
                            "required": ["step_id", "name", "type"],
                        },
                    },
                },
                "required": ["orch_id", "steps"],
            },
        },
    },
]


# Set of all builder-tool names. Consumed by tool dispatch paths
# (react_engine, ToolStepExecutor) to route these to execute_builder_tool
# instead of MCP / custom-tool execution, and by aggregate_all_tools to
# expose them as first-class tools that any agent can declare.
BUILDER_TOOL_NAMES = {t["function"]["name"] for t in BUILDER_TOOL_SCHEMAS}


# ─── Tool Implementations ──────────────────────────────────────────────────────

async def execute_builder_tool(tool_name: str, args: dict, server_module: Any) -> str:
    """Dispatch a builder tool call and return a JSON string result."""
    try:
        result = await _dispatch(tool_name, args, server_module)
        return json.dumps(result, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _dispatch(tool_name: str, args: dict, server_module: Any) -> Any:
    from core.routes.agents import load_user_agents, save_user_agents
    from core.routes.orchestrations import load_orchestrations, save_orchestrations

    if tool_name == "list_agents":
        agents = load_user_agents()
        return [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "type": a.get("type"),
                "description": a.get("description", ""),
                "tool_count": len(a.get("tools", [])),
                "model": a.get("model"),
            }
            for a in agents
            if a.get("type") != "builder"  # hide the builder itself
        ]

    elif tool_name == "get_agent":
        agents = load_user_agents()
        agent = next((a for a in agents if a["id"] == args["agent_id"]), None)
        if not agent:
            return {"error": f"Agent '{args['agent_id']}' not found"}
        return agent

    elif tool_name == "create_agent":
        agents = load_user_agents()
        new_id = f"agent_{int(time.time() * 1000)}"
        agent = {
            "id": new_id,
            "name": args["name"],
            "description": args.get("description", ""),
            "avatar": "default",
            "type": args.get("type", "conversational"),
            "tools": args.get("tools", ["all"]),
            "repos": args.get("repos", []),
            "db_configs": args.get("db_configs", []),
            "system_prompt": args.get("system_prompt", ""),
            "orchestration_id": None,
            "model": args.get("model") or None,
            "provider": None,
            "max_turns": None,
        }
        agents.append(agent)
        save_user_agents(agents)
        return {"status": "created", "agent": agent}

    elif tool_name == "create_agents":
        specs = args.get("agents") or []
        if not isinstance(specs, list) or not specs:
            return {"error": "agents must be a non-empty array of agent specs"}
        agents = load_user_agents()
        existing_ids = {a.get("id") for a in agents}
        created = []
        errors = []
        for i, spec in enumerate(specs):
            if not isinstance(spec, dict):
                errors.append({"index": i, "error": "spec is not an object"})
                continue
            for field in ("name", "description", "type", "tools", "system_prompt"):
                if field not in spec:
                    errors.append({"index": i, "role": spec.get("role"), "error": f"missing required field '{field}'"})
                    break
            else:
                # Guarantee uniqueness even when called repeatedly in the same millisecond.
                new_id = _random_id("agent_", length=10)
                while new_id in existing_ids:
                    new_id = _random_id("agent_", length=10)
                existing_ids.add(new_id)
                agent = {
                    "id": new_id,
                    "name": spec["name"],
                    "description": spec.get("description", ""),
                    "avatar": "default",
                    "type": spec.get("type", "conversational"),
                    "tools": spec.get("tools", ["all"]),
                    "repos": spec.get("repos", []) or [],
                    "db_configs": spec.get("db_configs", []) or [],
                    "system_prompt": spec.get("system_prompt", ""),
                    "orchestration_id": None,
                    "model": spec.get("model") or None,
                    "provider": None,
                    "max_turns": None,
                }
                agents.append(agent)
                created.append({
                    "role": spec.get("role"),
                    "id": new_id,
                    "name": agent["name"],
                    "type": agent["type"],
                    "repos": agent["repos"],
                })
        if created:
            save_user_agents(agents)
        return {"status": "created", "agents": created, "errors": errors}

    elif tool_name == "update_agent":
        agents = load_user_agents()
        idx = next((i for i, a in enumerate(agents) if a["id"] == args["agent_id"]), None)
        if idx is None:
            return {"error": f"Agent '{args['agent_id']}' not found"}
        agents[idx].update(args.get("fields", {}))
        save_user_agents(agents)
        return {"status": "updated", "agent": agents[idx]}

    elif tool_name == "list_all_tools":
        server_filter = (args.get("server_name") or "").strip()
        limit = int(args.get("limit") or 50)
        try:
            if server_filter:
                # Scoped: query the specific server's session directly
                session = server_module.agent_sessions.get(server_filter) or \
                          server_module.agent_sessions.get(f"ext_mcp_{server_filter}")
                if not session:
                    lowered = server_filter.lower().replace(" ", "_")
                    for key in server_module.agent_sessions:
                        if lowered in key.lower():
                            session = server_module.agent_sessions[key]
                            break
                if not session:
                    return {"error": f"Server '{server_filter}' not found. Call list_tool_servers to see available names."}
                tools_result = await session.list_tools()
                return [
                    {"name": t.name, "description": (t.description or "")[:120]}
                    for t in tools_result.tools[:limit]
                ]
            else:
                from core.tools import aggregate_all_tools
                from core.routes.tools import load_custom_tools
                from core.routes.agents import load_user_agents as _lau
                _agents = _lau()
                active_agent = next((a for a in _agents if a.get("type") != "builder"), _agents[0] if _agents else {})
                custom_tools = load_custom_tools()
                all_tools, _, _, _ = await aggregate_all_tools(
                    server_module.agent_sessions, active_agent, custom_tools
                )
                return [
                    {"name": t.name, "description": (t.description or "")[:120]}
                    for t in all_tools[:limit]
                ]
        except Exception as e:
            return {"error": f"Could not list tools: {e}"}

    elif tool_name == "get_tools_detail":
        try:
            from core.tools import aggregate_all_tools
            from core.routes.tools import load_custom_tools
            from core.routes.agents import load_user_agents as _lau
            _agents = _lau()
            active_agent = next((a for a in _agents if a.get("type") != "builder"), _agents[0] if _agents else {})
            custom_tools = load_custom_tools()
            all_tools, _, _, _ = await aggregate_all_tools(
                server_module.agent_sessions, active_agent, custom_tools
            )
            requested = set(args.get("tool_names", []))
            result = {}
            for t in all_tools:
                if t.name in requested:
                    result[t.name] = {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                    }
            missing = requested - result.keys()
            if missing:
                result["_not_found"] = sorted(missing)
            return result
        except Exception as e:
            return {"error": f"Could not fetch tool details: {e}"}

    elif tool_name == "list_tool_servers":
        ext_servers = _mcp_store.load()
        if not isinstance(ext_servers, list):
            ext_servers = []
        result = [
            {
                "name": s.get("name"),
                "label": s.get("label", s.get("name")),
                "type": s.get("server_type", "stdio"),
                "status": s.get("status", "unknown"),
                "tool_count": None,
            }
            for s in ext_servers
        ]
        # Also include MCP sessions started programmatically (not in mcp_store.json).
        # Sessions keyed as "ext_mcp_<name>" are external — strip the prefix and
        # mark them as external so callers know to use "name__tool_name" format.
        # Sessions without that prefix are native (no tool-name prefix needed).
        ext_keys = {f"ext_mcp_{s.get('name')}" for s in ext_servers}
        for sess_name, session in server_module.agent_sessions.items():
            if sess_name in ext_keys:
                continue
            try:
                tools_result = await session.list_tools()
                tool_count = len(tools_result.tools)
            except Exception:
                tool_count = 0
            is_ext = sess_name.startswith("ext_mcp_")
            display_name = sess_name[len("ext_mcp_"):] if is_ext else sess_name
            result.append({
                "name": display_name,
                "label": display_name.replace("_", " ").title(),
                "type": "external_mcp" if is_ext else "native_mcp",
                "status": "running",
                "tool_count": tool_count,
            })
        return result

    elif tool_name == "list_server_tools":
        server_name = args.get("server_name", "")
        session = server_module.agent_sessions.get(server_name) or \
                  server_module.agent_sessions.get(f"ext_mcp_{server_name}")
        if not session:
            lowered = server_name.lower().replace(" ", "_")
            for key in server_module.agent_sessions:
                if lowered in key.lower():
                    session = server_module.agent_sessions[key]
                    break
        if not session:
            return {"error": f"Server '{server_name}' not found. Call list_tool_servers to see available names."}
        try:
            tools_result = await session.list_tools()
            return [
                {"name": t.name, "description": (t.description or "")[:150]}
                for t in tools_result.tools
            ]
        except Exception as e:
            return {"error": f"Could not list tools for server '{server_name}': {e}"}

    elif tool_name == "list_repos":
        repos = _repos_store.load()
        if not isinstance(repos, list):
            return []
        return [
            {"id": r.get("id"), "name": r.get("name", r.get("path", "")), "path": r.get("path", "")}
            for r in repos
        ]

    elif tool_name == "list_db_configs":
        dbs = _db_store.load()
        if not isinstance(dbs, list):
            return []
        return [
            {"id": d.get("id"), "name": d.get("name", ""), "type": d.get("type", "")}
            for d in dbs
        ]

    elif tool_name == "list_orchestrations":
        orchs = load_orchestrations()
        return [
            {
                "id": o.get("id"),
                "name": o.get("name"),
                "description": o.get("description", ""),
                "step_count": len(o.get("steps", [])),
            }
            for o in orchs
        ]

    elif tool_name == "get_orchestration":
        orchs = load_orchestrations()
        orch = next((o for o in orchs if o["id"] == args["orch_id"]), None)
        if not orch:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        return orch

    elif tool_name == "create_orchestration":
        orchs = load_orchestrations()
        orch_id = _random_id("orch_")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        _normalize_state_schema_arg(args)
        _normalize_steps_arg(args)
        steps = args.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return {"error": "steps_json must decode to a non-empty JSON array of step objects"}
        steps = _fill_step_defaults(steps)

        orch = {
            "id": orch_id,
            "name": args["name"],
            "description": args.get("description", ""),
            "avatar": "default",
            "steps": steps,
            "entry_step_id": args["entry_step_id"],
            "state_schema": args.get("state_schema", {}),
            "max_total_turns": args.get("max_total_turns", 100),
            "max_total_cost_usd": None,
            "timeout_minutes": args.get("timeout_minutes", 30),
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        orchs.append(orch)
        save_orchestrations(orchs)
        return {"status": "created", "orchestration": orch}

    elif tool_name == "update_orchestration":
        orchs = load_orchestrations()
        idx = next((i for i, o in enumerate(orchs) if o["id"] == args["orch_id"]), None)
        if idx is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        # update_orchestration now accepts flat top-level fields (like the other
        # builder tools), falling back to legacy `fields` for back-compat.
        legacy_fields = args.get("fields") or {}
        fields = dict(legacy_fields)
        for key in ("name", "description", "entry_step_id", "state_schema_json",
                    "steps_json", "max_total_turns", "timeout_minutes"):
            if key in args and args[key] is not None:
                fields[key] = args[key]
        _normalize_state_schema_arg(fields)
        _normalize_steps_arg(fields)
        if "steps" in fields and isinstance(fields["steps"], list):
            fields["steps"] = _fill_step_defaults(fields["steps"])
        if not fields or set(fields.keys()) - {"orch_id"} == set():
            return {"error": "no updatable fields supplied"}
        # Drop orch_id if it snuck in
        fields.pop("orch_id", None)
        orchs[idx].update(fields)
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {"status": "updated", "orchestration": orchs[idx]}

    elif tool_name == "create_orchestration_skeleton":
        orchs = load_orchestrations()
        orch_id = _random_id("orch_")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _normalize_state_schema_arg(args)
        orch = {
            "id": orch_id,
            "name": args["name"],
            "description": args.get("description", ""),
            "avatar": "default",
            "steps": [],
            "entry_step_id": args["entry_step_id"],
            "state_schema": args.get("state_schema", {}),
            "max_total_turns": args.get("max_total_turns", 100),
            "max_total_cost_usd": None,
            "timeout_minutes": args.get("timeout_minutes", 30),
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        orchs.append(orch)
        save_orchestrations(orchs)
        return {"status": "created", "orchestration": orch}

    elif tool_name == "add_steps":
        orchs = load_orchestrations()
        idx = next((i for i, o in enumerate(orchs) if o["id"] == args["orch_id"]), None)
        if idx is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        raw_steps = args.get("steps_json", args.get("steps", ""))
        _normalize_steps_arg(args)
        new_steps = args.get("steps", []) or []
        if not isinstance(new_steps, list) or not new_steps:
            # Give the LLM a preview of what it sent so it can self-correct
            preview = str(raw_steps)[:300] if raw_steps else "(empty)"
            return {
                "error": f"steps_json must decode to a non-empty JSON array of step objects. "
                         f"Received: {preview}... "
                         f"Hint: ensure all inner quotes are escaped as \\\" and inner *_json values use \\\\\\\" for their quotes."
            }
        existing = orchs[idx].get("steps", []) or []
        existing_ids = {s.get("id") for s in existing}
        incoming_ids = [s.get("id") for s in new_steps if s.get("id")]
        dup = existing_ids.intersection(incoming_ids)
        if dup:
            return {"error": f"Duplicate step ids: {sorted(dup)}"}
        # Offset canvas positions so newly-added steps continue the zig-zag
        # pattern past any existing steps.
        offset = len(existing)
        filled = _fill_step_defaults(new_steps)
        for i, s in enumerate(filled):
            s["position_x"] = _POS_X_START + (offset + i) * _POS_X_STEP
            s["position_y"] = _zigzag_y(offset + i)
        orchs[idx]["steps"] = existing + filled
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {
            "status": "appended",
            "added_step_ids": [s["id"] for s in filled],
            "total_steps": len(orchs[idx]["steps"]),
        }

    elif tool_name == "add_single_step":
        # Flat-parameter tool: assemble a step dict from individual args,
        # then reuse the same pipeline as add_steps.
        orchs = load_orchestrations()
        orch_id = args.get("orch_id")
        idx = next((i for i, o in enumerate(orchs) if o["id"] == orch_id), None)
        if idx is None:
            return {"error": f"Orchestration '{orch_id}' not found"}

        step_id = args.get("step_id")
        if not step_id:
            return {"error": "step_id is required"}
        step_name = args.get("name")
        step_type = args.get("type")
        if not step_name or not step_type:
            return {"error": "name and type are required"}

        # Check duplicate
        existing = orchs[idx].get("steps", []) or []
        if any(s.get("id") == step_id for s in existing):
            return {"error": f"Duplicate step id: {step_id}"}

        # Build step dict from flat args (skip orch_id)
        step = {}
        for k, v in args.items():
            if k == "orch_id" or v is None:
                continue
            step[k] = v
        # Rename flat keys to match internal format
        step["id"] = step.pop("step_id", step_id)

        # Normalize *_json fields (route_map_json → route_map, etc.)
        step = _normalize_step_inputs(step)
        filled = _fill_step_defaults([step])
        # Position continues the zig-zag past any existing steps.
        offset = len(existing)
        filled[0]["position_x"] = _POS_X_START + offset * _POS_X_STEP
        filled[0]["position_y"] = _zigzag_y(offset)
        orchs[idx]["steps"] = existing + filled
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {
            "status": "step_added",
            "step_id": filled[0]["id"],
            "total_steps": len(orchs[idx]["steps"]),
        }

    elif tool_name == "add_multiple_steps":
        # Batch version of add_single_step — takes a native array of step objects.
        orchs = load_orchestrations()
        orch_id = args.get("orch_id")
        idx = next((i for i, o in enumerate(orchs) if o["id"] == orch_id), None)
        if idx is None:
            return {"error": f"Orchestration '{orch_id}' not found"}

        raw_steps = args.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            return {"error": "steps must be a non-empty array of step objects"}

        existing = orchs[idx].get("steps", []) or []
        existing_ids = {s.get("id") for s in existing}

        processed = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                return {"error": f"Each step must be an object, got {type(raw).__name__}"}
            sid = raw.get("step_id")
            if not sid or not raw.get("name") or not raw.get("type"):
                return {"error": f"Each step needs step_id, name, and type. Got: {raw}"}
            if sid in existing_ids:
                return {"error": f"Duplicate step id: {sid}"}
            existing_ids.add(sid)
            # Build step dict (rename step_id → id, drop None values)
            step = {k: v for k, v in raw.items() if v is not None}
            step["id"] = step.pop("step_id", sid)
            step = _normalize_step_inputs(step)
            processed.append(step)

        filled = _fill_step_defaults(processed)
        offset = len(existing)
        for i, s in enumerate(filled):
            s["position_x"] = _POS_X_START + (offset + i) * _POS_X_STEP
            s["position_y"] = _zigzag_y(offset + i)
        orchs[idx]["steps"] = existing + filled
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {
            "status": "steps_added",
            "added_step_ids": [s["id"] for s in filled],
            "total_steps": len(orchs[idx]["steps"]),
        }

    elif tool_name == "set_orchestration_meta":
        orchs = load_orchestrations()
        idx = next((i for i, o in enumerate(orchs) if o["id"] == args["orch_id"]), None)
        if idx is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        # Accept flat top-level fields (preferred); legacy `fields` wrapper still
        # works for callers that haven't migrated yet.
        raw_fields = dict(args.get("fields") or {})
        for key in ("name", "description", "entry_step_id", "state_schema_json",
                    "max_total_turns", "timeout_minutes"):
            if key in args and args[key] is not None:
                raw_fields[key] = args[key]
        _normalize_state_schema_arg(raw_fields)
        allowed = {"name", "description", "entry_step_id", "state_schema", "max_total_turns", "timeout_minutes"}
        fields = {k: v for k, v in raw_fields.items() if k in allowed}
        if not fields:
            return {"error": f"No allowed meta fields to set. Allowed: {sorted(allowed)}"}
        orchs[idx].update(fields)
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {"status": "meta_updated", "fields": list(fields.keys())}

    elif tool_name == "update_step":
        orchs = load_orchestrations()
        idx = next((i for i, o in enumerate(orchs) if o["id"] == args["orch_id"]), None)
        if idx is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        step_id = args["step_id"]
        patch = _normalize_patch_arg(args)
        if not isinstance(patch, dict) or not patch:
            return {"error": "patch_json must decode to a non-empty JSON object of step fields"}
        steps = orchs[idx].get("steps", []) or []
        sidx = next((i for i, s in enumerate(steps) if s.get("id") == step_id), None)
        if sidx is None:
            return {"error": f"Step '{step_id}' not found in orchestration '{args['orch_id']}'"}
        merged = {**steps[sidx], **patch}
        merged["id"] = step_id  # id is immutable via patch
        steps[sidx] = _fill_step_defaults([merged])[0]
        orchs[idx]["steps"] = steps
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {"status": "step_updated", "step": steps[sidx]}

    elif tool_name == "remove_step":
        orchs = load_orchestrations()
        idx = next((i for i, o in enumerate(orchs) if o["id"] == args["orch_id"]), None)
        if idx is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        step_id = args["step_id"]
        steps = orchs[idx].get("steps", []) or []
        before = len(steps)
        steps = [s for s in steps if s.get("id") != step_id]
        if len(steps) == before:
            return {"error": f"Step '{step_id}' not found in orchestration '{args['orch_id']}'"}
        orchs[idx]["steps"] = steps
        orchs[idx]["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_orchestrations(orchs)
        return {"status": "step_removed", "step_id": step_id, "remaining_steps": len(steps)}

    elif tool_name == "validate_orchestration":
        orchs = load_orchestrations()
        orch = next((o for o in orchs if o["id"] == args["orch_id"]), None)
        if not orch:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        return _validate_orchestration(orch)

    else:
        return {"error": f"Unknown tool: {tool_name}"}


def _validate_orchestration(orch: dict) -> dict:
    """Return {"ok": bool, "issues": [str, ...]} describing wiring problems.

    Checks (matches the engine's expectations in core.orchestration.engine):
      - entry_step_id resolves to a real step
      - no duplicate step ids
      - every next_step_id / route_map target / loop_step_ids entry /
        parallel_branches element resolves to a real step (empty string = end)
      - every reachable path terminates at an `end`-type step
      - every step's input_keys are either `user_input` / `user_query` or the
        `output_key` of some step in the orchestration (a key with no writer
        anywhere is meaningless — it can only ever be its schema default)
    """
    issues: list[str] = []
    steps = orch.get("steps", []) or []
    if not steps:
        return {"ok": False, "issues": ["Orchestration has no steps."]}

    by_id: dict[str, dict] = {}
    for s in steps:
        sid = s.get("id") or ""
        if not sid:
            issues.append("Found a step with no id.")
            continue
        if sid in by_id:
            issues.append(f"Duplicate step id '{sid}'.")
        by_id[sid] = s

    entry = orch.get("entry_step_id") or ""
    if not entry:
        issues.append("entry_step_id is empty.")
    elif entry not in by_id:
        issues.append(f"entry_step_id '{entry}' does not match any step.")

    def _ref_ok(ref: str | None) -> bool:
        # Empty string / None means "end of orchestration" — acceptable.
        return not ref or ref in by_id

    for s in steps:
        sid = s.get("id", "?")
        stype = s.get("type")
        nxt = s.get("next_step_id")
        if nxt and not _ref_ok(nxt):
            issues.append(f"Step '{sid}' next_step_id '{nxt}' points to unknown step.")
        for label, target in (s.get("route_map") or {}).items():
            if target and not _ref_ok(target):
                issues.append(
                    f"Step '{sid}' route_map[{label!r}] → '{target}' points to unknown step."
                )
        for lsid in s.get("loop_step_ids") or []:
            if not _ref_ok(lsid):
                issues.append(f"Step '{sid}' loop body references unknown step '{lsid}'.")
        for branch in s.get("parallel_branches") or []:
            for bsid in branch:
                if not _ref_ok(bsid):
                    issues.append(
                        f"Step '{sid}' parallel branch references unknown step '{bsid}'."
                    )
        if stype == "agent" and not s.get("agent_id"):
            issues.append(f"Agent step '{sid}' has no agent_id.")
        if stype == "evaluator" and not (s.get("route_map") or {}):
            issues.append(f"Evaluator step '{sid}' has an empty route_map.")
        if stype == "tool" and not s.get("forced_tool"):
            issues.append(f"Tool step '{sid}' has no forced_tool.")
        if stype == "human" and not s.get("human_prompt"):
            issues.append(f"Human step '{sid}' has no human_prompt.")
        if stype == "transform" and not s.get("transform_code"):
            issues.append(f"Transform step '{sid}' has no transform_code.")
        if stype == "if_else":
            if not s.get("if_condition"):
                issues.append(f"If/Else step '{sid}' has no if_condition.")
            if s.get("if_true_step_id") and not _ref_ok(s.get("if_true_step_id")):
                issues.append(f"If/Else step '{sid}' if_true_step_id points to unknown step.")
            if s.get("if_false_step_id") and not _ref_ok(s.get("if_false_step_id")):
                issues.append(f"If/Else step '{sid}' if_false_step_id points to unknown step.")
        if stype == "switch":
            if not s.get("switch_expression"):
                issues.append(f"Switch step '{sid}' has no switch_expression.")
            if not (s.get("switch_cases") or {}):
                issues.append(f"Switch step '{sid}' has no switch_cases defined.")
            for val, target in (s.get("switch_cases") or {}).items():
                if target and not _ref_ok(target):
                    issues.append(f"Switch step '{sid}' case '{val}' points to unknown step '{target}'.")
            default_id = s.get("switch_default_step_id")
            if default_id and not _ref_ok(default_id):
                issues.append(f"Switch step '{sid}' switch_default_step_id points to unknown step.")
        if stype == "extract_json" and not s.get("output_key"):
            issues.append(f"Extract JSON step '{sid}' has no output_key — extracted JSON will not be stored.")
        if stype == "print" and not s.get("print_content"):
            issues.append(f"Print step '{sid}' has no print_content.")

    # Writer-membership check: every input_key must be `user_input` /
    # `user_query` (engine-seeded) or the `output_key` of some step. Branch
    # conditioning is not the validator's concern — the engine falls back to
    # the schema default on unset reads, which is the intended pattern for
    # optional branch outputs and iterative refinement loops.
    FREE_KEYS = {"user_input", "user_query"}
    writable: set[str] = set(FREE_KEYS)
    for s in steps:
        ok = s.get("output_key")
        if ok:
            writable.add(ok)
    for s in steps:
        sid = s.get("id", "?")
        for k in s.get("input_keys") or []:
            if k not in writable:
                issues.append(
                    f"Step '{sid}' reads input_key '{k}' but no step writes it — either use an existing output_key or delete the read."
                )

    # Reachability walk: every reachable step must be able to hit an `end`.
    # Dead-end non-end steps (no next_step_id / routes / branches) are bugs.
    if entry in by_id:
        from collections import deque
        visited: set[str] = set()
        queue = deque([entry])
        while queue:
            cur = queue.popleft()
            if cur in visited:
                continue
            visited.add(cur)
            s = by_id.get(cur)
            if not s:
                continue
            if s.get("type") == "end":
                continue
            successors: list[str] = []
            nxt = s.get("next_step_id")
            if nxt and nxt in by_id:
                successors.append(nxt)
            for target in (s.get("route_map") or {}).values():
                if target and target in by_id:
                    successors.append(target)
            for lsid in s.get("loop_step_ids") or []:
                if lsid in by_id:
                    successors.append(lsid)
            for branch in s.get("parallel_branches") or []:
                for bsid in branch:
                    if bsid in by_id:
                        successors.append(bsid)
            # New branching step successors
            for ref_key in ("if_true_step_id", "if_false_step_id", "switch_default_step_id"):
                ref = s.get(ref_key)
                if ref and ref in by_id:
                    successors.append(ref)
            for target in (s.get("switch_cases") or {}).values():
                if target and target in by_id:
                    successors.append(target)
            if not successors:
                issues.append(
                    f"Step '{cur}' (type={s.get('type')}) has no next_step_id / routes / branches — path dead-ends without reaching an `end` step."
                )
                continue
            for nxt_id in successors:
                if nxt_id not in visited:
                    queue.append(nxt_id)

    return {"ok": not issues, "issues": issues}


def _parse_json_field(value: Any, fallback: Any) -> Any:
    """Parse a JSON-encoded string (as emitted by Gemini via *_json fields).
    Falls back to `fallback` on empty / malformed input so the saver
    doesn't stall on a stray trailing comma. Tries common LLM JSON repairs
    before giving up."""
    if value is None:
        return fallback
    if not isinstance(value, str):
        return value  # already parsed (e.g. passed natively by tests)
    s = value.strip()
    if not s:
        return fallback
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # ── Common LLM JSON repairs ──────────────────────────────────────────
    repaired = s
    # 1. Trailing commas before } or ]
    import re
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    # 2. Single quotes → double quotes (only for simple cases)
    if '"' not in repaired and "'" in repaired:
        repaired = repaired.replace("'", '"')
    if repaired != s:
        try:
            result = json.loads(repaired)
            print(f"DEBUG _parse_json_field: 🔧 repaired JSON (trailing commas / quotes)", flush=True)
            return result
        except json.JSONDecodeError:
            pass
    print(f"DEBUG _parse_json_field: ❌ JSON parse failed, using fallback. Input preview: {s[:200]}", flush=True)
    return fallback


def _normalize_step_inputs(step: dict) -> dict:
    """Convert `*_json` string fields emitted by the LLM into their native
    dict/list form, so downstream code (and `_fill_step_defaults`) sees a
    uniform shape. Idempotent — keys without a `*_json` sibling pass through."""
    s = dict(step)
    if "route_map_json" in s:
        s["route_map"] = _parse_json_field(s.pop("route_map_json"), {})
    if "route_descriptions_json" in s:
        s["route_descriptions"] = _parse_json_field(s.pop("route_descriptions_json"), {})
    if "parallel_branches_json" in s:
        s["parallel_branches"] = _parse_json_field(s.pop("parallel_branches_json"), [])
    if "human_fields_json" in s:
        s["human_fields"] = _parse_json_field(s.pop("human_fields_json"), [])
    if "switch_cases_json" in s:
        s["switch_cases"] = _parse_json_field(s.pop("switch_cases_json"), {})
    return s


def _normalize_state_schema_arg(args: dict) -> None:
    """In-place: if `state_schema_json` is present, parse it into `state_schema`."""
    if "state_schema_json" in args:
        args["state_schema"] = _parse_json_field(args.pop("state_schema_json"), {})


def _normalize_steps_arg(args: dict) -> None:
    """In-place: if `steps_json` is present, parse it into `steps`. Accepts
    either a JSON-encoded string (preferred — keeps Gemini's tool boundary
    flat) or an already-decoded list (back-compat with tests)."""
    if "steps_json" in args:
        raw = args["steps_json"]
        parsed = _parse_json_field(args.pop("steps_json"), [])
        if isinstance(parsed, list):
            args["steps"] = parsed
        else:
            print(f"DEBUG _normalize_steps_arg: ❌ steps_json decoded to {type(parsed).__name__}, not list. Raw preview: {str(raw)[:300]}", flush=True)
            args["steps"] = []


def _normalize_patch_arg(args: dict) -> dict:
    """Extract and parse a step patch. Prefers `patch_json` (JSON string),
    falls back to legacy `patch` (already-decoded dict). Returns the patch
    dict (possibly empty)."""
    if "patch_json" in args:
        parsed = _parse_json_field(args.pop("patch_json"), {})
        return parsed if isinstance(parsed, dict) else {}
    legacy = args.get("patch") or {}
    return legacy if isinstance(legacy, dict) else {}


# Canvas layout constants. Zig-zag keeps long flows visible within typical
# screen widths by using vertical space instead of a single long horizontal line.
_POS_X_START = -600
_POS_X_STEP = 260
_POS_Y_AMPLITUDE = 180.0


def _zigzag_y(index: int) -> float:
    """Triangle-wave y: center → up → center → down, repeating every 4 steps."""
    pattern = (0.0, -_POS_Y_AMPLITUDE, 0.0, _POS_Y_AMPLITUDE)
    return pattern[index % 4]


def _fill_step_defaults(steps: list) -> list:
    """Ensure every step has the required default fields."""
    result = []
    for i, step in enumerate(steps):
        s = _normalize_step_inputs(step)
        # Generate ID if missing
        if not s.get("id"):
            s["id"] = _random_id("step_")
        # Canvas positions: zig-zag so more steps fit in screen width.
        if "position_x" not in s:
            s["position_x"] = _POS_X_START + i * _POS_X_STEP
        if "position_y" not in s:
            s["position_y"] = _zigzag_y(i)
        # Defaults
        s.setdefault("agent_id", None)
        s.setdefault("prompt_template", None)
        s.setdefault("route_map", {})
        s.setdefault("route_descriptions", {})
        s.setdefault("evaluator_prompt", None)
        s.setdefault("model", None)
        s.setdefault("parallel_branches", [])
        s.setdefault("merge_strategy", "list")
        s.setdefault("loop_count", 3)
        s.setdefault("loop_step_ids", [])
        s.setdefault("transform_code", None)
        s.setdefault("human_prompt", None)
        s.setdefault("human_fields", [])
        s.setdefault("human_channel_id", None)
        s.setdefault("human_timeout_seconds", 3600)
        s.setdefault("input_keys", [])
        s.setdefault("output_key", None)
        s.setdefault("forced_tool", None)
        s.setdefault("max_turns", 15)
        s.setdefault("timeout_seconds", 300)
        s.setdefault("allowed_tools", None)
        s.setdefault("next_step_id", None)
        s.setdefault("max_iterations", 3)
        # New deterministic step defaults
        s.setdefault("print_content", None)
        s.setdefault("if_condition", None)
        s.setdefault("if_true_step_id", None)
        s.setdefault("if_false_step_id", None)
        s.setdefault("switch_expression", None)
        s.setdefault("switch_cases", {})
        s.setdefault("switch_default_step_id", None)
        result.append(s)
    return result


