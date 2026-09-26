"""
Builder tools for the Synapse AI Builder agent.

Provides CRUD operations on agents and orchestrations that the builder
meta-agent can call to design and create multi-agent workflows.
"""
import json
import random
import string
import time
from typing import Any

from core.state_types import (
    CANONICAL_STATE_TYPES,
    matches_declared_type,
    normalize_state_type,
)
from core.store import collections
from core.store.resources import load_mcp_servers


def _random_id(prefix: str, length: int = 7) -> str:
    chars = string.ascii_lowercase + string.digits
    return prefix + ''.join(random.choices(chars, k=length))


# ─── What the builder is allowed to see and touch ────────────────────────────
# The builder is itself a seeded orchestration with sub-agents (`native_builder`),
# and those rows live in the same store as the customer's own. They carry a
# `system` marker, and every read path here filters on it: the builder must not
# list, edit or delete its own machinery. Cloud additionally soft-deletes an
# orchestration by stamping `is_active` false, which the engine's list does not
# filter — so a retired orchestration is invisible here too, rather than being
# resurrected by the next save.

def _is_visible(resource: dict) -> bool:
    """Whether a stored agent/orchestration is workspace content."""
    if resource.get("system"):
        return False
    return resource.get("is_active", True) is not False


async def _visible_orchestration(orch_id: str | None) -> dict | None:
    """Point-load one orchestration, or None if it is missing or not ours to touch."""
    if not orch_id:
        return None
    from core.store.resources import get_orchestration
    orch = await get_orchestration(orch_id)
    if orch is None or not _is_visible(orch):
        return None
    return orch


def _tool_name(raw: Any) -> str:
    """A custom tool's name is an identifier the model types into a tool call."""
    name = str(raw or "").strip()
    return name if name and name.replace("_", "a").isalnum() and not name[0].isdigit() else ""


async def _refuse_inward_url(url: str) -> dict | None:
    """Refuse a tool URL that points back inside our own network.

    Narrow on purpose. A host that does not resolve from this machine right now
    is allowed through — an internal API, a service not yet deployed, a DNS
    blip — because refusing there would be a guess about someone else's
    network, and the tool would be just as creatable from the tools screen.
    What is refused is what is never right: a non-HTTP scheme, no host, and a
    host that is (or resolves to) loopback, link-local, or private space. That
    is the shape a prompt-injected builder writes when it goes looking for the
    cloud metadata endpoint.

    This is no longer the only check these URLs get — `core/http_tool.py::send`
    re-checks the rendered URL at the moment of the call (S4), which is the one
    that holds when DNS moves after a tool is saved. This check stays because a
    refusal while the model is still writing is worth more than one at run time.
    """
    import ipaddress
    from urllib.parse import urlparse

    from core import egress_guard

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": f"A tool URL must be http or https, not '{parsed.scheme or url}'."}
    host = (parsed.hostname or "").strip().lower().strip("[]")
    if not host:
        return {"error": "That URL has no host."}
    if host in egress_guard.allowed_hosts():
        return None

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            addresses = await egress_guard.resolve(host)
        except egress_guard.BlockedEgress:
            return None  # unresolvable from here; not our call to make

    for address in addresses:
        reason = egress_guard.reject_address(address)
        if reason is not None:
            return {"error": f"That URL points inside our own network ({reason}) and cannot be used as a tool."}
    return None


def _require_confirm(args: dict, what: str) -> dict | None:
    """Destructive tools refuse until the caller says so explicitly.

    Two layers guard a delete: this one, and the fact that `delete_*` is
    declared only on the operator agent, which runs only after a human has
    approved a plan naming the deletion.
    """
    if args.get("confirm") is True:
        return None
    return {
        "error": (
            f"Refusing to {what} without confirmation. This cannot be undone. "
            f"Call again with confirm=true once the user has agreed."
        )
    }


def _refuse_ungated_transform(steps: list) -> dict | None:
    """Refuse `transform` steps when this workspace has no code runtime.

    Checked at authoring time rather than only at run time: a plan gate that
    fires halfway through a run leaves the customer with a saved orchestration
    that cannot complete. `code_execution_enabled` is set by the cloud's
    per-plan settings binding; open-source installs run their own sandbox and
    default to true.
    """
    from core.config import load_settings

    if load_settings().get("code_execution_enabled", True):
        return None
    offenders = [s.get("id") or "?" for s in steps if s.get("type") == "transform"]
    if not offenders:
        return None
    return {
        "error": (
            f"Transform steps run Python code, which this workspace's plan does not "
            f"include: {offenders}. Use an `llm` step to reshape data, or upgrade the "
            f"plan to run code."
        )
    }


#: The five step fields that carry an expression. `core.expressions` decides
#: what one may contain; this is only the list of places to look.
_EXPRESSION_FIELDS = (
    "if_condition",
    "switch_expression",
    "loop_over",
    "loop_while_condition",
    "loop_break_condition",
)


def _expression_problems(steps: list, *, unsafe_only: bool) -> list[str]:
    """Every expression problem in `steps`, as sentences naming step and field."""
    from core import expressions

    look = expressions.unsafe_reason if unsafe_only else expressions.check_expression
    found = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = step.get("id") or "?"
        for field in _EXPRESSION_FIELDS:
            reason = look(step.get(field))
            if reason:
                found.append(f"step '{sid}' {field}: {reason}")
    return found


def _refuse_unsafe_expression(steps: list) -> dict | None:
    """Refuse steps whose expression this engine will not run.

    Checked at authoring time for the reason `_refuse_ungated_transform` gives:
    an orchestration that saves and then refuses to evaluate is worse than one
    that would not save. It matters more here, because `if_condition` is a
    parameter this model writes directly — so refusing now is what turns a
    prompt-injected expression into a corrected one rather than a stored one.

    Only *unsafe* expressions are refused. A typo is left to the validator to
    report: the engine survives one with a warning, and failing a tool call on
    a mistyped condition would be the worse trade.
    """
    problems = _expression_problems(steps, unsafe_only=True)
    if not problems:
        return None
    return {
        "error": (
            "Refusing to save: an expression uses something this engine will not run. "
            + " ".join(problems)
            + " An expression reads `state` with dots, compares and combines values, "
            "and may call str/int/len and the common string methods — nothing else."
        )
    }


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
                "'orchestrator' for agents that run orchestrations, "
                "'delegate' for agents that dynamically route queries to sub-agents (set delegate_agent_ids to restrict which agents it can delegate to; empty = all agents). "
                "Set tools=['all'] to give access to all tools, or list specific tool names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name for the agent"},
                    "description": {"type": "string", "description": "What this agent does"},
                    "type": {
                        "type": "string",
                        "enum": ["conversational", "code", "orchestrator", "delegate"],
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
                    "delegate_agent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "For delegate agents: list of agent IDs to restrict delegation to. Leave empty to allow delegation to any agent.",
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
                                    "enum": ["conversational", "code", "orchestrator", "delegate"],
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
                                "delegate_agent_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "For delegate agents: agent IDs to restrict delegation to. Empty = allow any agent.",
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
                    "strict_state_types": {
                        "type": "boolean",
                        "description": (
                            "Stop a run when a step's text will not become the type its "
                            "state key declares — an llm step answering in prose where the "
                            "key is declared `list`, say. Off by default: the text stays put "
                            "and the run log says so. Turn it on only when asked to."
                        ),
                    },
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
                    "next_step_id": {"type": "string", "description": "Next step ID. NOT used for evaluator/if_else/switch steps. REQUIRED on a parallel step, where it is the join and must name a merge step."},
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
                        "description": "JSON string of field definitions. The answer is stored under output_key as a mapping {field name: value}: name the output_key for the step (e.g. `review`), not after a field, type it dict, and read one field as `{state.review.feedback}`. Example: '[{\"name\":\"feedback\",\"type\":\"textarea\",\"label\":\"Your feedback\"}]'",
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
                    "loop_count": {"type": "integer", "description": "Number of loop iterations (fixed-count loops)"},
                    "loop_over": {"type": "string", "description": "Expression over shared state yielding a list, e.g. `state.tickets` (loop steps). When set, the loop runs once per item instead of loop_count. Same restricted expression syntax as if_condition; filter a list in a transform step and loop over what it writes."},
                    "loop_item_key": {"type": "string", "description": "State key each for-each item is written to (default `loop_item`)"},
                    "loop_index_key": {"type": "string", "description": "State key each iteration's 0-based index is written to, in every loop mode (default `loop_index`)"},
                    "loop_while_condition": {"type": "string", "description": "Condition re-evaluated before each iteration, e.g. `(state.n or 0) < 5` (loop steps). Same restricted expression syntax as if_condition."},
                    "loop_break_condition": {"type": "string", "description": "Condition evaluated after each iteration; True stops the loop (loop steps). Same restricted expression syntax as if_condition."},
                    "loop_max_iterations": {"type": "integer", "description": "Hard ceiling on iterations for any loop mode (default 50)"},
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
                            "Condition for if_else steps, evaluated against shared state. "
                            "Use dot-notation: e.g. `state.result.flag == True` or `state.score > 5`. "
                            "Missing keys are treated as None. Restricted Python over `state`: dot-notation, comparisons, and/or/not, arithmetic, `in`, the functions str/int/float/bool/list/dict/len/abs/min/max/round, and the common string methods. No f-strings, comprehensions or lambdas."
                        ),
                    },
                    "if_true_step_id": {"type": "string", "description": "Step to go to when if_condition is True (if_else steps)"},
                    "if_false_step_id": {"type": "string", "description": "Step to go to when if_condition is False (if_else steps)"},
                    "switch_expression": {
                        "type": "string",
                        "description": (
                            "Expression whose str() result is matched against switch_cases (switch steps). "
                            "Example: `state.category` or `state.result.status.lower()`. Restricted Python over `state`: dot-notation, comparisons, and/or/not, arithmetic, `in`, the functions str/int/float/bool/list/dict/len/abs/min/max/round, and the common string methods. No f-strings, comprehensions or lambdas."
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
                    "include_full_history": {
                        "type": "boolean",
                        "description": (
                            "For agent/llm/tool steps: controls revision-history rendering on re-runs. "
                            "Default (unset) auto-enables full history whenever this step runs more than once. "
                            "Set false to force last-attempt only (smaller prompt); set true is equivalent to default. "
                            "Only relevant if you want to explicitly opt OUT of full history."
                        ),
                    },
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
                                "loop_over": {"type": "string", "description": "Expression over shared state yielding a list, e.g. `state.tickets` (loop steps). When set, the loop runs once per item instead of loop_count. Same restricted expression syntax as if_condition; filter a list in a transform step and loop over what it writes."},
                                "loop_item_key": {"type": "string", "description": "State key each for-each item is written to (default `loop_item`)"},
                                "loop_index_key": {"type": "string", "description": "State key each iteration's 0-based index is written to, in every loop mode (default `loop_index`)"},
                                "loop_while_condition": {"type": "string", "description": "Condition re-evaluated before each iteration, e.g. `(state.n or 0) < 5` (loop steps). Same restricted expression syntax as if_condition."},
                                "loop_break_condition": {"type": "string", "description": "Condition evaluated after each iteration; True stops the loop (loop steps). Same restricted expression syntax as if_condition."},
                                "loop_max_iterations": {"type": "integer", "description": "Hard ceiling on iterations for any loop mode (default 50)"},
                                "transform_code": {"type": "string"},
                                "print_content": {"type": "string", "description": "Markdown/text for print steps. Supports {state.key} interpolation."},
                                "if_condition": {"type": "string", "description": "Condition for if_else steps (e.g. `state.flag == True`). Restricted Python over `state`: dot-notation, comparisons, and/or/not, arithmetic, `in`, the functions str/int/float/bool/list/dict/len/abs/min/max/round, and the common string methods. No f-strings, comprehensions or lambdas."},
                                "if_true_step_id": {"type": "string", "description": "Step ID when condition is True"},
                                "if_false_step_id": {"type": "string", "description": "Step ID when condition is False"},
                                "switch_expression": {"type": "string", "description": "Expression to evaluate for switch steps. Same restricted expression syntax as if_condition."},
                                "switch_cases_json": {"type": "string", "description": "JSON string: '{\"value\":\"step_id\"}'"},
                                "switch_default_step_id": {"type": "string", "description": "Fallback step when no case matches"},
                                "max_turns": {"type": "integer"},
                                "timeout_seconds": {"type": "integer"},
                                "model": {"type": "string"},
                                "include_full_history": {"type": "boolean", "description": "Opt OUT of full revision history on re-runs (default: auto-on for any re-run). Set false to keep prompts small."},
                            },
                            "required": ["step_id", "name", "type"],
                        },
                    },
                },
                "required": ["orch_id", "steps"],
            },
        },
    },
    # ── Custom HTTP tools ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_custom_tools",
            "description": "List this workspace's custom HTTP tools with their name, description, method, URL and group.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_http_tool",
            "description": (
                "Create a custom HTTP tool an agent can call. The tool's inputs are declared as a "
                "JSON Schema; use {{ placeholders }} in the URL, headers and body to interpolate them "
                "(e.g. https://api.example.com/users/{{ user_id }}). Use {{ global.KEY }} for a "
                "workspace variable and {{ args }} for the whole argument payload. Re-creating a tool "
                "with an existing name replaces it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique tool name (snake_case, e.g. create_ticket)"},
                    "description": {"type": "string", "description": "What the tool does — the model reads this to decide when to call it"},
                    "url": {"type": "string", "description": "Full https URL, with {{ placeholders }} for path/query values"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "description": "HTTP method (default GET)"},
                    "headers_json": {"type": "string", "description": "JSON object of static headers, e.g. '{\"Authorization\":\"Bearer {{ global.API_KEY }}\"}'"},
                    "input_schema_json": {"type": "string", "description": "JSON Schema object describing the tool's arguments, e.g. '{\"type\":\"object\",\"properties\":{\"user_id\":{\"type\":\"string\"}},\"required\":[\"user_id\"]}'"},
                    "body_template": {"type": "string", "description": "Request body template for POST/PUT/PATCH, with {{ placeholders }}. Omit to send the arguments as JSON."},
                    "group": {"type": "string", "description": "Optional group name to file the tool under"},
                },
                "required": ["name", "description", "url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_openapi",
            "description": (
                "Import an OpenAPI 3.x / Swagger 2.0 spec as a set of custom HTTP tools — one tool per "
                "operation, filed under a single group. Pass the spec text (JSON or YAML). Use this "
                "instead of hand-writing many create_http_tool calls when the user supplies a spec."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spec_text": {"type": "string", "description": "The raw OpenAPI spec, JSON or YAML"},
                    "base_url": {"type": "string", "description": "Override the spec's server base URL"},
                    "name_prefix": {"type": "string", "description": "Prefix for every generated tool name, to avoid collisions"},
                    "group": {"type": "string", "description": "Group name (default: the spec's title)"},
                },
                "required": ["spec_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_custom_tool",
            "description": "Delete a custom tool by name. Destructive: requires confirm=true, and agents referencing the tool lose it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Must be true. Deleting is not reversible."},
                },
                "required": ["name", "confirm"],
            },
        },
    },
    # ── Skills ───────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List the skills available in this workspace (name, description, source). Skills are reusable instruction documents an agent loads on demand.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill",
            "description": "Read one skill's full body by name.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": (
                "Create a reusable skill: a markdown instruction document an agent can load when the "
                "task calls for it. Prefer a skill over stuffing procedure into a system prompt — it is "
                "shared across agents and only enters the context when needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "kebab-case name, e.g. quarterly-report-format"},
                    "description": {"type": "string", "description": "One line: when an agent should load this skill"},
                    "body": {"type": "string", "description": "The skill's markdown body — the instructions themselves"},
                },
                "required": ["name", "description", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_skill",
            "description": "Replace an existing skill's description and/or body.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "attach_skills",
            "description": "Set the list of skills an agent can load. Replaces the agent's current skill list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "names": {"type": "array", "items": {"type": "string"}, "description": "Skill names; pass [] to detach all"},
                },
                "required": ["agent_id", "names"],
            },
        },
    },
    # ── Schedules ────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "finish_build",
            "description": (
                "Close the build this run started: remove any orchestration it created that "
                "never passed validation. Takes no arguments; touches nothing outside this run."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_schedules",
            "description": "List this workspace's schedules with their target, cadence, enabled state and next run.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_schedule",
            "description": (
                "Run an agent or orchestration on a cadence. Either an interval "
                "(interval_value + interval_unit) or a cron expression — not both. "
                "It is created paused: a person checks what each run starts with and turns it on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "target_type": {"type": "string", "enum": ["agent", "orchestration"]},
                    "target_id": {"type": "string", "description": "The agent or orchestration id to run"},
                    "prompt": {"type": "string", "description": "The message the scheduled run starts with"},
                    "schedule_type": {"type": "string", "enum": ["interval", "cron"]},
                    "interval_value": {"type": "integer"},
                    "interval_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
                    "cron_expression": {"type": "string", "description": "Standard 5-field cron, e.g. '0 8 * * 1-5'"},
                },
                "required": ["name", "target_type", "target_id", "prompt", "schedule_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_schedule_enabled",
            "description": "Pause or resume a schedule without deleting it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "enabled": {"type": "boolean"},
                },
                "required": ["schedule_id", "enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_schedule",
            "description": "Delete a schedule. Destructive: requires confirm=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Must be true."},
                },
                "required": ["schedule_id", "confirm"],
            },
        },
    },
    # ── Variables and runs ───────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_variables",
            "description": (
                "List the workspace variable keys with a short preview of each value. Values are "
                "referenced in tool URLs, headers and bodies as {{ global.KEY }}. Previews are "
                "truncated — never quote a value back to the user."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_runs",
            "description": "Recent orchestration runs, newest first, with their status and step. Use this to answer questions about what ran and what failed.",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many runs (default 10, max 50)"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_run_summary",
            "description": "Status snapshot for one run: status, current step, whether it is waiting on a human, start and end times.",
            "parameters": {
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
        },
    },
    # ── Deletes ──────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "delete_agent",
            "description": (
                "Delete an agent. Destructive: requires confirm=true. Orchestration steps that name "
                "this agent will fail until they are repointed — check with list_orchestrations first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Must be true."},
                },
                "required": ["agent_id", "confirm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_orchestration",
            "description": "Delete an orchestration. Destructive: requires confirm=true. Its run history is kept.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orch_id": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Must be true."},
                },
                "required": ["orch_id", "confirm"],
            },
        },
    },
]


# Set of all builder-tool names. Consumed by tool dispatch paths
# (react_engine, ToolStepExecutor) to route these to execute_builder_tool
# instead of MCP / custom-tool execution, and by aggregate_all_tools to
# expose them as first-class tools that any agent can declare.
BUILDER_TOOL_NAMES = {t["function"]["name"] for t in BUILDER_TOOL_SCHEMAS}


# ─── Tool server capability ───────────────────────────────────────────────────
#
# `list_tool_servers` is the first call every builder prompt tells an agent to
# make, and the Requirements Analyst gets one round of tool calls — so whatever
# this answers is often the whole basis for choosing an integration. It used to
# answer with names: the analyst read `deepwiki` as a wiki to search for general
# research, against its own "don't guess capabilities" rule, and the agent it
# actually holds answers questions about GitHub repositories (F126).
#
# A name is not a capability, so the listing carries a few of the server's real
# tools. Bounded on purpose: `list_all_tools` is banned in the analyst's own
# prompt for blowing up context, and acme's three servers hold 64 tools whose
# raw definitions are 62 KB. Eight names with a one-line description is ~2.4 KB
# for the same three and says what each server is for; `tool_count` tells the
# agent there is more, and `list_server_tools` is the next call the prompt
# already names.

#: How many of a server's tools the listing names, and how much of each
#: description it keeps. Enough to characterise a server, not to work from.
_SERVER_TOOLS_SHOWN = 8
_SERVER_TOOL_DESC = 90


def _tool_summary(tools: list[dict] | None) -> dict:
    """`tool_count` plus a bounded preview, for one server's listing entry."""
    tools = tools or []
    preview = []
    for t in tools[:_SERVER_TOOLS_SHOWN]:
        # First line only: an MCP description is often a paragraph with usage
        # notes under it, and the first line is the sentence that says what it
        # does.
        desc = (t.get("description") or "").strip().split("\n")[0]
        preview.append({"name": t.get("name"), "description": desc[:_SERVER_TOOL_DESC]})
    return {"tool_count": len(tools) or None, "tools": preview}


async def _session_tools(server_module: Any, name: str | None) -> list[dict]:
    """A registered server's tools from its live session, when the row has none."""
    session = (getattr(server_module, "agent_sessions", None) or {}).get(f"ext_mcp_{name}")
    if not session:
        return []
    try:
        return [{"name": t.name, "description": t.description}
                for t in (await session.list_tools()).tools]
    except Exception:
        # A server that cannot be reached is still worth listing by name.
        return []


# ─── Embedder seams ────────────────────────────────────────────────────────────

import contextvars as _contextvars

#: Asked before any builder tool creates an orchestration or agent (F154).
#: ``await check(kind, adding)`` returns a sentence refusing it, or None. None
#: installed means no limit — OSS. Cloud installs its plan's census, so the
#: builder and the template gallery refuse the same thing for the same reason.
_room_check: Any = None


def set_room_check(check: Any) -> None:
    """Install the plan-room check. ``None`` removes it."""
    global _room_check
    _room_check = check


async def _refused(kind: str, adding: int) -> dict | None:
    if _room_check is None:
        return None
    reason = await _room_check(kind, adding)
    return {"error": reason, "refused": "plan_limit"} if reason else None


#: The run a builder tool is working inside, when the engine says (F156). A
#: skeleton is stamped with it until it validates, so `finish_build` can remove
#: what its own run started and never finished — never another run's.
_current_run: _contextvars.ContextVar[str | None] = _contextvars.ContextVar(
    "builder_tools_current_run", default=None
)


# ─── Tool Implementations ──────────────────────────────────────────────────────

def _long_agent_name(name) -> str | None:
    """The refusal for an agent name past the limit (F217), or None."""
    from core.models import AGENT_NAME_MAX
    if isinstance(name, str) and len(name) > AGENT_NAME_MAX:
        return (f"An agent's name is at most {AGENT_NAME_MAX} characters; this one is "
                f"{len(name)}. Choose a shorter name.")
    return None


async def execute_builder_tool(tool_name: str, args: dict, server_module: Any) -> str:
    """Dispatch a builder tool call and return a JSON string result."""
    output, _events = await execute_builder_tool_with_events(tool_name, args, server_module)
    return output


async def execute_builder_tool_with_events(
    tool_name: str, args: dict, server_module: Any, *, run_id: str | None = None
) -> tuple[str, list[dict]]:
    """Dispatch a builder tool, returning its JSON result *and* what it changed.

    The result string is what the model reads, so it stays lean. The events are
    what the UI reads, so they carry the whole resource. Keeping them separate
    is the point: the panel used to recover "an orchestration was saved" by
    running a regex over a 500-character truncation of the result, which failed
    on any orchestration big enough to be worth building.
    """
    events: list[dict] = []
    token = _current_run.set(run_id)
    try:
        result = await _dispatch(tool_name, args, server_module, events)
        return json.dumps(result, default=str), events
    except Exception as e:
        return json.dumps({"error": str(e)}), []
    finally:
        _current_run.reset(token)


def _emit(
    events: list[dict],
    kind: str,
    action: str,
    resource_id: str | None,
    name: str | None,
    resource: Any = None,
) -> None:
    """Record that the builder changed something the workspace can see."""
    events.append({
        "type": "resource_saved",
        "kind": kind,           # agent | orchestration | tool | skill | schedule
        "action": action,       # created | updated | deleted
        "id": resource_id,
        "name": name,
        "resource": resource,
    })


async def _dispatch(tool_name: str, args: dict, server_module: Any, events: list[dict]) -> Any:
    from core.routes.agents import load_user_agents
    from core.routes.orchestrations import load_orchestrations
    from core.store.resources import (
        get_agent as _load_agent,
        get_orchestration as _load_orch,
        save_agent,
        save_orchestration,
    )

    if tool_name == "list_agents":
        agents = await load_user_agents()
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
            if not a.get("system")  # the builder's own machinery is not workspace content
        ]

    elif tool_name == "get_agent":
        agent = await _load_agent(args["agent_id"])
        if not agent or agent.get("system"):
            return {"error": f"Agent '{args['agent_id']}' not found"}
        return agent

    elif tool_name == "create_agent":
        if (too_long := _long_agent_name(args.get("name"))) is not None:
            return {"error": too_long}
        if (refusal := await _refused("agent", 1)) is not None:
            return refusal
        agents = await load_user_agents()
        existing_ids = {a.get("id") for a in agents}
        # Guarantee uniqueness: the id is a primary key, and on cloud it is a
        # global one. A millisecond timestamp collides under any concurrency.
        new_id = _random_id("agent_", length=10)
        while new_id in existing_ids:
            new_id = _random_id("agent_", length=10)
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
            "delegate_agent_ids": args.get("delegate_agent_ids", []) or [],
        }
        await save_agent(agent)
        _emit(events, "agent", "created", agent["id"], agent["name"], agent)
        return {"status": "created", "agent": agent}

    elif tool_name == "create_agents":
        specs = args.get("agents") or []
        if not isinstance(specs, list) or not specs:
            return {"error": "agents must be a non-empty array of agent specs"}
        if (refusal := await _refused("agent", len(specs))) is not None:
            return refusal
        agents = await load_user_agents()
        existing_ids = {a.get("id") for a in agents}
        created = []
        documents = []
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
                if (too_long := _long_agent_name(spec["name"])) is not None:
                    errors.append({"index": i, "role": spec.get("role"), "error": too_long})
                    continue
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
                    "delegate_agent_ids": spec.get("delegate_agent_ids", []) or [],
                }
                documents.append(agent)
                created.append({
                    "role": spec.get("role"),
                    "id": new_id,
                    "name": agent["name"],
                    "type": agent["type"],
                    "repos": agent["repos"],
                })
        for document in documents:
            await save_agent(document)
            _emit(events, "agent", "created", document["id"], document["name"], document)
        return {"status": "created", "agents": created, "errors": errors}

    elif tool_name == "update_agent":
        agent = await _load_agent(args["agent_id"])
        if not agent:
            return {"error": f"Agent '{args['agent_id']}' not found"}
        if agent.get("system"):
            return {"error": f"Agent '{args['agent_id']}' is part of the builder itself and cannot be edited."}
        fields = args.get("fields", {}) or {}
        # Marking an agent as the builder's own hides it from every list (F158).
        if "system" in fields or fields.get("type") == "builder":
            return {"error": "An agent cannot be made part of the builder: `system` and type 'builder' are not editable."}
        if "name" in fields and (too_long := _long_agent_name(fields["name"])) is not None:
            return {"error": too_long}
        agent.update(fields)
        agent["id"] = args["agent_id"]  # the id is not patchable
        await save_agent(agent)
        _emit(events, "agent", "updated", agent["id"], agent.get("name"), agent)
        return {"status": "updated", "agent": agent}

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
                _agents = await load_user_agents()
                active_agent = next((a for a in _agents if not a.get("system")), _agents[0] if _agents else {})
                custom_tools = await load_custom_tools()
                all_tools, _, _, _ = await aggregate_all_tools(
                    server_module, active_agent, custom_tools
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
            _agents = await load_user_agents()
            active_agent = next((a for a in _agents if not a.get("system")), _agents[0] if _agents else {})
            custom_tools = await load_custom_tools()
            all_tools, _, _, _ = await aggregate_all_tools(
                server_module, active_agent, custom_tools
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
        from core.routes.tools import _NATIVE_SERVER_LABELS

        ext_servers = await load_mcp_servers()
        result = []
        for s in ext_servers:
            name = s.get("name")
            # A stored row already carries what the server exposes, so the
            # capability costs nothing to report (F126). A row saved before
            # that, or one whose OAuth never completed, has an empty list and
            # falls through to its live session.
            tools = s.get("tools") or await _session_tools(server_module, name)
            result.append({
                "name": name,
                "label": s.get("label", name),
                "type": s.get("server_type", "stdio"),
                "status": s.get("status", "unknown"),
                **_tool_summary(tools),
            })
        # Also include MCP sessions started programmatically (not in mcp_store.json).
        # Sessions keyed as "ext_mcp_<name>" are external — strip the prefix and
        # mark them as external so callers know to use "name__tool_name" format.
        # Sessions without that prefix are native (no tool-name prefix needed).
        ext_keys = {f"ext_mcp_{s.get('name')}" for s in ext_servers}
        for sess_name, session in server_module.agent_sessions.items():
            if sess_name in ext_keys:
                continue
            try:
                tools = [
                    {"name": t.name, "description": t.description}
                    for t in (await session.list_tools()).tools
                ]
            except Exception:
                tools = []
            is_ext = sess_name.startswith("ext_mcp_")
            display_name = sess_name[len("ext_mcp_"):] if is_ext else sess_name
            result.append({
                "name": display_name,
                "label": (None if is_ext else _NATIVE_SERVER_LABELS.get(display_name))
                         or display_name.replace("_", " ").title(),
                "type": "external_mcp" if is_ext else "native_mcp",
                "status": "running",
                **_tool_summary(tools),
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
        repos = await collections.load("repos")
        return [
            {"id": r.get("id"), "name": r.get("name", r.get("path", "")), "path": r.get("path", "")}
            for r in repos
        ]

    elif tool_name == "list_db_configs":
        dbs = await collections.load("db_configs")
        return [
            {"id": d.get("id"), "name": d.get("name", ""), "type": d.get("type", "")}
            for d in dbs
        ]

    elif tool_name == "list_orchestrations":
        orchs = await load_orchestrations()
        return [
            {
                "id": o.get("id"),
                "name": o.get("name"),
                "description": o.get("description", ""),
                "step_count": len(o.get("steps", [])),
            }
            for o in orchs
            if _is_visible(o)
        ]

    elif tool_name == "get_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        return orch

    elif tool_name == "create_orchestration":
        if (refusal := await _refused("orchestration", 1)) is not None:
            return refusal
        existing_ids = {o.get("id") for o in await load_orchestrations()}
        orch_id = _random_id("orch_")
        while orch_id in existing_ids:
            orch_id = _random_id("orch_")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        _normalize_state_schema_arg(args)
        _normalize_steps_arg(args)
        steps = args.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return {"error": "steps_json must decode to a non-empty JSON array of step objects"}
        steps = _fill_step_defaults(steps)
        refused = _refuse_ungated_transform(steps)
        if refused:
            return refused
        refused = _refuse_unsafe_expression(steps)
        if refused:
            return refused

        orch = {
            "id": orch_id,
            "name": args["name"],
            "description": args.get("description", ""),
            "avatar": "default",
            "steps": steps,
            "entry_step_id": args["entry_step_id"],
            "state_schema": args.get("state_schema", {}),
            "max_total_turns": args.get("max_total_turns", 100),
            "timeout_minutes": args.get("timeout_minutes", 30),
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        await save_orchestration(orch)
        _emit(events, "orchestration", "created", orch["id"], orch["name"], orch)
        return {"status": "created", "orchestration": orch}

    elif tool_name == "update_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
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
            refused = _refuse_ungated_transform(fields["steps"])
            if refused:
                return refused
            refused = _refuse_unsafe_expression(fields["steps"])
            if refused:
                return refused
        if not fields or set(fields.keys()) - {"orch_id"} == set():
            return {"error": "no updatable fields supplied"}
        # Drop orch_id if it snuck in
        fields.pop("orch_id", None)
        orch.update(fields)
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "updated", "orchestration": orch}

    elif tool_name == "create_orchestration_skeleton":
        if (refusal := await _refused("orchestration", 1)) is not None:
            return refusal
        existing_ids = {o.get("id") for o in await load_orchestrations()}
        orch_id = _random_id("orch_")
        while orch_id in existing_ids:
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
            "timeout_minutes": args.get("timeout_minutes", 30),
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        if (run := _current_run.get()) is not None:
            orch["building_run"] = run  # cleared when it validates (F156)
        await save_orchestration(orch)
        _emit(events, "orchestration", "created", orch["id"], orch["name"], orch)
        return {"status": "created", "orchestration": orch}

    elif tool_name == "add_steps":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
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
        existing = orch.get("steps", []) or []
        existing_ids = {s.get("id") for s in existing}
        incoming_ids = [s.get("id") for s in new_steps if s.get("id")]
        dup = existing_ids.intersection(incoming_ids)
        if dup:
            return {"error": f"Duplicate step ids: {sorted(dup)}"}
        # Offset canvas positions so newly-added steps continue the zig-zag
        # pattern past any existing steps.
        offset = len(existing)
        filled = _fill_step_defaults(new_steps)
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_unsafe_expression(filled)
        if refused:
            return refused
        for i, s in enumerate(filled):
            s["position_x"] = _POS_X_START + (offset + i) * _POS_X_STEP
            s["position_y"] = _zigzag_y(offset + i)
        orch["steps"] = existing + filled
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {
            "status": "appended",
            "added_step_ids": [s["id"] for s in filled],
            "total_steps": len(orch["steps"]),
        }

    elif tool_name == "add_single_step":
        # Flat-parameter tool: assemble a step dict from individual args,
        # then reuse the same pipeline as add_steps.
        orch_id = args.get("orch_id")
        orch = await _visible_orchestration(orch_id)
        if orch is None:
            return {"error": f"Orchestration '{orch_id}' not found"}

        step_id = args.get("step_id")
        if not step_id:
            return {"error": "step_id is required"}
        step_name = args.get("name")
        step_type = args.get("type")
        if not step_name or not step_type:
            return {"error": "name and type are required"}

        # Check duplicate
        existing = orch.get("steps", []) or []
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
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_unsafe_expression(filled)
        if refused:
            return refused
        # Position continues the zig-zag past any existing steps.
        offset = len(existing)
        filled[0]["position_x"] = _POS_X_START + offset * _POS_X_STEP
        filled[0]["position_y"] = _zigzag_y(offset)
        orch["steps"] = existing + filled
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {
            "status": "step_added",
            "step_id": filled[0]["id"],
            "total_steps": len(orch["steps"]),
        }

    elif tool_name == "add_multiple_steps":
        # Batch version of add_single_step — takes a native array of step objects.
        orch_id = args.get("orch_id")
        orch = await _visible_orchestration(orch_id)
        if orch is None:
            return {"error": f"Orchestration '{orch_id}' not found"}

        raw_steps = args.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            return {"error": "steps must be a non-empty array of step objects"}

        existing = orch.get("steps", []) or []
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
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_unsafe_expression(filled)
        if refused:
            return refused
        offset = len(existing)
        for i, s in enumerate(filled):
            s["position_x"] = _POS_X_START + (offset + i) * _POS_X_STEP
            s["position_y"] = _zigzag_y(offset + i)
        orch["steps"] = existing + filled
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {
            "status": "steps_added",
            "added_step_ids": [s["id"] for s in filled],
            "total_steps": len(orch["steps"]),
        }

    elif tool_name == "set_orchestration_meta":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        # Accept flat top-level fields (preferred); legacy `fields` wrapper still
        # works for callers that haven't migrated yet.
        raw_fields = dict(args.get("fields") or {})
        for key in ("name", "description", "entry_step_id", "state_schema_json",
                    "max_total_turns", "timeout_minutes"):
            if key in args and args[key] is not None:
                raw_fields[key] = args[key]
        _normalize_state_schema_arg(raw_fields)
        allowed = {"name", "description", "entry_step_id", "state_schema",
                   "max_total_turns", "timeout_minutes", "strict_state_types"}
        fields = {k: v for k, v in raw_fields.items() if k in allowed}
        if not fields:
            return {"error": f"No allowed meta fields to set. Allowed: {sorted(allowed)}"}
        orch.update(fields)
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "meta_updated", "fields": list(fields.keys())}

    elif tool_name == "update_step":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        step_id = args["step_id"]
        patch = _normalize_patch_arg(args)
        if not isinstance(patch, dict) or not patch:
            return {"error": "patch_json must decode to a non-empty JSON object of step fields"}
        steps = orch.get("steps", []) or []
        sidx = next((i for i, s in enumerate(steps) if s.get("id") == step_id), None)
        if sidx is None:
            return {"error": f"Step '{step_id}' not found in orchestration '{args['orch_id']}'"}
        merged = {**steps[sidx], **patch}
        merged["id"] = step_id  # id is immutable via patch
        steps[sidx] = _fill_step_defaults([merged])[0]
        refused = _refuse_ungated_transform([steps[sidx]])
        if refused:
            return refused
        refused = _refuse_unsafe_expression([steps[sidx]])
        if refused:
            return refused
        orch["steps"] = steps
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "step_updated", "step": steps[sidx]}

    elif tool_name == "remove_step":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        step_id = args["step_id"]
        steps = orch.get("steps", []) or []
        before = len(steps)
        steps = [s for s in steps if s.get("id") != step_id]
        if len(steps) == before:
            return {"error": f"Step '{step_id}' not found in orchestration '{args['orch_id']}'"}
        orch["steps"] = steps
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(orch)
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "step_removed", "step_id": step_id, "remaining_steps": len(steps)}

    elif tool_name == "validate_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        verdict = _validate_orchestration(orch)
        if verdict.get("ok") and orch.pop("building_run", None) is not None:
            await save_orchestration(orch)  # finished: `finish_build` keeps it
        return verdict

    elif tool_name == "finish_build":
        # F156: remove what this run's saver started and never validated. Never
        # another run's, and nothing at all outside a run.
        run = _current_run.get()
        removed: list[str] = []
        if run is not None:
            from core.store.resources import delete_orchestration as _delete_orch
            for orch in await load_orchestrations():
                if orch.get("building_run") == run:
                    await _delete_orch(orch["id"])
                    _emit(events, "orchestration", "deleted", orch["id"], orch.get("name"))
                    removed.append(orch["id"])
        return {"status": "finished", "removed": removed}

    # ── Custom HTTP tools ────────────────────────────────────────────────────

    elif tool_name == "list_custom_tools":
        from core.routes.tools import load_custom_tools
        return [
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "method": t.get("method", "GET"),
                "url": t.get("url", ""),
                "group": t.get("group"),
                "tool_type": t.get("tool_type", "http"),
            }
            for t in await load_custom_tools()
        ]

    elif tool_name == "create_http_tool":
        from core.store.resources import save_tool

        name = _tool_name(args.get("name"))
        if not name:
            return {"error": "name must be a tool-style identifier (letters, digits, underscores)"}
        url = str(args.get("url") or "").strip()
        refused = await _refuse_inward_url(url)
        if refused:
            return refused
        headers = _parse_json_field(args.get("headers_json"), {})
        if not isinstance(headers, dict):
            return {"error": "headers_json must decode to a JSON object"}
        input_schema = _parse_json_field(args.get("input_schema_json"), {"type": "object", "properties": {}})
        if not isinstance(input_schema, dict):
            return {"error": "input_schema_json must decode to a JSON Schema object"}
        group_name = str(args.get("group") or "").strip()
        group_id = None
        if group_name:
            from core.routes.tools import _group_id, _upsert_group
            group_id = _group_id(group_name)
            await _upsert_group({"id": group_id, "name": group_name, "description": ""})

        tool = {
            "name": name,
            "generalName": name.replace("_", " ").title()[:80],
            "description": str(args.get("description") or "").strip(),
            "tool_type": "http",
            "method": str(args.get("method") or "GET").upper(),
            "url": url,
            "headers": headers,
            "inputSchema": input_schema,
            "group": group_id,
        }
        # Stored as `body`, the key `http_tool` and both tool editors use. It was
        # stored under the parameter's own name, so the template never reached
        # the request and the bare arguments went out instead (F2).
        body_template = args.get("body_template")
        if body_template:
            tool["body"] = body_template
        await save_tool(tool)
        _emit(events, "tool", "created", name, name, tool)
        return {"status": "created", "id": name, "name": name}

    elif tool_name == "import_openapi":
        from core.openapi_import import parse_openapi_spec
        from core.routes.tools import _group_id, _parse_spec_text, _upsert_group
        from core.store.resources import save_tools

        try:
            spec = _parse_spec_text(args.get("spec_text") or "")
        except Exception as exc:  # HTTPException from the shared parser
            return {"error": f"Could not parse the spec: {getattr(exc, 'detail', exc)}"}
        try:
            generated = parse_openapi_spec(
                spec,
                base_url=(args.get("base_url") or None),
                name_prefix=str(args.get("name_prefix") or ""),
            )
        except ValueError as exc:
            return {"error": str(exc)}

        info = spec.get("info") or {}
        group_name = (
            str(args.get("group") or "").strip()
            or str(info.get("title") or "").strip()
            or str(args.get("name_prefix") or "").strip()
            or "Imported API"
        )
        group = {
            "id": _group_id(group_name),
            "name": group_name,
            "description": str(info.get("description") or "").strip()[:500],
        }
        for t in generated:
            t["group"] = group["id"]
        await _upsert_group(group)
        await save_tools(generated)
        # One event for the import, not one per operation: a 60-endpoint spec
        # would otherwise fill the panel with sixty identical banners.
        _emit(events, "tool", "created", group["id"], group_name,
              {"group": group, "count": len(generated), "names": [t["name"] for t in generated]})
        return {
            "status": "imported",
            "id": group["id"],
            "name": group_name,
            "tool_names": [t["name"] for t in generated],
            "count": len(generated),
        }

    elif tool_name == "delete_custom_tool":
        refused = _require_confirm(args, f"delete the tool '{args.get('name')}'")
        if refused:
            return refused
        from core.store.resources import delete_tool
        name = str(args.get("name") or "")
        if not await delete_tool(name):
            return {"error": f"Custom tool '{name}' not found"}
        _emit(events, "tool", "deleted", name, name)
        return {"status": "deleted", "id": name, "name": name}

    # ── Skills ───────────────────────────────────────────────────────────────

    elif tool_name == "list_skills":
        from core import skills as skills_mod
        return await skills_mod.list_skills()

    elif tool_name == "get_skill":
        from core import skills as skills_mod
        skill = await skills_mod.get_skill(str(args.get("name") or ""))
        if skill is None:
            return {"error": f"Skill '{args.get('name')}' not found"}
        return skill

    elif tool_name in ("create_skill", "update_skill"):
        from core import skills as skills_mod

        name = skills_mod.normalize_name(str(args.get("name") or ""))
        if not skills_mod._NAME_RE.match(name):
            return {"error": f"'{name}' is not a usable skill name (letters, digits, ., _, -)"}
        library = await skills_mod.load_library()
        existing = next((s for s in library if (s.get("name") or s.get("id")) == name), None)
        if tool_name == "update_skill" and existing is None:
            return {"error": f"Skill '{name}' is not in the library — create it first"}
        description = str(args.get("description") or (existing or {}).get("description") or "").strip()
        body = args.get("body")
        if body is None:
            body = (existing or {}).get("body", "")
        if not description:
            return {"error": "description is required — it is how an agent decides to load the skill"}
        document = {"id": name, "name": name, "description": description, "body": body}
        await skills_mod.save_library([s for s in library if (s.get("name") or s.get("id")) != name] + [document])
        _emit(events, "skill", "updated" if existing else "created", name, name, document)
        return {
            "status": "updated" if existing else "created",
            "id": name,
            "name": name,
        }

    elif tool_name == "attach_skills":
        from core import skills as skills_mod

        agent = await _load_agent(args["agent_id"])
        if not agent:
            return {"error": f"Agent '{args['agent_id']}' not found"}
        if agent.get("system"):
            return {"error": f"Agent '{args['agent_id']}' is part of the builder itself and cannot be edited."}
        wanted = [skills_mod.normalize_name(n) for n in (args.get("names") or [])]
        known = {s["name"] for s in await skills_mod.list_skills()}
        missing = [n for n in wanted if n not in known]
        if missing:
            return {"error": f"No such skill(s): {missing}. Call list_skills to see what exists."}
        agent["skills"] = wanted
        agent["id"] = args["agent_id"]
        await save_agent(agent)
        _emit(events, "agent", "updated", agent["id"], agent.get("name"), agent)
        return {"status": "updated", "id": agent["id"], "name": agent.get("name"), "skills": wanted}

    # ── Schedules ────────────────────────────────────────────────────────────

    elif tool_name == "list_schedules":
        from core.store import schedules as schedule_store
        return [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "enabled": s.get("enabled", True),
                "target_type": s.get("target_type"),
                "target_id": s.get("target_id"),
                "schedule_type": s.get("schedule_type"),
                "interval_value": s.get("interval_value"),
                "interval_unit": s.get("interval_unit"),
                "cron_expression": s.get("cron_expression"),
                "next_run_at": s.get("next_run_at"),
                "last_run_at": s.get("last_run_at"),
            }
            for s in await schedule_store.load()
        ]

    elif tool_name == "create_schedule":
        import uuid as _uuid

        from pydantic import ValidationError

        from core.models_schedule import ScheduleCreate
        from core.scheduler import _iso, _utc_now, compute_next_run
        from core.store import schedules as schedule_store

        # The target has to exist, or the schedule fires into nothing forever.
        target_type = args.get("target_type")
        target_id = str(args.get("target_id") or "")
        if target_type == "agent":
            target = await _load_agent(target_id)
            if target is None or target.get("system"):
                return {"error": f"Agent '{target_id}' not found"}
        elif target_type == "orchestration":
            if await _visible_orchestration(target_id) is None:
                return {"error": f"Orchestration '{target_id}' not found"}
        else:
            return {"error": "target_type must be 'agent' or 'orchestration'"}

        try:
            body = ScheduleCreate.model_validate({
                k: v for k, v in args.items()
                if k in ScheduleCreate.model_fields and v is not None
            })
        except ValidationError as exc:
            return {"error": f"Invalid schedule: {exc.errors()[0]['msg']}"}

        schedule = body.model_dump()
        # F155 (D122): always created paused, whatever was asked. The builder's
        # schedules began each run with a restatement of the task rather than its
        # input ("Pull the latest blog posts from our client's three competitors"
        # with none named), so every run would have chosen its own subject. A
        # person reads what it starts with and turns it on — `set_schedule_enabled`
        # when they ask the builder, or the Schedules screen.
        schedule["enabled"] = False
        schedule["id"] = f"sched_{_uuid.uuid4().hex[:8]}"
        schedule["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        schedule["last_run_at"] = None
        try:
            schedule["next_run_at"] = _iso(compute_next_run(schedule, _utc_now()))
        except Exception as exc:
            return {"error": f"Could not work out when that would next run: {exc}"}
        await schedule_store.save(schedule)
        _emit(events, "schedule", "created", schedule["id"], schedule["name"], schedule)
        return {
            "status": "created",
            "id": schedule["id"],
            "name": schedule["name"],
            "enabled": False,
            "next_run_at": schedule["next_run_at"],
            "note": (
                "Created paused. Check the message each run starts with, then turn it on "
                "under Schedules."
            ),
        }

    elif tool_name == "set_schedule_enabled":
        from core.store import schedules as schedule_store

        schedule_id = str(args.get("schedule_id") or "")
        schedule = await schedule_store.get(schedule_id)
        if schedule is None:
            return {"error": f"Schedule '{schedule_id}' not found"}
        schedule["enabled"] = bool(args.get("enabled"))
        await schedule_store.save(schedule)
        _emit(events, "schedule", "updated", schedule_id, schedule.get("name"), schedule)
        return {
            "status": "updated",
            "id": schedule_id,
            "name": schedule.get("name"),
            "enabled": schedule["enabled"],
        }

    elif tool_name == "delete_schedule":
        refused = _require_confirm(args, f"delete the schedule '{args.get('schedule_id')}'")
        if refused:
            return refused
        from core.store import schedules as schedule_store

        schedule_id = str(args.get("schedule_id") or "")
        schedule = await schedule_store.get(schedule_id)
        if schedule is None:
            return {"error": f"Schedule '{schedule_id}' not found"}
        await schedule_store.delete_one(schedule_id)
        _emit(events, "schedule", "deleted", schedule_id, schedule.get("name"))
        return {"status": "deleted", "id": schedule_id, "name": schedule.get("name")}

    # ── Variables and runs ───────────────────────────────────────────────────

    elif tool_name == "list_variables":
        from core import variables

        # Keys and a short preview only. A variable holds things like an API
        # token; the builder needs to know it exists and how to reference it,
        # never what it says.
        return [
            {"key": key, "preview": str(value)[:40], "reference": f"{{{{ global.{key} }}}}"}
            for key, value in sorted((await variables.load()).items())
        ]

    elif tool_name == "list_recent_runs":
        from core.orchestration.state import SharedState

        limit = max(1, min(int(args.get("limit") or 10), 50))
        visible = {o["id"] for o in await load_orchestrations() if _is_visible(o) and o.get("id")}
        runs = await SharedState.list_runs(limit=limit, orchestration_ids=visible)
        return [
            {
                "run_id": r.get("run_id"),
                "orchestration_id": r.get("orchestration_id"),
                "status": r.get("status"),
                "current_step_id": r.get("current_step_id"),
                "started_at": r.get("started_at"),
                "ended_at": r.get("ended_at"),
            }
            for r in runs
        ]

    elif tool_name == "get_run_summary":
        from core.routes.orchestrations import _run_summary

        summary = await _run_summary(str(args.get("run_id") or ""))
        if summary is None:
            return {"error": f"Run '{args.get('run_id')}' has no checkpoint"}
        if await _visible_orchestration(summary.get("orchestration_id")) is None:
            return {"error": f"Run '{args.get('run_id')}' has no checkpoint"}
        return summary

    # ── Deletes ──────────────────────────────────────────────────────────────

    elif tool_name == "delete_agent":
        refused = _require_confirm(args, f"delete the agent '{args.get('agent_id')}'")
        if refused:
            return refused
        from core.store.resources import delete_agent as _delete_agent

        agent_id = str(args.get("agent_id") or "")
        agent = await _load_agent(agent_id)
        if agent is None or agent.get("system"):
            return {"error": f"Agent '{agent_id}' not found"}
        # Name what breaks rather than silently leaving dangling references.
        orphaned = [
            {"orchestration_id": o.get("id"), "step_id": s.get("id")}
            for o in await load_orchestrations() if _is_visible(o)
            for s in (o.get("steps") or []) if s.get("agent_id") == agent_id
        ]
        await _delete_agent(agent_id)
        _emit(events, "agent", "deleted", agent_id, agent.get("name"))
        return {
            "status": "deleted",
            "id": agent_id,
            "name": agent.get("name"),
            "orphaned_steps": orphaned,
        }

    elif tool_name == "delete_orchestration":
        refused = _require_confirm(args, f"delete the orchestration '{args.get('orch_id')}'")
        if refused:
            return refused
        from core.store.resources import delete_orchestration as _delete_orch

        orch_id = str(args.get("orch_id") or "")
        orch = await _visible_orchestration(orch_id)
        if orch is None:
            return {"error": f"Orchestration '{orch_id}' not found"}
        await _delete_orch(orch_id)
        _emit(events, "orchestration", "deleted", orch_id, orch.get("name"))
        return {"status": "deleted", "id": orch_id, "name": orch.get("name")}

    else:
        return {"error": f"Unknown tool: {tool_name}"}


#: Steps that exist to produce a value, so losing it is worth an issue.
_RESULT_STEP_TYPES = frozenset({"agent", "llm", "tool", "merge", "transform", "extract_json", "human"})


def _validate_orchestration(orch: dict) -> dict:
    """Return {"ok": bool, "issues": [str, ...]} describing wiring problems.

    Checks (matches the engine's expectations in core.orchestration.engine):
      - entry_step_id resolves to a real step
      - no duplicate step ids
      - every next_step_id / route_map target / loop_step_ids entry /
        parallel_branches element resolves to a real step (empty string = end)
      - every reachable path terminates at an `end`-type step
      - every step's input_keys are written by something: `user_input` /
        `user_query`, some step's `output_key`, a for-each loop's item or
        index key, or `human_response` where a human step exists (a key with
        no writer anywhere is meaningless — it can only ever be its schema
        default)
      - every state_schema entry declares a type we can read, and its default
        is of that type
      - a human step with fields writes a mapping, so its key is not declared
        as text or a number, and no Print shows that mapping whole (F189)
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
        if stype == "parallel":
            # The join, and it is not optional. `resolve_branch_chain` builds a
            # single-entry branch by following `next_step_id` until it reaches
            # this step's own `next_step_id` or a step with no next — so with no
            # join each branch runs the whole rest of the graph, one branch after
            # another, and the first to reach an `end` takes the run with it.
            # Measured on a real orchestration: branch one ran Summarizer →
            # Compiler → Review → Finalizer and branch two never started.
            #
            # A merge step specifically, because combining what the branches
            # produced is what a join is for, and `merge` is the step that does
            # it. The message names the repair: these go back to the saver,
            # which acts on them literally (told a read had no writer, it
            # deleted the read).
            joined = by_id.get(nxt) if nxt else None
            if not nxt or (joined or {}).get("type") != "merge":
                where = f"'{nxt}', which is not a merge step" if nxt else "nothing"
                issues.append(
                    f"Parallel step '{sid}' converges on {where} — it "
                    f"needs next_step_id set to the merge step where its branches meet; "
                    f"add a merge step if the orchestration has none."
                )
        if stype in _RESULT_STEP_TYPES and not (s.get("output_key") or "").strip():
            # These executors write `shared_state[output_key]` and nothing else
            # keeps what they produced. Routing steps, `print` and the two
            # containers are left out: their key is a record, not the point.
            issues.append(
                f"Step '{sid}' ({stype}) has no output_key — its result is discarded and no "
                f"later step can read it."
            )
        if stype == "merge" and not (s.get("input_keys") or []):
            # It combines the keys it is given and nothing else — with none it
            # writes an empty result over its output_key and says nothing.
            issues.append(
                f"Merge step '{sid}' has no input_keys — set them to the output_key of each "
                f"branch it combines."
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
        if stype == "loop":
            body = s.get("loop_step_ids") or []
            if sid in body:
                issues.append(
                    f"Loop step '{sid}' lists itself in loop_step_ids — it would "
                    f"re-enter itself forever."
                )
            cap = s.get("loop_max_iterations", 50)
            if not isinstance(cap, int) or cap < 1:
                issues.append(f"Loop step '{sid}' loop_max_iterations must be a positive integer.")
            elif not s.get("loop_over") and not s.get("loop_while_condition"):
                count = s.get("loop_count", 3)
                if not isinstance(count, int) or count < 1:
                    issues.append(f"Loop step '{sid}' loop_count must be a positive integer.")
                elif count > cap:
                    issues.append(
                        f"Loop step '{sid}' loop_count ({count}) exceeds its "
                        f"loop_max_iterations ({cap}) and would be clamped."
                    )
            for bsid in body:
                bstep = by_id.get(bsid)
                if not bstep:
                    continue
                # The loop runs its body as an ordered list; routing decisions
                # made inside it are evaluated and then discarded, because
                # _resolve_next only runs for steps the engine itself drives.
                if bstep.get("type") in ("if_else", "switch", "evaluator"):
                    issues.append(
                        f"Loop step '{sid}' body contains '{bsid}' of type "
                        f"'{bstep.get('type')}' — its routing is ignored inside a "
                        f"loop body. Put the branch after the loop."
                    )
                # A parallel branch auto-follows next_step_id; a loop body does
                # not, so a chain wired this way silently runs only its head.
                nxt = bstep.get("next_step_id")
                if nxt and nxt not in body and nxt != s.get("next_step_id"):
                    issues.append(
                        f"Loop body step '{bsid}' has next_step_id '{nxt}', which is "
                        f"neither in the loop body nor the loop's done path. A loop "
                        f"body runs only the steps listed in loop_step_ids."
                    )

    # Expression check. `_refuse_unsafe_expression` is the gate on the write
    # paths; this catches the rest — an imported bundle, a hand-edited document,
    # a step the model wrote before the gate existed — and reports a typo too,
    # which the gate deliberately lets through.
    for problem in _expression_problems(steps, unsafe_only=False):
        issues.append(f"Expression problem: {problem}")

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
        # `output_key` is not the only way a key reaches shared state (F123).
        # Every loop writes its iteration's index before the body runs (F212),
        # and a **for-each** loop its item too (`steps.py`, under
        # `if mode == "foreach"`); a count or while loop has no item, so a body
        # reading `loop_item` there is still told so.
        if s.get("type") == "loop":
            writable.add(s.get("loop_index_key") or "loop_index")
            if s.get("loop_over"):
                writable.add(s.get("loop_item_key") or "loop_item")
        # And the engine writes a human answer to `human_response` as well as
        # to the step's own output_key, so evaluators that check it still work.
        if s.get("type") == "human":
            writable.add("human_response")
    for s in steps:
        sid = s.get("id", "?")
        for k in s.get("input_keys") or []:
            if k not in writable:
                # The repair the saver should reach for comes first. Told
                # "delete the read", it deleted one that was correct (F123).
                issues.append(
                    f"Step '{sid}' reads input_key '{k}', which nothing writes — give an "
                    f"earlier step '{k}' as its output_key, or change the read to a key that "
                    f"is already written. Remove the read only if the step does not need that value."
                )

    # Reachability walk: every reachable step must be able to hit an `end`.
    # Dead-end non-end steps (no next_step_id / routes / branches) are bugs —
    # except inside a container, whose members' continuation is the container's
    # (F124). A loop runs the steps in `loop_step_ids` in order and never
    # follows `next_step_id`; `resolve_branch_chain` walks a single-entry
    # branch until the join or a step with no next. Demanding one here is what
    # made the builder give a loop body an edge to the step after the loop.
    # The containers themselves are ordinary steps and stay checked.
    contained: set[str] = set()
    for s in steps:
        contained.update(x for x in (s.get("loop_step_ids") or []) if x)
        for branch in s.get("parallel_branches") or []:
            contained.update(x for x in branch if x)
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
                if cur not in contained:
                    issues.append(
                        f"Step '{cur}' (type={s.get('type')}) has no next_step_id / routes / branches — path dead-ends without reaching an `end` step."
                    )
                continue
            for nxt_id in successors:
                if nxt_id not in visited:
                    queue.append(nxt_id)

    # ── A human step's answer and the key it lands in (F189) ────────────────
    # A human step with fields stores a mapping {field: value} under its output
    # key (F106: a step that declares fields keeps the mapping it was answered
    # with); one with no fields stores the text. The builder named a review
    # step's key after its one field and typed it `str`, so the Print after it
    # showed "Final Summary: {'final_summary': …}". The repair leads, as every
    # message here does, because the savers act on these literally.
    schema = orch.get("state_schema") or {}
    fielded: dict[str, tuple[str, str]] = {}  # key -> (the step that asks, its first field)
    for s in steps:
        if s.get("type") != "human":
            continue
        fields = s.get("human_fields")
        if fields is None and "human_fields_json" in s:
            fields = _parse_json_field(s.get("human_fields_json"), [])
        names = [f["name"] for f in (fields or []) if isinstance(f, dict) and f.get("name")]
        if not names:
            continue
        hid, key = s.get("id", "?"), s.get("output_key") or "human_response"
        fielded[key] = (hid, names[0])
        entry = schema.get(key)
        declared = normalize_state_type(entry.get("type")) if isinstance(entry, dict) else None
        if declared and declared != "dict":
            issues.append(
                f"Human step '{hid}' collects fields ({', '.join(names)}), so '{key}' holds a mapping "
                f"{{field: value}}, not {declared}: declare '{key}' as dict in state_schema and read one "
                f"field as {{state.{key}.{names[0]}}}. Clearer still, name the output_key for the step "
                f"(e.g. 'review'), not after one of its fields."
            )
    for s in steps:
        if s.get("type") != "print":
            continue
        for key, (hid, field) in fielded.items():
            if f"{{state.{key}}}" in (s.get("print_content") or ""):
                issues.append(
                    f"Print step '{s.get('id', '?')}' shows {{state.{key}}}, human step '{hid}'s whole "
                    f"answer, which is a mapping and prints as a Python dict: show one field, "
                    f"{{state.{key}.{field}}}."
                )

    # ── State schema ─────────────────────────────────────────────────────────
    # Nothing used to look at `state_schema` at all. A key could carry no type,
    # or a type nobody recognised, and it saved, loaded and ran — the symptoms
    # being a blank dropdown in the editor and an engine with nothing to coerce
    # a step's raw text against. Each message names the allowed set, because
    # both savers cap themselves at two or three repair attempts: a message
    # without the fix in it burns that budget and reports DONE anyway.
    allowed = "Use one of: " + ", ".join(CANONICAL_STATE_TYPES) + "."
    for key, entry in (orch.get("state_schema") or {}).items():
        if not isinstance(entry, dict):
            issues.append(
                f"State key '{key}' is {type(entry).__name__}, not an object — each key "
                f"maps to {{type, default, description}}. {allowed}"
            )
            continue
        declared = entry.get("type")
        canonical = normalize_state_type(declared)
        if canonical is None:
            what = f"an unknown type '{declared}'" if declared else "no type"
            issues.append(f"State key '{key}' has {what}. {allowed}")
            continue
        if "default" in entry and not matches_declared_type(canonical, entry["default"]):
            issues.append(
                f"State key '{key}' is declared {canonical} but its default is "
                f"{type(entry['default']).__name__} — change the default or the type."
            )

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
        s.setdefault("loop_max_iterations", 50)
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
        s.setdefault("include_full_history", None)
        result.append(s)
    return result


