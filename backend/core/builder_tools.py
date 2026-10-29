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
from core.expressions import vocabulary_sentence
from core.store import collections
from core.store.resources import load_mcp_servers
from core.store.ids import new_id as _mint_id


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

#: What `update_orchestration` sets, flat or through its legacy `fields` (F784): its declared fields, and
#: `steps`/`state_schema` as `_normalize_steps_arg`/`_normalize_state_schema_arg` make them. The run caps
#: (`RUN_CAP_PROPERTIES`) are added where it is used.
_UPDATE_ORCHESTRATION_FIELDS = frozenset({
    "name", "description", "entry_step_id", "state_schema_json", "state_schema",
    "steps_json", "steps", "max_total_turns", "timeout_minutes",
})


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


async def _workflows_called(orch: dict, agents: dict) -> tuple[list[dict], list[str]]:
    """The workflows `orch` calls, at any depth, and an issue for each call whose
    workflow is gone (F429).

    A workflow calls another through an agent step whose agent runs it, or a
    Workflow step that names it (E3), so this is what validating a parent has to
    validate with it. A step that runs a workflow already on the chain that
    reached it — this one included — is a loop: every run would call itself
    again without end (F691).
    """
    from core.orchestration.save_rules import loop_sentence
    from core.store.resources import get_orchestration

    called: list[dict] = []
    issues: list[str] = []
    seen = {orch.get("id")}

    async def visit(current: dict, chain: tuple) -> None:
        where = "" if current is orch else f"In the inner workflow '{current.get('name')}' ({current.get('id')}): "
        for s in current.get("steps") or []:
            direct = s.get("type") == "workflow"
            target = {} if direct else (agents.get(s.get("agent_id")) or {})
            if direct:
                child_id = s.get("workflow_id") or None
            else:
                child_id = target.get("orchestration_id") if target.get("type") == "orchestrator" else None
            if not child_id:
                continue
            if child_id in chain:
                looped = "this workflow" if child_id == orch.get("id") else f"the workflow '{child_id}' that calls it"
                issues.append(loop_sentence(s, target, looped, where))
                continue
            if child_id in seen:
                continue
            seen.add(child_id)
            child = await get_orchestration(child_id)
            if child is None or child.get("is_active", True) is False:
                issues.append(
                    f"{where}Workflow step '{s.get('id')}' runs the workflow '{child_id}', which does not exist — "
                    f"build it first, or point the step at one that does." if direct else
                    f"{where}Agent step '{s.get('id')}' uses workflow agent '{target.get('id')}', whose "
                    f"orchestration '{child_id}' does not exist — build it again, deploy_orchestration it, "
                    f"and use the agent id it returns."
                )
                continue
            if child.get("system"):
                continue
            called.append(child)
            await visit(child, (*chain, child_id))

    await visit(orch, (orch.get("id"),))
    return called, issues


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


#: Knowledge's step types (R1): they search knowledge bases and add records to one.
KNOWLEDGE_STEP_TYPES = frozenset({"retrieve", "ingest"})
#: Builder tools that exist only while Knowledge is on — absent from every roster while it is off.
KNOWLEDGE_BUILDER_TOOLS = frozenset({"list_knowledge_bases"})
#: Builder tools `"all"` does not reach (`core.tools.permits(opt_in=)`): an agent names them, as the builder's
#: analyst does. Knowledge on or off, no `"all"` agent's roster — nor its prompt cache — changes (B1b's rule).
OPT_IN_BUILDER_TOOLS: set[str] = set(KNOWLEDGE_BUILDER_TOOLS)
_KNOWLEDGE_OFF = "Knowledge is switched off in this workspace"


def builder_tool_offered(name: str) -> bool:
    """Whether builder tool `name` exists here right now: Knowledge's are absent while it is off, even to an agent
    that names them. (An `"all"` agent never gets them: `OPT_IN_BUILDER_TOOLS`.)"""
    if name not in KNOWLEDGE_BUILDER_TOOLS:
        return True
    from core.knowledge.config import knowledge_enabled

    return knowledge_enabled()


def _refuse_knowledge_off(steps: list) -> dict | None:
    """Refuse Retrieve and Ingest steps while Knowledge is off: each would fail its run at that step."""
    offenders = [s.get("id") or "?" for s in steps if s.get("type") in KNOWLEDGE_STEP_TYPES]
    if not offenders:
        return None
    from core.knowledge.config import knowledge_enabled

    if knowledge_enabled():
        return None
    return {
        "error": (
            f"Retrieve and Ingest steps search and fill knowledge bases, and {_KNOWLEDGE_OFF}: "
            f"{offenders}. Build the orchestration without them."
        )
    }


def _expression_problems(steps: list, *, unsafe_only: bool) -> list[str]:
    """Every expression problem in `steps`, as sentences naming step and field:
    one rule with every door that saves a workflow (F850)."""
    from core.orchestration.save_rules import expression_problems

    return expression_problems(steps, unsafe_only=unsafe_only)


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
    from core.expressions import vocabulary_sentence

    return {
        "error": (
            "Refusing to save: an expression uses something this engine will not run. "
            + " ".join(problems)
            # What it may use, from the tables that decide it (F957).
            + " " + vocabulary_sentence()
        )
    }


def _refuse_misshapen_steps(steps: list) -> dict | None:
    """Refuse steps the engine's own step model would not read (F1047).

    A field in the wrong shape — a human step's `human_fields` written as
    `{"fields": [...]}` rather than the list itself, as rc4.3's saver did on
    prod — saved as given, broke the editor's page on open and failed the run
    at its first load. Each problem names the step, the field and the shape it
    needs, so the model writes the step again; nothing is saved.
    """
    from pydantic import ValidationError

    from core.models_orchestration import StepConfig

    problems = []
    for s in steps:
        try:
            StepConfig.model_validate(s)
        except ValidationError as e:
            said = "; ".join(dict.fromkeys(_shape_problem(err) for err in e.errors()))
            problems.append(f"Step '{s.get('id', '?')}': {said}.")
    if not problems:
        return None
    return {
        "error": (
            "Refusing to save: a step is not in the shape the engine reads, so the workflow would neither open "
            "nor run. " + " ".join(problems) + " Write the step again with each field in that shape."
        )
    }


def _shape_problem(err: dict) -> str:
    """One pydantic error as the field, the shape it needs and what it was given."""
    from core.models_orchestration import StepType

    loc = err.get("loc") or ()
    if not loc:  # a rule across fields: its own sentence
        return str(err.get("msg", "")).removeprefix("Value error, ")
    field = str(loc[0]) + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in loc[1:])
    kind = err.get("type", "")
    if kind == "missing":
        return f"`{field}` is required"
    if kind == "enum" and loc == ("type",):
        return f"`type` must be one of {', '.join(t.value for t in StepType)}, not {err.get('input')!r}"
    need = _NEEDS.get(kind)
    if need is None:
        return f"`{field}`: {err.get('msg', 'not readable')}"
    if field == "human_fields":
        need = 'a list of objects, e.g. [{"name": "email", "type": "string"}]'
    return f"`{field}` must be {need}, not {_given(err.get('input'))}"


_NEEDS = {
    "list_type": "a list", "dict_type": "an object", "string_type": "a string",
    "int_type": "a whole number", "int_parsing": "a whole number", "int_from_float": "a whole number",
    "float_type": "a number", "float_parsing": "a number", "bool_type": "true or false", "bool_parsing": "true or false",
}


def _given(value: Any) -> str:
    if isinstance(value, bool):
        return "true/false"
    return {dict: "an object", list: "a list", str: "a string", int: "a number", float: "a number",
            type(None): "null"}.get(type(value), type(value).__name__)


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
    "switch_cases_json. Retrieve and ingest steps take add_single_step's flat "
    "retrieve_* / ingest_* fields. An agent step that should read the images "
    "a person attaches in chat sets \"pass_images\": true.\n"
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
    "route_descriptions_json); a retrieve or ingest step's flat retrieve_* / ingest_* "
    "fields change only those settings; pass_images (agent steps) turns Sees attachments on or off. "
    "Example: '{\"prompt_template\":\"New prompt\",\"next_step_id\":\"step_xyz7890\"}'"
)

STATE_SCHEMA_JSON_DESCRIPTION = (
    "JSON-encoded state-schema object mapping each state key to "
    "{type, default, description}. Allowed types: str, int, float, bool, list, dict. "
    "Example: '{\"user_input\":{\"type\":\"str\",\"default\":\"\",\"description\":\"Initial user message\"}}'. "
    "Pass '{}' (empty object JSON) when the orchestration has no upfront state keys."
)

#: A run's cap (CAP1, D272), as every tool that writes a workflow's top-level
#: fields takes it. A number, not a nullable one: 0 removes a cap.
RUN_CAP_PROPERTIES = {
    "max_run_credits": {
        "type": "number",
        "description": (
            "Cloud only: the most execution credits one run of this workflow may use, 1 or more. "
            "A run that reaches it stops before its next step and says so. Set a cap only when "
            "the person asks for one; 0 removes it."),
    },
    "max_run_usd": {
        "type": "number",
        "description": (
            "The most one run of this workflow may spend on models, in US dollars (0.5 is fifty "
            "cents). A run that has spent it stops before its next step and says so. Set a cap "
            "only when the person asks for one; 0 removes it."),
    },
}


# ─── Tool Schemas ─────────────────────────────────────────────────────────────

#: The tags the builder may give a custom tool it makes (TAG1, D296). Only claims that make the tool stricter are kept
#: (`core.tool_tags.tighten`): its method's tags are used otherwise, and the person sees and changes them in the editor.
_TAG_PARAMS = {
    "read_only": {"type": "boolean", "description": "false when this GET changes something (e.g. /send?to=…). Omit otherwise."},
    "destructive": {"type": "boolean", "description": "true when a call deletes, cancels, pays or sends something that cannot be undone. Omit otherwise."},
    "idempotent": {"type": "boolean", "description": "false when calling twice does the work twice (books two rooms). Omit otherwise."},
    "open_world": {"type": "boolean", "description": "Every HTTP tool reaches outside this workspace; omit."},
}


def _builder_tags(tool: dict, before: dict | None, claimed: dict | None) -> dict:
    """`tool` as the builder saves it (TAG1): its method's tags, or the person's when it replaces a tool they tagged
    with the same method, made stricter by what the builder claims and never looser. Its own tags stay marked as a
    claim; a person's stay theirs."""
    from core.tool_tags import carried, defaults_for, hints_of, tags_of, tighten
    from core.tools import BUILDER_HINTS

    carried(tool, before)
    persons = isinstance(tool.get("annotations"), dict) and not tool.get("annotations_by")
    base = tags_of(tool.get("annotations")) or defaults_for(tool.get("method"))
    tool["annotations"] = hints_of(tighten(base, claimed))
    if not persons:
        tool["annotations_by"] = BUILDER_HINTS
    return tool


#: What a condition or switch expression may use, from the engine's own tables (F957): one source with the save's
#: refusal and the planner's steps passage, so a function the engine adds or refuses is said everywhere at once.
_EXPRESSION_DIALECT = ("Restricted Python over `state`. " + vocabulary_sentence()
                       + " No f-strings, comprehensions or lambdas.")

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
            "name": "deploy_orchestration",
            "description": (
                "Deploy an orchestration that already exists as an agent (type 'orchestrator'), so "
                "another orchestration's agent step can run it as a sub-workflow. Returns the agent id — "
                "the one already deployed if it has been. This is the only way to make an orchestrator "
                "agent: create_agent cannot, because an orchestrator agent with no orchestration runs "
                "nothing. One orchestration is built per build, so a nested design is built child first."
            ),
            "parameters": {
                "type": "object",
                "properties": {"orch_id": {"type": "string", "description": "The orchestration to deploy"}},
                "required": ["orch_id"],
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
                "'delegate' for a Team agent: a lead whose members (delegate_agent_ids; empty = any agent) do the work, "
                "as a team in parallel (the default) or one at a time (`team`); its own tools are what its lead answers "
                "small questions with, usually none. "
                "Set tools=['all'] to give access to all tools, or list specific tool names."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Display name for the agent"},
                    "description": {"type": "string", "description": "What this agent does"},
                    "type": {
                        "type": "string",
                        "enum": ["conversational", "code", "delegate"],
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
                        "description": "For a Team agent: its members' agent IDs. Leave empty to allow any agent.",
                    },
                    "team": {
                        "type": "object",
                        "description": (
                            "For a Team agent (type 'delegate') only: how its members work. Omit it and a new Team agent "
                            "works as a team. {\"mode\": \"team\"}: members work in parallel on what its lead assigns, "
                            "talking while they work; \"one_at_a_time\": the lead hands work to one member and waits. "
                            "Optional: at_once (members working at once, default 3), task_budget (tasks in all, default "
                            "6), spend_limit_usd (per turn)."
                        ),
                        "properties": {
                            "mode": {"type": "string", "enum": ["team", "one_at_a_time"]},
                            "at_once": {"type": "integer", "minimum": 1},
                            "task_budget": {"type": "integer", "minimum": 1},
                            "spend_limit_usd": {"type": "number"},
                        },
                    },
                    "tool_policy": {
                        "type": "object",
                        "description": "When the agent must ask a person before a tool runs: {\"tool:<name>\": \"confirm\"} for each tool that books, pays, sends, posts or deletes. The plan's 'Asks you first' list says which. Omit for none.",
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
                                    "enum": ["conversational", "code", "delegate"],
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
                                    "description": "For a Team agent: its members' agent IDs. Empty = allow any agent.",
                                },
                                "team": {
                                    "type": "object",
                                    "description": (
                                        "For a Team agent (type 'delegate') only: how its members work. Omit it and a new Team agent "
                                        "works as a team. {\"mode\": \"team\"}: members work in parallel on what its lead assigns, "
                                        "talking while they work; \"one_at_a_time\": the lead hands work to one member and waits. "
                                        "Optional: at_once (members working at once, default 3), task_budget (tasks in all, default "
                                        "6), spend_limit_usd (per turn)."
                                    ),
                                    "properties": {
                                        "mode": {"type": "string", "enum": ["team", "one_at_a_time"]},
                                        "at_once": {"type": "integer", "minimum": 1},
                                        "task_budget": {"type": "integer", "minimum": 1},
                                        "spend_limit_usd": {"type": "number"},
                                    },
                                },
                                "tool_policy": {
                                    "type": "object",
                                    "description": "When the agent must ask a person before a tool runs: {\"tool:<name>\": \"confirm\"} for each tool that books, pays, sends, posts or deletes. The plan's 'Asks you first' list says which. Omit for none.",
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
                        "description": "Fields to update (name, description, tools, system_prompt, model, etc.; "
                                       "for a Team agent, delegate_agent_ids and team: how its members work, as "
                                       "create_agent takes it)",
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
                    **RUN_CAP_PROPERTIES,
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
                    **RUN_CAP_PROPERTIES,
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
                    **RUN_CAP_PROPERTIES,
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
                "renames, description edits, entry_step_id changes, state_schema updates, turn/timeout "
                "budget changes, or a cap on what one run may spend. Pass only the fields you want to "
                "change."
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
                    **RUN_CAP_PROPERTIES,
                    "strict_state_types": {
                        "type": "boolean",
                        "description": (
                            "Stop a run when a step's text will not become the type its "
                            "state key declares — an llm step answering in prose where the "
                            "key is declared `list`, say. Off by default: the text stays put "
                            "and the run log says so. Turn it on only when asked to."
                        ),
                    },
                    "result_key": {
                        "type": "string",
                        "description": (
                            "The state key whose value is what this orchestration makes: its "
                            "answer when chatted with as an agent, and what a parent gets back "
                            "when it runs nested. Leave empty for the last step that produces "
                            "content (a human step's answer, an evaluator's route label and an "
                            "end step never count). Set it when the result is not the last step's "
                            "output — a workflow ending in an approval of its report, say."
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
                        "enum": ["agent", "llm", "evaluator", "parallel", "merge", "human", "tool", "loop", "transform", "extract_json", "if_else", "switch", "print", "end", "retrieve", "ingest", "workflow", "browser"],
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
                        "description": "JSON string of field definitions. The answer is stored under output_key as a mapping {field name: value}: name the output_key for the step (e.g. `review`), not after a field, type it dict, and read one field as `{state.review.feedback}`. Types: string, textarea, number, boolean, email, date, phone, choice (a dropdown; give it \"options\": [..], one is picked). Example: '[{\"name\":\"feedback\",\"type\":\"textarea\",\"label\":\"Your feedback\"},{\"name\":\"decision\",\"type\":\"choice\",\"label\":\"Decision\",\"options\":[\"Approve\",\"Later\"]}]'",
                    },
                    "forced_tool": {"type": "string", "description": "Tool name for tool steps"},
                    "workflow_id": {"type": "string", "description": "The id of the workflow a workflow step runs, as a nested run (workflow steps)"},
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
                            "Missing keys are treated as None. "
                            + _EXPRESSION_DIALECT
                        ),
                    },
                    "if_true_step_id": {"type": "string", "description": "Step to go to when if_condition is True (if_else steps)"},
                    "if_false_step_id": {"type": "string", "description": "Step to go to when if_condition is False (if_else steps)"},
                    "switch_expression": {
                        "type": "string",
                        "description": (
                            "Expression whose str() result is matched against switch_cases (switch steps). "
                            "Example: `state.category` or `state.result.status.lower()`. "
                            + _EXPRESSION_DIALECT
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
                    # ── Knowledge steps: retrieve / ingest (only while Knowledge is on) ──
                    "retrieve_kb_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Knowledge base ids to search, from list_knowledge_bases (retrieve steps; at most 10)",
                    },
                    "retrieve_query": {"type": "string", "description": "What to search for, with {state.key} placeholders, e.g. `{state.question}` (retrieve steps)"},
                    "retrieve_filters_json": {
                        "type": "string",
                        "description": (
                            "Optional JSON metadata filter (retrieve steps), e.g. '{\"doc_type\":\"contract\",\"year\":{\"$gte\":2024}}'. "
                            "Operators: $eq $ne $in $nin $gt $gte $lt $lte, combined with $and $or $not"
                        ),
                    },
                    "retrieve_top_k": {"type": "integer", "description": "How many passages, 1-50; unset: the knowledge base's own (retrieve steps)"},
                    "retrieve_rerank": {"type": "boolean", "description": "Rerank on or off; unset: the knowledge base's default (retrieve steps)"},
                    "retrieve_snapshot": {"type": "string", "description": "\"active\" (default), or a ksnap_… id to pin ONE knowledge base's index version (retrieve steps)"},
                    "ingest_kb_id": {"type": "string", "description": "Knowledge base id the records are added to, from list_knowledge_bases (ingest steps)"},
                    "ingest_records_from": {"type": "string", "description": "State key holding the list of objects to add, e.g. `tickets` (ingest steps)"},
                    "ingest_id_field": {"type": "string", "description": "Field naming each record, e.g. `ticket_id`: the same id replaces its record. Unset: an id from the record's content (ingest steps)"},
                    "ingest_text_field": {"type": "string", "description": "Field holding the text to index; unset: the whole object is indexed as JSON (ingest steps)"},
                    "ingest_metadata_fields": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Fields copied into each record's metadata, so retrieve steps can filter on them (ingest steps)",
                    },
                    "max_iterations": {"type": "integer", "description": "How many times this step may run in one run (default 3). A route to it after that fails the run, unless on_exhausted_step_id names where to go. Raise it for a cycle a person drives (a revision or clarification loop)."},
                    "on_exhausted_step_id": {"type": "string", "description": "Step the run goes to when a route reaches this step after it has run max_iterations times, e.g. an escalation or a best-effort finish. Unset, the run fails with \"'<step>' gave up after N attempts\"."},
                    # ──────────────────────────────────────────────────────────
                    "max_turns": {"type": "integer", "description": "Max agent turns (default 15)"},
                    "timeout_seconds": {"type": "integer", "description": (
                        "Step timeout (default 300). An agent step whose agent runs a workflow waits "
                        "on that workflow's own timeout_minutes at the default; set another value to cap it")},
                    "model": {"type": "string", "description": "LLM model override for this step"},
                    "pass_images": {"type": "boolean", "description": (
                        "Agent steps only: Sees attachments. The images a person attaches in chat reach this "
                        "step's model (pick one that can read images). A step that runs another workflow "
                        "passes them on to it. Off by default; chat offers Attach only when a step has it.")},
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
                                    "enum": ["agent", "llm", "evaluator", "parallel", "merge", "human", "tool", "loop", "transform", "extract_json", "if_else", "switch", "print", "end", "retrieve", "ingest", "workflow", "browser"],
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
                                "workflow_id": {"type": "string", "description": "The id of the workflow a workflow step runs (workflow steps)"},
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
                                "if_condition": {"type": "string", "description": "Condition for if_else steps (e.g. `state.flag == True`). " + _EXPRESSION_DIALECT},
                                "if_true_step_id": {"type": "string", "description": "Step ID when condition is True"},
                                "if_false_step_id": {"type": "string", "description": "Step ID when condition is False"},
                                "switch_expression": {"type": "string", "description": "Expression to evaluate for switch steps. Same restricted expression syntax as if_condition."},
                                "switch_cases_json": {"type": "string", "description": "JSON string: '{\"value\":\"step_id\"}'"},
                                "switch_default_step_id": {"type": "string", "description": "Fallback step when no case matches"},
                                "retrieve_kb_ids": {"type": "array", "items": {"type": "string"}, "description": "Knowledge base ids to search (retrieve steps)"},
                                "retrieve_query": {"type": "string", "description": "Search query with {state.key} placeholders (retrieve steps)"},
                                "retrieve_filters_json": {"type": "string", "description": "JSON metadata filter (retrieve steps)"},
                                "retrieve_top_k": {"type": "integer"},
                                "retrieve_rerank": {"type": "boolean"},
                                "retrieve_snapshot": {"type": "string", "description": "\"active\" or a ksnap_… id (retrieve steps)"},
                                "ingest_kb_id": {"type": "string", "description": "Knowledge base id the records go into (ingest steps)"},
                                "ingest_records_from": {"type": "string", "description": "State key holding the list of objects (ingest steps)"},
                                "ingest_id_field": {"type": "string"},
                                "ingest_text_field": {"type": "string"},
                                "ingest_metadata_fields": {"type": "array", "items": {"type": "string"}},
                                "max_iterations": {"type": "integer", "description": "Times this step may run in one run (default 3); a route to it after that fails the run unless on_exhausted_step_id is set"},
                                "on_exhausted_step_id": {"type": "string", "description": "Step to go to when this step has used its max_iterations"},
                                "max_turns": {"type": "integer"},
                                "timeout_seconds": {"type": "integer"},
                                "model": {"type": "string"},
                                "pass_images": {"type": "boolean", "description": "Agent steps only: Sees attachments — the images a person attaches in chat reach this step"},
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
                    **_TAG_PARAMS,
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
                    "tags_json": {"type": "string", "description": (
                        "Optional JSON object of operation name → tags, only for an operation riskier than its "
                        "method says, e.g. '{\"send_invoice\": {\"destructive\": true}}'. Tags: read_only, "
                        "destructive, idempotent, open_world. Each operation otherwise takes its method's tags.")},
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
            "name": "list_knowledge_bases",
            "description": (
                "List this workspace's knowledge bases (id, name, description, folder, status, top_k): searchable "
                "folders of documents. A retrieve step searches them by id (retrieve_kb_ids); an ingest step adds "
                "records to one (ingest_kb_id). Only active ones can be used."
            ),
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
                    "timezone": {"type": "string", "description": (
                        "The timezone its cron is read in, as a name like America/New_York or Europe/Amsterdam: the "
                        "one the person named, else their own. Leave it out for UTC")},
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
            "name": "update_schedule",
            "description": (
                "Change a schedule in place: its time (cron_expression, or interval_value + interval_unit), the "
                "message each run starts with (prompt), its name, or what it runs (target_type + target_id). Give "
                "only what changes. It keeps its id, its history and whether it is on. Use this to move a "
                "schedule's time — never delete it and create another. To pause or resume it, use "
                "set_schedule_enabled."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "prompt": {"type": "string", "description": "The message each scheduled run starts with"},
                    "target_type": {"type": "string", "enum": ["agent", "orchestration"]},
                    "target_id": {"type": "string"},
                    "schedule_type": {"type": "string", "enum": ["interval", "cron"]},
                    "interval_value": {"type": "integer"},
                    "interval_unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
                    "cron_expression": {"type": "string", "description": "Standard 5-field cron, read in its timezone"},
                    "timezone": {"type": "string", "description": (
                        "The timezone its cron is read in, as a name like America/New_York or Europe/Amsterdam: the "
                        "one the person named, else their own. Leave it out for UTC")},
                },
                "required": ["schedule_id"],
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
            "description": (
                "Recent workflow runs, newest first: each with its workflow's name, status, step, times, whether it "
                "waits on a person, and `error`, why a failed or cancelled run stopped. `name` keeps one workflow's "
                "runs. Use this to answer questions about what ran and what failed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many runs (default 10, max 50)"},
                    "name": {"type": "string", "description": "Only runs of workflows whose name contains this."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_run_summary",
            "description": (
                "One run step by step: its workflow's name, status, current step, whether it waits on a person, "
                "times, `error` (why it failed) and what each step wrote. A workflow's id reads its latest run."
            ),
            "parameters": {
                "type": "object",
                "properties": {"run_id": {"type": "string", "description": "The run's id, or a workflow's id"}},
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
                "Delete an agent. Destructive: requires confirm=true. Refused while anything uses it "
                "— a widget, a channel or phone number, a schedule, a workflow's step or another agent "
                "that hands work to it — naming each; repoint or delete those first."
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

#: Builder tools defined in their own modules (BWA1, D274): name → `handler(args, server_module, events)`.
#: Each registers itself once, at import, through `register_builder_tool`, so a new tool never touches this file.
_REGISTERED: dict[str, Any] = {}


def register_builder_tool(schema: dict, handler: Any) -> None:
    """Add a builder tool that lives in another module. Its schema joins `BUILDER_TOOL_SCHEMAS` and `_dispatch` hands
    its calls to `handler`, which may `_emit`.

    Opt-in (F495): offered to an agent that names it — the builder's own agents, a forced tool step — never through
    `"all"`, so no other agent's roster, prompt or cache changes when one is added."""
    name = schema["function"]["name"]
    if name in BUILDER_TOOL_NAMES and name not in _REGISTERED:
        raise ValueError(f"builder tool {name!r} already exists")
    if name not in _REGISTERED:
        BUILDER_TOOL_SCHEMAS.append(schema)
        BUILDER_TOOL_NAMES.add(name)
    OPT_IN_BUILDER_TOOLS.add(name)
    _REGISTERED[name] = handler


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

def _agent_policy(value) -> dict:
    """An agent's tool policy as the builder may write it: the categories and `tool:<name>` rules the engine knows,
    nothing else (`tool_policy.clean_policy`, the agents route's own writer)."""
    from core.tool_policy import clean_policy
    return clean_policy(value)


def _long_agent_name(name) -> str | None:
    """The refusal for an agent name past the limit (F217), or None."""
    from core.models import AGENT_NAME_MAX
    if isinstance(name, str) and len(name) > AGENT_NAME_MAX:
        return (f"An agent's name is at most {AGENT_NAME_MAX} characters; this one is "
                f"{len(name)}. Choose a shorter name.")
    return None


_HOLLOW_WORKFLOW_AGENT = (
    "An orchestrator agent runs an orchestration that already exists, so it is not created here: "
    "build that orchestration first, then call deploy_orchestration(orch_id) and use the agent id it "
    "returns. One orchestration is built per build — a nested design is built child first."
)


async def _plan_and_agents_of(run: str | None) -> tuple[str, set[str]]:
    """The plan `run` showed the person and the agents it made, from its last checkpoint; nothing outside a run."""
    if run is None:
        return "", set()
    from core.orchestration.run_writes import WRITES_KEY
    from core.orchestration.state import SharedState

    try:
        state = (await SharedState.restore(run)).run.shared_state or {}
    except FileNotFoundError:
        return "", set()
    made = {w.get("id") for w in state.get(WRITES_KEY) or []
            if isinstance(w, dict) and w.get("kind") == "agent" and w.get("action") == "created"}
    return str(state.get("plan_draft") or ""), made


async def _orphaned_agents(run: str, removed: list[str]) -> list[dict]:
    """The agents `run` made for a build that saved no workflow, which nothing else uses (F492).

    The agent creator runs before the saver, so a saver that stopped ("Tool not found", "Agents are missing")
    left them in the workspace with nothing to run them. Read from the run's own writes at its last checkpoint
    (`run_writes`); a build that kept any workflow keeps its agents. Whether something else uses one is
    `finish_build`'s to ask, as the agent delete asks it (F467).
    """
    from core.orchestration.run_writes import WRITES_KEY
    from core.orchestration.state import SharedState

    try:
        writes = (await SharedState.restore(run)).run.shared_state.get(WRITES_KEY) or []
    except FileNotFoundError:
        return []
    writes = [w for w in writes if isinstance(w, dict)]
    gone = {(w.get("kind"), w.get("id")) for w in writes if w.get("action") == "deleted"}
    built = [w for w in writes if w.get("kind") == "orchestration" and w.get("action") == "created"
             and w.get("id") not in removed and ("orchestration", w.get("id")) not in gone]
    if built:
        return []
    return [{"id": w["id"], "name": w.get("name")} for w in writes
            if w.get("kind") == "agent" and w.get("action") == "created" and w.get("id")
            and ("agent", w.get("id")) not in gone]


#: An issue a person can act on (connect a tool, make an agent) names what is missing in these words (F489, F264).
_ACTIONABLE = ("which nothing here provides", "which does not exist")


def _issues_said(issues: list) -> list[str]:
    """A failed validation's first three issues as a rollback says them (F867, SAI2's conditions): the validator's
    own words, what a person can act on first (SAI2: three wiring issues ahead of the missing tool read as
    plumbing), each cut to a sentence's length, without the closing stop the line adds."""
    ordered = sorted(issues, key=lambda issue: 0 if any(m in str(issue) for m in _ACTIONABLE) else 1)
    said = []
    for issue in ordered[:3]:
        text = " ".join(str(issue).split()).rstrip(". ")
        said.append(text if len(text) <= 160 else text[:159].rsplit(" ", 1)[0] + "…")
    return said


async def _why_nothing_was_built(run: str, removed: list[str]) -> str:
    """Why `run`'s build saved no workflow, when its plan check still names what nothing here provides (F768): the
    tools, the provider its research found for them, and what to do. "" when it saved one, or for another reason."""
    from core.orchestration.run_writes import WRITES_KEY
    from core.orchestration.state import SharedState

    try:
        state = (await SharedState.restore(run)).run.shared_state or {}
    except FileNotFoundError:
        return ""
    writes = [w for w in state.get(WRITES_KEY) or [] if isinstance(w, dict)]
    gone = {(w.get("kind"), w.get("id")) for w in writes if w.get("action") == "deleted"}
    if any(w.get("kind") == "orchestration" and w.get("action") == "created" and w.get("id") not in removed
           and ("orchestration", w.get("id")) not in gone for w in writes):
        return ""
    check = state.get("plan_check") if isinstance(state.get("plan_check"), dict) else {}
    missing = [str(t) for t in (check.get("missing_tools") or []) + (check.get("missing_agents") or [])]
    if not missing:
        return ""
    plan = str(state.get("plan_draft") or "").lower()
    findings = state.get("plan_findings") if isinstance(state.get("plan_findings"), dict) else {}
    found = []
    for option in findings.get("options") or []:
        provider = str(option.get("provider") or "").strip() if isinstance(option, dict) else ""
        if provider and option.get("how") in ("mcp", "api") and provider.lower() in plan:
            way = "its own MCP server" if option["how"] == "mcp" else "a public API"
            page = next(iter(option.get("sources") or []), "")
            found.append(f"{provider} has {way}" + (f" ({page})" if page else ""))
    names = ", ".join(f"`{t}`" for t in missing[:5])
    return ("Nothing was built: this plan uses " + names + ", which nothing in this workspace provides yet. "
            + (("The research found: " + "; ".join(found) + ". ") if found else "")
            + "Connect the app or service it needs, then ask me to build it again.")


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
        # Only what the run's person may do (ASSIST1, F681): a write is refused below builder, by their role now.
        from core.builder_access import acting_for, refusal
        refused, caller = await refusal(tool_name, run_id)
        if refused:
            return json.dumps({"error": refused}), []
        from core.native_builder import turn_facts
        if why := await turn_facts.answer_card_refusal(run_id, tool_name, args or {}):
            return json.dumps({"error": why}), []
        with acting_for(caller):
            result = await _dispatch(tool_name, args, server_module, events)
        # What the call put up is kept on its turn's card, for the next turn to read in code (F977).
        await turn_facts.keep(run_id, getattr(caller, "user_id", None), **turn_facts.facts_of(events))
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
    reason: str | None = None,
) -> None:
    """Record that the builder changed something the workspace can see. `reason`: why a removal happened, which
    the run's own summary says (`run_writes`)."""
    events.append({
        "type": "resource_saved",
        "kind": kind,           # agent | orchestration | tool | skill | schedule
        "action": action,       # created | updated | deleted
        "id": resource_id,
        "name": name,
        "resource": resource,
        **({"reason": reason} if reason else {}),
    })


#: The builder tools that read the whole session set (`agent_sessions`, or
#: `aggregate_all_tools` over it) rather than one agent's tools. A fleet turn
#: that may call one of them is given every MCP server, because what they list
#: is whatever the module holds (VOX-MCP, `core/scale/mcp_needs.py`). A new
#: tool below that reads the sessions belongs here;
#: `test_mcp_needs.py` checks this list against `_dispatch`.
READS_EVERY_SESSION: frozenset[str] = frozenset({
    "list_all_tools",
    "get_tools_detail",
    "list_tool_servers",
    "list_server_tools",
})


def _team_block(agent_type: Any, team: Any, *, new: bool) -> tuple[dict | None, str | None]:
    """A Team agent's `team` block as the builder saves it (TEAM1e; TEAM1a's `AgentTeam`), or why not.

    Only a Team agent (stored `delegate`) has one. A new one the builder makes works as a team unless the call
    says otherwise, as a new Team agent in the editor does (20 Q1); a delegate saved before teams keeps none (D204).
    """
    if agent_type != "delegate":
        if team is not None:
            return None, ("Not saved: only a Team agent (type 'delegate') has members and a `team` block. Leave "
                          "`team` out, or make it a Team agent with its members in delegate_agent_ids.")
        return None, None
    from pydantic import ValidationError
    from core.models import AgentTeam
    if team is None:
        return (AgentTeam(mode="team").model_dump() if new else None), None
    try:
        return AgentTeam(**team).model_dump(), None
    except (TypeError, ValidationError) as exc:
        return None, f"Not saved: the team settings are not valid ({exc})."


async def _workspace_catalogue(server_module) -> list:
    """Every tool this workspace has, for the builder's discovery (`list_all_tools`, `get_tools_detail`).

    Asked of the workspace, as `builder_guard`'s missing-tool check asks it (`{"tools": ["all"]}`), never of one
    agent's list. It used to borrow the first non-system agent's: on a workspace whose first agent holds no tools,
    the builder was told the workspace's own tools did not exist (F866, QA1c on production).
    """
    from core.tools import aggregate_all_tools
    from core.routes.tools import load_custom_tools
    return (await aggregate_all_tools(server_module, {"tools": ["all"]}, await load_custom_tools()))[0]


async def _steps_as_run_calls(orch: dict, server_module: Any) -> dict:
    """A workflow's Tool steps saved with their tool as a run calls it (F1035): a step the builder wrote as
    `web_scraper.scrape_url`, or with an external server's bare name, would fail its first run."""
    from core import builder_guard

    steps = [s for s in orch.get("steps") or [] if isinstance(s, dict) and isinstance(s.get("forced_tool"), str)
             and s.get("forced_tool")]
    if steps:
        fixed = await builder_guard.canonical_names([s["forced_tool"] for s in steps], server_module)
        for step, name in zip(steps, fixed):
            step["forced_tool"] = name
    return orch


async def _dispatch(tool_name: str, args: dict, server_module: Any, events: list[dict]) -> Any:
    if tool_name in _REGISTERED:  # a tool from its own module (`register_builder_tool`)
        return await _REGISTERED[tool_name](args, server_module, events)
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
        team_block, team_refusal = _team_block(args.get("type"), args.get("team"), new=True)
        if team_refusal:
            return {"error": team_refusal}
        if args.get("type") == "orchestrator":
            return {"error": _HOLLOW_WORKFLOW_AGENT}
        if (too_long := _long_agent_name(args.get("name"))) is not None:
            return {"error": too_long}
        from core import builder_guard  # F489: never a tool nothing provides
        if (phantom := await builder_guard.unknown(args.get("tools") or [], server_module)) is not None:
            return phantom
        if args.get("tools"):  # saved as a run calls them (F1035): `web_scraper.scrape_url` → `scrape_url`
            args = {**args, "tools": await builder_guard.canonical_names(args["tools"], server_module)}
        if (refusal := await _refused("agent", 1)) is not None:
            return refusal
        agents = await load_user_agents()
        existing_ids = {a.get("id") for a in agents}
        # Guarantee uniqueness: the id is a primary key, and on cloud it is a
        # global one, minted the way cloud's routes mint (F431). A millisecond
        # timestamp collides under any concurrency.
        new_id = _mint_id("agent", length=10)
        while new_id in existing_ids:
            new_id = _mint_id("agent", length=10)
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
        if team_block is not None:
            agent["team"] = team_block
        if (policy := _agent_policy(args.get("tool_policy"))):
            agent["tool_policy"] = policy
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
                if spec.get("type") == "orchestrator":
                    errors.append({"index": i, "role": spec.get("role"), "error": _HOLLOW_WORKFLOW_AGENT})
                    continue
                team_block, team_refusal = _team_block(spec.get("type"), spec.get("team"), new=True)
                if team_refusal:
                    errors.append({"index": i, "role": spec.get("role"), "error": team_refusal})
                    continue
                if (too_long := _long_agent_name(spec["name"])) is not None:
                    errors.append({"index": i, "role": spec.get("role"), "error": too_long})
                    continue
                from core import builder_guard  # F489: never a tool nothing provides
                if (phantom := await builder_guard.unknown(spec.get("tools") or [], server_module)) is not None:
                    errors.append({"index": i, "role": spec.get("role"), "error": phantom["error"]})
                    continue
                if spec.get("tools"):  # saved as a run calls them (F1035)
                    spec = {**spec, "tools": await builder_guard.canonical_names(spec["tools"], server_module)}
                # Guarantee uniqueness even when called repeatedly in the same millisecond.
                new_id = _mint_id("agent", length=10)
                while new_id in existing_ids:
                    new_id = _mint_id("agent", length=10)
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
                if team_block is not None:
                    agent["team"] = team_block
                if (policy := _agent_policy(spec.get("tool_policy"))):
                    agent["tool_policy"] = policy
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
        if "tools" in fields:
            from core import builder_guard  # F489: never a tool nothing provides
            if (phantom := await builder_guard.unknown(fields.get("tools") or [], server_module)) is not None:
                return phantom
            fields["tools"] = await builder_guard.canonical_names(fields.get("tools") or [], server_module)  # F1035
        if "tool_policy" in fields:
            fields["tool_policy"] = _agent_policy(fields["tool_policy"])
        if fields.get("team") is not None:
            # A Team's "How members work" (TEAM1a): checked as the agent editor's save checks it, and only on a
            # Team agent (TEAM1e).
            team_block, team_refusal = _team_block(fields.get("type", agent.get("type")), fields["team"], new=False)
            if team_refusal:
                return {"error": team_refusal}
            fields["team"] = team_block
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
                all_tools = await _workspace_catalogue(server_module)
                return [
                    {"name": t.name, "description": (t.description or "")[:120]}
                    for t in all_tools[:limit]
                ]
        except Exception as e:
            return {"error": f"Could not list tools: {e}"}

    elif tool_name == "get_tools_detail":
        try:
            all_tools = await _workspace_catalogue(server_module)
            requested = set(args.get("tool_names", []))
            # Each asked-for name as a run calls it (F1035): `firecrawl_scrape`, as list_server_tools printed it,
            # is the catalogue's `firecrawl__firecrawl_scrape`.
            from core import builder_guard

            asked = [n for n in requested if isinstance(n, str)]
            as_called = dict(zip(asked, await builder_guard.canonical_names(asked, server_module)))
            by_name = {t.name: t for t in all_tools}
            result = {}
            for name in asked:
                t = by_name.get(as_called[name])
                if t is not None:
                    result[name] = {
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
            # Each tool by the name a run calls it (F1035): an external server's are `server__tool`, as the catalogue,
            # the resolver and every saved agent hold them. Printed bare, they were the names the builder then used
            # and could not find.
            session_key = next((k for k, v in server_module.agent_sessions.items() if v is session), server_name)
            from core.tool_router import SEP

            def called(tool_name: str) -> str:
                router = getattr(server_module, "tool_router", None)
                key = router.key_for(session_key, tool_name) if hasattr(router, "key_for") else None
                if session_key.startswith("ext_mcp_"):
                    return key or f"{session_key[len('ext_mcp_'):]}{SEP}{tool_name}"
                # A native server's tool is called bare, unless another server won its bare name: then its key.
                if key and hasattr(router, "declares") and not router.declares(session_key, tool_name):
                    return key
                return tool_name

            return [
                {"name": called(t.name), "description": (t.description or "")[:150]}
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
        orch_id = _mint_id("orch")
        while orch_id in existing_ids:
            orch_id = _mint_id("orch")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        _normalize_state_schema_arg(args)
        _normalize_steps_arg(args)
        steps = args.get("steps", [])
        if not isinstance(steps, list) or not steps:
            return {"error": "steps_json must decode to a non-empty JSON array of step objects"}
        steps = _fill_step_defaults(steps)
        refused = _refuse_misshapen_steps(steps)
        if refused:
            return refused
        refused = _refuse_ungated_transform(steps)
        if refused:
            return refused
        refused = _refuse_knowledge_off(steps)
        if refused:
            return refused
        refused = _refuse_unsafe_expression(steps)
        if refused:
            return refused
        try:
            caps = _run_cap_args(args)
        except ValueError as e:
            return {"error": str(e)}

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
            **{k: v for k, v in caps.items() if v is not None},
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
        _emit(events, "orchestration", "created", orch["id"], orch["name"], orch)
        return {"status": "created", "orchestration": orch}

    elif tool_name == "update_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        # update_orchestration now accepts flat top-level fields (like the other
        # builder tools), falling back to legacy `fields` for back-compat — for the
        # fields it declares only, on this workflow only (F784). Written whole, an
        # `id` in it overwrote another workflow (a system or deleted one too, as the
        # check above read `orch_id`), and `rubric_id`, `system` or `is_active` went in
        # unchecked. An echo of this workflow's own id or times is ignored.
        legacy_fields = args.get("fields") or {}
        if not isinstance(legacy_fields, dict):
            return {"error": "`fields` must be an object of the fields to set."}
        echoed = {k for k in ("created_at", "updated_at") if k in legacy_fields}
        if legacy_fields.get("id") == orch["id"]:
            echoed.add("id")
        settable = _UPDATE_ORCHESTRATION_FIELDS | set(RUN_CAP_PROPERTIES)
        refused_keys = sorted(set(legacy_fields) - settable - echoed)
        if refused_keys:
            return {"error": f"update_orchestration cannot set {', '.join(refused_keys)}: it sets "
                             f"{', '.join(sorted(settable))}, on the workflow `orch_id` names."}
        fields = {k: v for k, v in legacy_fields.items() if k in settable}
        for key in ("name", "description", "entry_step_id", "state_schema_json",
                    "steps_json", "max_total_turns", "timeout_minutes"):
            if key in args and args[key] is not None:
                fields[key] = args[key]
        try:
            fields.update(_run_cap_args({**legacy_fields, **args}))
        except ValueError as e:
            return {"error": str(e)}
        _normalize_state_schema_arg(fields)
        _normalize_steps_arg(fields)
        if "steps" in fields and isinstance(fields["steps"], list):
            fields["steps"] = _fill_step_defaults(fields["steps"])
            refused = _refuse_misshapen_steps(fields["steps"])
            if refused:
                return refused
            refused = _refuse_ungated_transform(fields["steps"])
            if refused:
                return refused
            refused = _refuse_knowledge_off(fields["steps"])
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
        orch["id"] = args["orch_id"]  # the id is not patchable (F784), as `update_agent`'s is not
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "updated", "orchestration": orch}

    elif tool_name == "create_orchestration_skeleton":
        if (refusal := await _refused("orchestration", 1)) is not None:
            return refusal
        existing_ids = {o.get("id") for o in await load_orchestrations()}
        orch_id = _mint_id("orch")
        while orch_id in existing_ids:
            orch_id = _mint_id("orch")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _normalize_state_schema_arg(args)
        try:
            caps = _run_cap_args(args)
        except ValueError as e:
            return {"error": str(e)}
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
            **{k: v for k, v in caps.items() if v is not None},
            "trigger": "manual",
            "created_at": now,
            "updated_at": now,
        }
        if (run := _current_run.get()) is not None:
            orch["building_run"] = run  # cleared when it validates (F156)
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
        refused = _refuse_misshapen_steps(filled)
        if refused:
            return refused
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_knowledge_off(filled)
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
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
        refused = _refuse_misshapen_steps(filled)
        if refused:
            return refused
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_knowledge_off(filled)
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
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
        refused = _refuse_misshapen_steps(filled)
        if refused:
            return refused
        refused = _refuse_ungated_transform(filled)
        if refused:
            return refused
        refused = _refuse_knowledge_off(filled)
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
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
                    "max_total_turns", "timeout_minutes", "strict_state_types", "result_key"):
            if key in args and args[key] is not None:
                raw_fields[key] = args[key]
        _normalize_state_schema_arg(raw_fields)
        allowed = {"name", "description", "entry_step_id", "state_schema",
                   "max_total_turns", "timeout_minutes", "strict_state_types", "result_key"}
        fields = {k: v for k, v in raw_fields.items() if k in allowed}
        try:
            fields.update(_run_cap_args({**dict(args.get("fields") or {}), **args}))
        except ValueError as e:
            return {"error": str(e)}
        if not fields:
            return {"error": f"No allowed meta fields to set. Allowed: {sorted(allowed | set(RUN_CAP_PROPERTIES))}"}
        orch.update(fields)
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
        refused = _refuse_misshapen_steps([steps[sidx]])
        if refused:
            return refused
        refused = _refuse_ungated_transform([steps[sidx]])
        if refused:
            return refused
        refused = _refuse_knowledge_off([steps[sidx]])
        if refused:
            return refused
        refused = _refuse_unsafe_expression([steps[sidx]])
        if refused:
            return refused
        orch["steps"] = steps
        orch["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
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
        await save_orchestration(await _steps_as_run_calls(orch, server_module))
        _emit(events, "orchestration", "updated", orch["id"], orch.get("name"), orch)
        return {"status": "step_removed", "step_id": step_id, "remaining_steps": len(steps)}

    elif tool_name == "deploy_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        from core.orchestration.deploy import deploy_as_agent
        # A new workflow agent is an agent row like any other, and asks for room
        # first (F436, F154's posture); one already made is only answered.
        already = any(a.get("orchestration_id") == orch["id"] for a in await load_user_agents())
        if not already and (refusal := await _refused("agent", 1)) is not None:
            return refusal
        result = await deploy_as_agent(orch["id"])
        if result.get("agent"):
            _emit(events, "agent", "created", result["agent_id"], result["agent"]["name"], result["agent"])
        # A deployed workflow keeps its build's mark: the parent that calls it
        # validates it and finishes it, and `finish_build` keeps it regardless (F429).
        return result

    elif tool_name == "validate_orchestration":
        orch = await _visible_orchestration(args["orch_id"])
        if orch is None:
            return {"error": f"Orchestration '{args['orch_id']}' not found"}
        # The agents its steps name, so a step whose agent is missing or runs no
        # orchestration is caught here rather than on the first run (F264).
        agents = {a.get("id"): a for a in await load_user_agents() if a.get("id")}
        # And the workflows it calls, at any depth: a parent passes only with what
        # it calls, so a nested build is validated, and finished, whole (F429).
        called, issues = await _workflows_called(orch, agents)
        # And the knowledge bases its Retrieve and Ingest steps name, while Knowledge is on.
        knowledge_on = builder_tool_offered("list_knowledge_bases")
        kbs = None
        if knowledge_on and any(s.get("type") in KNOWLEDGE_STEP_TYPES for w in (orch, *called) for s in w.get("steps") or []):
            from core.knowledge import bases
            kbs = {kb.id: kb for kb in await bases.list_bases()}
        verdict = _validate_orchestration(orch, agents=agents, kbs=kbs, knowledge_off=not knowledge_on)
        issues = [*verdict["issues"], *issues]
        # The inner workflows this build made are checked with it; the workspace's
        # own, which run today, only have to exist — never refused, never rewritten.
        run = _current_run.get()
        ours = [child for child in called if run is not None and child.get("building_run") == run]
        for child in ours:
            child_verdict = _validate_orchestration(child, agents=agents, kbs=kbs, knowledge_off=not knowledge_on,
                                                    allow_dead_ends=True)
            issues += [f"In the inner workflow '{child.get('name')}' ({child['id']}): {i}"
                       for i in child_verdict["issues"]]
            verdict["warnings"] = [*verdict.get("warnings", []), *(
                f"In the inner workflow '{child.get('name')}' ({child['id']}): {w}"
                for w in child_verdict.get("warnings", []))]
        # F489: a forced tool nothing provides fails the first run; an agent naming one runs without it.
        from core import builder_guard
        tool_issues, tool_warnings = await builder_guard.missing_in([orch, *ours], agents, server_module)
        issues += tool_issues
        verdict["warnings"] = [*verdict.get("warnings", []), *tool_warnings]
        # E1: a Tool step's set arguments fit its tool, or every run fails.
        issues += await builder_guard.argument_issues([orch, *ours], server_module)
        # Decision 4 (BWA1): in a build of its own, nothing that changes something outside runs with nobody
        # approving it, except what the approved plan lets act without asking. A workflow it only edits is warned.
        plan, made = await _plan_and_agents_of(run)
        guard_issues, guard_warnings = await builder_guard.unapproved(
            [orch, *ours], agents, server_module, made_agents=made, exempt=builder_guard.exempt_in_plan(plan),
            strict=run is not None and orch.get("building_run") == run)
        issues += guard_issues
        verdict["warnings"] = [*verdict.get("warnings", []), *guard_warnings]
        verdict = {**verdict, "ok": not issues, "issues": issues}
        if verdict["ok"]:
            for workflow in (orch, *ours):
                workflow.pop("building_issues", None)
                if workflow.pop("building_run", None) is not None:
                    await save_orchestration(await _steps_as_run_calls(workflow, server_module))  # finished: `finish_build` keeps it
        elif run is not None and orch.get("building_run") == run:
            # Kept with the build in the making, for `finish_build`'s line if it is rolled back (F867).
            said = _issues_said(issues)
            if orch.get("building_issues") != said:
                orch["building_issues"] = said
                await save_orchestration(await _steps_as_run_calls(orch, server_module))
        return verdict

    elif tool_name == "finish_build":
        # F156: remove what this run's saver started and never validated. Never
        # another run's, and nothing at all outside a run. F429: and never what
        # something still runs — a workflow agent (which is how any other
        # workflow calls it) or a schedule. That is kept, and said.
        run = _current_run.get()
        removed: list[str] = []
        removed_names: list[str] = []
        kept: list[dict] = []
        mine = [o for o in await load_orchestrations() if o.get("building_run") == run] if run is not None else []
        if mine:
            from core.store import schedules as schedule_store
            from core.store.resources import delete_orchestration as _delete_orch
            deployed = {a.get("orchestration_id") for a in await load_user_agents() if a.get("orchestration_id")}
            scheduled = {s.get("target_id") for s in await schedule_store.load()
                         if s.get("target_type") == "orchestration"}
            for orch in mine:
                reasons = [why for why, holds in (("a workflow agent runs it", orch["id"] in deployed),
                                                  ("a schedule runs it", orch["id"] in scheduled)) if holds]
                if reasons:
                    orch.pop("building_run", None)
                    orch.pop("building_issues", None)
                    await save_orchestration(await _steps_as_run_calls(orch, server_module))
                    kept.append({"id": orch["id"], "name": orch.get("name"), "reason": " and ".join(reasons)})
                    continue
                await _delete_orch(orch["id"])
                # Why, in the validator's words (F867): the person cannot fix what they are not told.
                said = orch.get("building_issues") or []
                _emit(events, "orchestration", "deleted", orch["id"], orch.get("name"),
                      reason=("it never passed validation: " + "; ".join(said)) if said else None)
                removed.append(orch["id"])
                removed_names.append(orch.get("name") or orch["id"])
        if run is not None:
            from core import agent_memory
            from core.orchestration.dependents import what_uses_agent
            from core.store.resources import delete_agent as _delete_agent
            left = await _orphaned_agents(run, removed)
            # As the agent delete does (F467, F469): one something uses stays, and its memory goes with it. A lead
            # the build made goes on one pass, so the member it handed work to is free on the next.
            while left:
                free = [agent for agent in left if not await what_uses_agent(agent["id"])]
                if not free:
                    break
                for agent in free:
                    await _delete_agent(agent["id"])
                    await agent_memory.wipe(agent["id"])
                    _emit(events, "agent", "deleted", agent["id"], agent.get("name"),
                          reason="the build stopped before its workflow was saved")
                    removed_names.append(agent.get("name") or agent["id"])
                left = [agent for agent in left if agent not in free]
        stopped = await _why_nothing_was_built(run, removed) if run is not None else ""
        if stopped:
            events.append({"type": "build_stopped", "reason": stopped})
        return {"status": "finished", "removed": removed, "removed_names": removed_names, "kept": kept,
                **({"stopped": stopped} if stopped else {})}

    # ── Custom HTTP tools ────────────────────────────────────────────────────

    elif tool_name == "list_custom_tools":
        from core.routes.tools import load_custom_tools
        from core.tool_tags import tags_of
        return [
            {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "method": t.get("method", "GET"),
                "url": t.get("url", ""),
                "group": t.get("group"),
                "tool_type": t.get("tool_type", "http"),
                # What it says it does (TAG1); "not set" until someone saves it, and who set them when not a person.
                "tags": tags_of(t.get("annotations")) or "not set",
                **({"tags_by": t["annotations_by"]} if t.get("annotations_by") else {}),
            }
            for t in await load_custom_tools()
        ]

    elif tool_name == "create_http_tool":
        from core.store.resources import save_tool

        name = _tool_name(args.get("name"))
        if not name:
            return {"error": "name must be a tool-style identifier (letters, digits, underscores)"}
        url = str(args.get("url") or "").strip()
        from core.builder_guard import PLACES_CALLS, places_calls
        if places_calls(url, args.get("method")):
            # Outbound calls wait for VOX2's consent model (V20, F497): the builder never makes a tool that dials.
            return {"error": PLACES_CALLS}
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
        # Its method's tags, made stricter by what it claims, marked as the builder's: believed when they restrict, as a
        # server's are (F488, TAG1). Replacing a tool the person tagged starts from their tags.
        from core.store.resources import load_tools
        before = next((t for t in await load_tools() if t.get("name") == name), None)
        # A tool made from a read API keeps to its base (F944, A8): only code writes a lock, and a builder never
        # moves a locked tool off it; replacing it carries the lock forward.
        from core.http_tool import under_lock
        from core.http_tool import MAKING_TOOLS
        # The stored lock, or a new one only from the build's own save (SAI2's review, blocker 1): never a caller's.
        lock = str((before or {}).get("base_lock") or (args.get("base_lock") if MAKING_TOOLS.get() else "") or "")
        if lock and not under_lock(url, lock):
            return {"error": f"`{name}` was made for {lock}, and keeps to that API: a tool for another address is a "
                             "new tool, under another name."}
        if lock:
            tool["base_lock"] = lock
            if before and before.get("base_lock"):
                # A re-create within its API keeps the key its Connect card saved (SAI2's nit).
                tool["headers"] = {**(before.get("headers") or {}), **headers}
        claimed = {t: args[t] for t in ("read_only", "destructive", "idempotent", "open_world") if t in args}
        await save_tool(_builder_tags(tool, before, claimed))
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

        # An operation that starts a phone call is left out, and said (F497); the rest of the API still comes in.
        from core.builder_guard import PLACES_CALLS, places_calls
        left_out = [t["name"] for t in generated if places_calls(t.get("url") or "", t.get("method"))]
        generated = [t for t in generated if t["name"] not in left_out]
        if not generated:
            return {"error": PLACES_CALLS}

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
        claims = _parse_json_field(args.get("tags_json"), {})
        claims = claims if isinstance(claims, dict) else {}
        prefix = str(args.get("name_prefix") or "")
        from core.store.resources import load_tools
        before = {t.get("name"): t for t in await load_tools()}

        def _claimed(name: str):
            claim = claims.get(name, claims.get(name[len(prefix):] if prefix and name.startswith(prefix) else None))
            return claim if isinstance(claim, dict) else None

        await save_tools([_builder_tags(t, before.get(t["name"]), _claimed(t["name"])) for t in generated])
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
            **({"left_out": left_out, "why": PLACES_CALLS} if left_out else {}),
        }

    elif tool_name == "delete_custom_tool":
        refused = _require_confirm(args, f"delete the tool '{args.get('name')}'")
        if refused:
            return refused
        from core.store.resources import delete_tool_named
        name = str(args.get("name") or "")
        if not await delete_tool_named(name):
            return {"error": f"Custom tool '{name}' not found"}
        _emit(events, "tool", "deleted", name, name)
        return {"status": "deleted", "id": name, "name": name}

    # ── Skills ───────────────────────────────────────────────────────────────

    elif tool_name == "list_knowledge_bases":
        if not builder_tool_offered(tool_name):
            return {"error": f"{_KNOWLEDGE_OFF}: there are no knowledge bases to list, and retrieve and "
                             f"ingest steps are not available."}
        from core.knowledge import bases
        return [{"id": kb.id, "name": kb.name, "description": kb.description, "folder": kb.folder,
                 "status": kb.status, "top_k": kb.retrieval.top_k}
                for kb in await bases.list_bases()]

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
        # The routes' own check (F197): -5 minutes is a next run in the past.
        from core.models_schedule import ScheduleInvalid, validate_timing
        try:
            validate_timing(schedule)
        except ScheduleInvalid as exc:
            return {"error": f"Invalid schedule: {exc}"}
        # F155 (D122): always created paused, whatever was asked. The builder's
        # schedules began each run with a restatement of the task rather than its
        # input ("Pull the latest blog posts from our client's three competitors"
        # with none named), so every run would have chosen its own subject. A
        # person reads what it starts with and turns it on — `set_schedule_enabled`
        # when they ask the builder, or the Schedules screen.
        schedule["enabled"] = False
        schedule["id"] = _mint_id("sched", length=8)
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

    elif tool_name == "update_schedule":
        # F578: the builder could only delete a schedule and create another to move its time, which lost its id
        # and history and left the new one paused (F155). Patched as the routes' PATCH does; never its enabled
        # state, which stays a person's to turn on (set_schedule_enabled when they ask).
        from pydantic import ValidationError

        from core.models_schedule import ScheduleInvalid, ScheduleUpdate, validate_timing
        from core.scheduler import _iso, _utc_now, compute_next_run
        from core.store import schedules as schedule_store

        schedule_id = str(args.get("schedule_id") or "")
        schedule = await schedule_store.get(schedule_id)
        if schedule is None:
            return {"error": f"Schedule '{schedule_id}' not found"}
        try:
            patch = ScheduleUpdate.model_validate({
                k: v for k, v in args.items()
                if k in ScheduleUpdate.model_fields and k != "enabled" and v is not None
            }).model_dump(exclude_none=True)
        except ValidationError as exc:
            return {"error": f"Invalid schedule: {exc.errors()[0]['msg']}"}
        if not patch:
            return {"error": "Nothing to change: give the time, prompt, name or target that changes."}
        patched = {**schedule, **patch}
        if {"target_type", "target_id"} & patch.keys():
            if patched.get("target_type") == "agent":
                target = await _load_agent(str(patched.get("target_id") or ""))
                if target is None or target.get("system"):
                    return {"error": f"Agent '{patched.get('target_id')}' not found"}
            elif await _visible_orchestration(str(patched.get("target_id") or "")) is None:
                return {"error": f"Orchestration '{patched.get('target_id')}' not found"}
        if {"schedule_type", "interval_value", "interval_unit", "cron_expression", "timezone"} & patch.keys():
            try:
                validate_timing(patched)
                patched["next_run_at"] = _iso(compute_next_run(patched, _utc_now()))
            except ScheduleInvalid as exc:
                return {"error": f"Invalid schedule: {exc}"}
            except Exception as exc:
                return {"error": f"Could not work out when that would next run: {exc}"}
        await schedule_store.save(patched)
        _emit(events, "schedule", "updated", schedule_id, patched.get("name"), patched)
        return {"status": "updated", "id": schedule_id, "name": patched.get("name"),
                "changed": sorted(patch), "enabled": patched.get("enabled"), "next_run_at": patched.get("next_run_at")}

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
        from core.secret_scrub import held_secrets

        # Keys and a short preview only. A variable holds things like an API
        # token; the builder needs to know it exists and how to reference it,
        # never what it says. A secret previews by name: cut to 40 characters it
        # no longer matched the value held to scrub, and its start was shown (F854).
        values = await variables.load()
        secrets = held_secrets()
        return [
            {"key": key, "preview": "‹secret›" if key in secrets else str(value)[:40],
             "reference": f"{{{{ global.{key} }}}}"}
            for key, value in sorted(values.items())
        ]

    elif tool_name == "list_recent_runs":
        from core.orchestration.state import SharedState

        limit = max(1, min(int(args.get("limit") or 10), 50))
        names = {o["id"]: o.get("name") for o in await load_orchestrations() if _is_visible(o) and o.get("id")}
        wanted = str(args.get("name") or "").strip().lower()
        if wanted:
            # One workflow's runs, by its name (F756): asked "is the invoice sync running?", the model read the
            # workspace's latest run, another workflow's, as Invoice sync's.
            names = {i: n for i, n in names.items() if wanted in str(n or "").lower()}
            if not names:
                return {"error": f"No workflow's name contains {args.get('name')!r}; list_orchestrations lists them."}
        runs = await SharedState.list_runs(limit=limit, orchestration_ids=set(names))
        return [
            {
                "run_id": r.get("run_id"),
                "orchestration_id": r.get("orchestration_id"),
                "status": r.get("status"),
                "current_step_id": r.get("current_step_id"),
                "started_at": r.get("started_at"),
                "ended_at": r.get("ended_at"),
                # What `read_workspace`'s `runs` says too (F756): whose run, why it stopped, whether it waits.
                "workflow": names.get(r.get("orchestration_id")),
                "error": r.get("error"),
                "waiting_for_human": bool(r.get("waiting_for_human")),
            }
            for r in runs
        ]

    elif tool_name == "get_run_summary":
        from core.builder_readers import read_workspace
        from core.orchestration.state import SharedState
        from core.routes.orchestrations import _run_summary

        run_id = str(args.get("run_id") or "").strip()
        flow = await _visible_orchestration(run_id)
        if flow is not None:
            # A workflow's id for a run's (F756: did-02 passed `orch_…` and heard "no checkpoint"): its latest run.
            latest = await SharedState.list_runs(limit=1, orchestration_ids={run_id})
            if not latest:
                return {"error": f"The workflow '{flow.get('name')}' has not run yet."}
            run_id = latest[0]["run_id"]
        summary = await _run_summary(run_id)
        if summary is None:
            return {"error": f"Run '{args.get('run_id')}' has no checkpoint"}
        if await _visible_orchestration(summary.get("orchestration_id")) is None:
            return {"error": f"Run '{args.get('run_id')}' has no checkpoint"}
        # The run step by step from `read_workspace`'s `run` reader, the host's where it has one (F756): its
        # workflow's name, why it failed, what each step wrote. The snapshot's own fields stay as they were.
        view = await read_workspace({"topic": "run", "run_id": run_id}, server_module, [])
        if "status" not in view:  # the reader refused or failed; a failed run's view has `error` and a status
            return {**summary, "note": f"The steps could not be read: {view['error']}"}
        view.pop("topic", None)
        return {**view, **summary}

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
        # Not while something uses it, in the route's words (F467): it used to be
        # deleted anyway, with the workflow steps it left pointing at nothing listed.
        from core.orchestration.dependents import agent_refusal, what_uses_agent
        users = await what_uses_agent(agent_id)
        if users:
            return {"error": agent_refusal(agent.get("name") or agent_id, users)}
        await _delete_agent(agent_id)
        # Its memory goes with it, as the route's delete takes it (F297, F469).
        from core import agent_memory
        await agent_memory.wipe(agent_id)
        _emit(events, "agent", "deleted", agent_id, agent.get("name"))
        return {
            "status": "deleted",
            "id": agent_id,
            "name": agent.get("name"),
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
        # Never one a workflow agent or a schedule runs (F408): the same rule
        # as the Orchestrations screen's delete, in the same words.
        from core.orchestration.dependents import refusal, what_runs
        runners = await what_runs(orch_id)
        if runners:
            return {"error": refusal(orch.get("name") or orch_id, runners)}
        await _delete_orch(orch_id)
        _emit(events, "orchestration", "deleted", orch_id, orch.get("name"))
        return {"status": "deleted", "id": orch_id, "name": orch.get("name")}

    else:
        return {"error": f"Unknown tool: {tool_name}"}


#: Steps that exist to produce a value, so losing it is worth an issue.
_RESULT_STEP_TYPES = frozenset({"agent", "llm", "tool", "merge", "transform", "extract_json", "human", "retrieve",
                                "workflow"})


def _knowledge_step_issues(step: dict, kbs: dict | None, knowledge_off: bool) -> list[str]:
    """A Retrieve or Ingest step's settings held to the contract, and the bases it names checked against the
    workspace's (R1). `kbs` (id → base) is None when they were not looked up: then only the settings are."""
    from core.knowledge import workflow_steps as W

    sid = step.get("id", "?")
    kind = "Retrieve" if step.get("type") == "retrieve" else "Ingest"
    if knowledge_off:
        return [f"{kind} step '{sid}' cannot run while Knowledge is switched off in this workspace — remove it."]
    try:
        if kind == "Retrieve":
            named = W.retrieve_config(step.get("retrieve"), step_name=sid).kb_ids
        else:
            named = [W.ingest_config(step.get("ingest"), step_name=sid).kb_id]
    except W.StepFailed as exc:
        return [f"{exc} — set it with the {kind.lower()}_* fields."]
    issues = []
    for kb_id in named if kbs is not None else []:
        kb = kbs.get(kb_id)
        if kb is None:
            issues.append(f"{kind} step '{sid}' uses knowledge base '{kb_id}', which does not exist — use an id "
                          f"list_knowledge_bases returns.")
        elif kb.status != "active":
            issues.append(f"{kind} step '{sid}' uses knowledge base '{kb.name}' ({kb_id}), which is disabled.")
    return issues


def step_successors(s: dict, by_id: dict) -> list[str]:
    """The steps a step can go to next — its next step, routes, loop body, branches, if/else and switch targets —
    that exist. The validator's walk and the approval guard (`builder_guard.unapproved`) read the graph the same way."""
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
    for ref_key in ("if_true_step_id", "if_false_step_id", "switch_default_step_id", "on_exhausted_step_id"):
        ref = s.get(ref_key)
        if ref and ref in by_id:
            successors.append(ref)
    for target in (s.get("switch_cases") or {}).values():
        if target and target in by_id:
            successors.append(target)
    return successors


#: Tools that read a file's whole content into state (their bare names).
_FILE_READERS = frozenset({"vault_read", "read_file", "read_text_file", "read_multiple_files"})


def _file_content_into_code(step: dict, steps: list[dict]) -> list[str]:
    """A code runner's program fed a file's content from state (F1212, CAP3's stage 0): the code should read the file
    where the sandbox mounts the vault, `/data/<path>`, not have its text carried in. Said with the path to use."""
    from core.orchestration.steps import CODE_TOOLS, tool_argument_refs

    if str(step.get("forced_tool") or "").split("__")[-1] not in CODE_TOOLS:
        return []
    code = (step.get("tool_arguments") or {}).get("code")
    if not isinstance(code, str):
        return []
    readers = {str(w.get("output_key")): w for w in steps
               if w.get("type") == "tool" and str(w.get("forced_tool") or "").split("__")[-1] in _FILE_READERS
               and w.get("output_key")}
    issues = []
    for ref in tool_argument_refs({"code": code}):
        reader = readers.get(ref.split(".")[0])
        if reader is None:
            continue
        path = str(((reader.get("tool_arguments") or {}).get("path")) or "").lstrip("/")
        where = f"'/data/{path}'" if path else "'/data/<its vault path>'"
        issues.append(
            f"Tool step '{step.get('id')}' runs code on a file's content carried in from 'state.{ref}' (step "
            f"'{reader.get('id')}' reads it). Code reads the vault where the sandbox mounts it: open {where} in the "
            f"code itself (e.g. pandas.read_csv({where})), and drop the read step if nothing else needs it."
        )
    return issues


def _validate_orchestration(orch: dict, agents: dict | None = None, kbs: dict | None = None,
                            knowledge_off: bool = False, allow_dead_ends: bool = False) -> dict:
    """Return {"ok": bool, "issues": [str, ...], "warnings": [str, ...]}: wiring
    problems, and what probably is not meant but runs (a prompt that reads a key
    before any step writes it, U74) — warnings never make `ok` false.

    Checks (matches the engine's expectations in core.orchestration.engine):
      - entry_step_id resolves to a real step
      - no duplicate step ids
      - every next_step_id / route_map target / loop_step_ids entry /
        parallel_branches element resolves to a real step (empty string = end)
      - every reachable path terminates at an `end`-type step (not asked of a
        workflow another one calls: `allow_dead_ends`, F429)
      - every step's input_keys are written by something: `user_input` /
        `user_query`, some step's `output_key`, a for-each loop's item or
        index key, or `human_response` where a human step exists (a key with
        no writer anywhere is meaningless — it can only ever be its schema
        default)
      - every state_schema entry declares a type we can read, and its default
        is of that type
      - a human step with fields writes a mapping, so its key is not declared
        as text or a number, and no Print shows that mapping whole (F189)
      - a retrieve or ingest step's settings hold to the contract, the
        knowledge bases it names exist (`kbs`, when given) and are active, an
        ingest step reads a key something writes or the schema declares, and
        neither is in an orchestration while Knowledge is off (R1)
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
        spent = s.get("on_exhausted_step_id")
        if spent and not _ref_ok(spent):
            issues.append(
                f"Step '{sid}' goes to '{spent}' when it reaches its limit (on_exhausted_step_id), "
                f"but no step '{spent}' exists."
            )
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
        elif stype == "agent" and agents is not None:
            # With the agents in hand (the tool passes them; the function stays
            # pure), the one behind the step is checked too (F264).
            target = agents.get(s.get("agent_id"))
            if target is None:
                issues.append(
                    f"Agent step '{sid}' uses agent '{s.get('agent_id')}', which does not exist — "
                    f"create it first, or use one list_agents returns."
                )
            elif target.get("type") == "orchestrator" and not target.get("orchestration_id"):
                issues.append(
                    f"Agent step '{sid}' uses orchestrator agent '{s.get('agent_id')}', which runs no "
                    f"orchestration — build that orchestration first, then deploy_orchestration it "
                    f"and use the agent id it returns."
                )
            # The tools it finishes with (E2) are tools it holds: a finish tool the agent cannot call never ends it.
            held = [t for t in target.get("tools") or [] if isinstance(t, str)] if target else []
            if target is not None and "all" not in held:
                who = target.get("name") or s.get("agent_id") if target else ""
                for name in s.get("finish_tools") or []:
                    if name not in held:
                        issues.append(
                            f"Agent step '{sid}' finishes with '{name}', which its agent '{who}' does not hold — "
                            "give the agent that tool, or finish with one it has."
                        )
        if stype == "evaluator" and not (s.get("route_map") or {}):
            issues.append(f"Evaluator step '{sid}' has an empty route_map.")
        if stype == "tool" and not s.get("forced_tool"):
            issues.append(f"Tool step '{sid}' has no forced_tool.")
        if stype == "workflow" and not s.get("workflow_id"):
            issues.append(f"Workflow step '{sid}' has no workflow_id: name the workflow it runs.")
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
        if stype in KNOWLEDGE_STEP_TYPES:
            issues.extend(_knowledge_step_issues(s, kbs, knowledge_off))
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
                        f"loop_max_iterations ({cap}), so the run fails before the loop starts — "
                        f"raise loop_max_iterations or lower loop_count."
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
    # conditioning is not the validator's concern — an unset read gets the
    # schema default, or reads empty in a prompt with none (U74), which is the
    # intended pattern for optional branch outputs and iterative refinement loops.
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
        # A Tool step's set arguments (E1) read state with no model to repair them: a key nothing writes would
        # fail every run ("could not set its arguments").
        if s.get("type") == "tool" and s.get("tool_arguments") is not None:
            if not isinstance(s["tool_arguments"], dict):
                issues.append(f"Tool step '{sid}' has tool_arguments that are not an object of argument → value.")
            else:
                from core.orchestration.steps import tool_argument_refs

                for ref in tool_argument_refs(s["tool_arguments"]):
                    key = ref.split(".")[0]
                    if key not in writable and key not in (orch.get("state_schema") or {}):
                        issues.append(
                            f"Tool step '{sid}' sets an argument from 'state.{ref}', but nothing writes '{key}' — "
                            f"give an earlier step '{key}' as its output_key, or declare it in the state schema."
                        )
                issues += _file_content_into_code(s, steps)
        # An Ingest step reads its records from one key. A key the schema
        # declares counts too: a run's caller can fill it, and a list default
        # is a way to seed records.
        if s.get("type") == "ingest" and isinstance(s.get("ingest"), dict):
            source = str(s["ingest"].get("records_from") or "").strip()
            source = source[len("state."):] if source.startswith("state.") else source
            if source and source not in writable and source not in (orch.get("state_schema") or {}):
                issues.append(
                    f"Ingest step '{sid}' reads its records from '{source}', which nothing writes — give the "
                    f"step that makes them '{source}' as its output_key, or declare it in the state schema."
                )
    # What a run makes (F249): a result_key no step writes is never the result.
    result_key = orch.get("result_key") or ""
    if result_key and result_key not in writable:
        issues.append(
            f"result_key '{result_key}' is written by no step — name the output_key of the step "
            f"whose output is this orchestration's result, or leave result_key empty for the last "
            f"step that produces content."
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
            successors = step_successors(s, by_id)
            if not successors:
                # A run whose last step finishes completes, End or no End: a called
                # workflow without one is not refused for it (F429, Naveen).
                if cur not in contained and not allow_dead_ends:
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
        # A dropdown with nothing to choose from cannot be answered (F455).
        from core.orchestration.human_fields import field_options, field_type
        for f in fields or []:
            if isinstance(f, dict) and field_type(f.get("type")) == "choice" and not field_options(f):
                issues.append(
                    f"Human step '{hid}' field '{f.get('name')}' is a dropdown (type choice) with no "
                    f'options: give it "options": ["…", "…"], one of which the person picks.'
                )
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

    # Parallel branches that write the same key: the engine fails the Parallel
    # before any branch runs (F256).
    from core.orchestration.branch_keys import same_key_branches, unset_reads
    issues.extend(same_key_branches(orch))

    # A prompt that reads a key before any step writes it reads it empty there
    # (U74): said, never refused — a feedback loop's first pass is a real pattern.
    return {"ok": not issues, "issues": issues, "warnings": unset_reads(orch)}


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
    # A Retrieve or Ingest step keeps its settings as one block, the contract's
    # own body (`step.retrieve` / `step.ingest`); the model writes them flat.
    # Folded into whatever block the step already has, so a patch of one field
    # changes only that field. A filter that is not JSON stays a string, for
    # `validate_orchestration` to name, rather than vanishing.
    for block, names in _KNOWLEDGE_FLAT_FIELDS.items():
        if f"{block}_json" in s:
            given = _parse_json_field(s.pop(f"{block}_json"), {})
            if isinstance(given, dict):
                s[block] = {**(s.get(block) or {}), **given}
        flat = {field: s.pop(name) for name, field in names.items() if name in s}
        if isinstance(flat.get("filters"), str):
            flat["filters"] = _parse_json_field(flat["filters"], flat["filters"]) if flat["filters"].strip() else None
        if flat:
            s[block] = {**(s.get(block) or {}), **flat}
    return s


#: The flat builder fields of a Retrieve or Ingest step → its settings block's field (R1).
_KNOWLEDGE_FLAT_FIELDS: dict[str, dict[str, str]] = {
    "retrieve": {"retrieve_kb_ids": "kb_ids", "retrieve_query": "query", "retrieve_filters_json": "filters",
                 "retrieve_top_k": "top_k", "retrieve_rerank": "rerank", "retrieve_snapshot": "snapshot"},
    "ingest": {"ingest_kb_id": "kb_id", "ingest_records_from": "records_from", "ingest_id_field": "id_field",
               "ingest_text_field": "text_field", "ingest_metadata_fields": "metadata_fields"},
}


def _run_cap_args(args: dict) -> dict:
    """The run caps `args` sets, as stored: `{field: number}`, None where 0 removes one.

    A field that is absent or null is left as it is. Raises ValueError, worded
    for the builder, for a value that is not a cap: a cap the engine would read
    as none must not be saved as if it were one (CAP1).
    """
    from core.orchestration.run_cap import clean

    out: dict = {}
    for key in RUN_CAP_PROPERTIES:
        value = args.get(key)
        if value is None:
            continue
        if not isinstance(value, bool) and value in (0, "0"):
            out[key] = None
            continue
        out[key] = clean(value, credits=key == "max_run_credits")
    return out


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
        s.setdefault("on_exhausted_step_id", None)
        # New deterministic step defaults
        s.setdefault("print_content", None)
        s.setdefault("if_condition", None)
        s.setdefault("if_true_step_id", None)
        s.setdefault("if_false_step_id", None)
        s.setdefault("switch_expression", None)
        s.setdefault("switch_cases", {})
        s.setdefault("switch_default_step_id", None)
        s.setdefault("include_full_history", None)
        # Only an agent step reads it, and the engine refuses it on any other:
        # a step written (or patched) as another type would save and then fail
        # to load at its first run (F313).
        if s.get("type") != "agent":
            s.pop("pass_images", None)
        result.append(s)
    return result


# The builder tools that live in their own modules register themselves on import (BWA1, D274). Last, so every name
# they import from here exists.
import importlib as _importlib  # noqa: E402

for _module in ("core.builder_connect", "core.builder_readers", "core.builder_help", "core.builder_guard",
                "core.builder_research", "core.builder_actions", "core.native_builder.change_tools",
                "core.native_builder.change_apply", "core.native_builder.build_plan",
                "core.native_builder.build_save", "core.native_builder.read_turn", "core.native_builder.classify",
                "core.native_builder.answer_pack", "core.native_builder.lookup", "core.native_builder.effort"):
    _importlib.import_module(_module)
