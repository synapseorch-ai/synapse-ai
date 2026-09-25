"""Which builder tools Synapse serves over MCP, and how each one is marked (D125).

An **allowlist**, deliberately. A builder tool added later is not exposed to every
MCP client by accident: it is served only once someone has decided it belongs here.

Three kinds of builder tool are not served:

* **Deletes** (`delete_agent`, `delete_orchestration`, `delete_custom_tool`,
  `delete_schedule`). Their only guard is a `confirm=true` argument; in the app a
  person approves the plan first (the builder's manage review). Over MCP the
  client's model would be the only thing deciding, so deleting stays in the app.
* **`finish_build`** cleans up after the builder run it is called from, and an
  MCP call is not one — it would do nothing.
* **`list_variables`** answers with the first 40 characters of each value, and
  those are often tokens.

`list_recent_runs` and `get_run_summary` are left out because the run tools in
`runs.py` answer the same questions with more.

MCP clients treat a tool as destructive unless it says otherwise, so every tool
here says so explicitly. The overwriters are marked destructive — they replace
what is there — and clients such as Claude Code ask before calling them.
"""
from __future__ import annotations

from mcp import types

#: Tools that only read. `validate_orchestration` clears a build-in-progress mark
#: when an orchestration passes, which changes nothing a person can see.
READ_TOOLS: tuple[str, ...] = (
    "list_agents",
    "get_agent",
    "list_orchestrations",
    "get_orchestration",
    "validate_orchestration",
    "list_tool_servers",
    "list_server_tools",
    "list_all_tools",
    "get_tools_detail",
    "list_custom_tools",
    "list_skills",
    "get_skill",
    "list_schedules",
    "list_repos",
    "list_db_configs",
)

#: Tools that add or edit.
BUILD_TOOLS: tuple[str, ...] = (
    "create_agent",
    "create_agents",
    "update_agent",
    "create_orchestration",
    "create_orchestration_skeleton",
    "add_steps",
    "add_single_step",
    "add_multiple_steps",
    "update_step",
    "remove_step",
    "update_orchestration",
    "set_orchestration_meta",
    "create_http_tool",
    "import_openapi",
    "create_skill",
    "update_skill",
    "attach_skills",
    "create_schedule",
    "set_schedule_enabled",
)

#: Build tools that replace or remove what is already there: every step of an
#: orchestration, one step, or a tool or skill of the same name.
OVERWRITING_TOOLS: frozenset[str] = frozenset({
    "update_orchestration",
    "remove_step",
    "create_http_tool",
    "create_skill",
})

SERVED_BUILDER_TOOLS: frozenset[str] = frozenset(READ_TOOLS + BUILD_TOOLS)

#: What a caller must have been granted to see and call a tool (D127). An embedder
#: may grant less than everything — cloud's OAuth tokens carry these — while a key,
#: and every self-hosted caller, is granted all three. `run` spends model credits.
SCOPES: tuple[str, ...] = ("read", "run", "build")

#: The run tools that only read a run.
READING_RUN_TOOLS: frozenset[str] = frozenset({"get_run", "list_runs"})


def annotations_for(name: str) -> types.ToolAnnotations:
    if name in READ_TOOLS:
        return types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    return types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=name in OVERWRITING_TOOLS,
        openWorldHint=name == "import_openapi",
    )


def scope_of(name: str) -> str | None:
    """The scope a served tool needs; None for a name that is not served."""
    from core.mcp_server import runs, workflows

    if name in READ_TOOLS or name in READING_RUN_TOOLS:
        return "read"
    if name in BUILD_TOOLS:
        return "build"
    if name in runs.RUN_TOOL_NAMES or name.startswith(workflows.PREFIX):
        return "run"
    return None


def builder_tools() -> list[types.Tool]:
    """The served builder tools as MCP tools, in the allowlist's order."""
    from core.builder_tools import BUILDER_TOOL_SCHEMAS

    by_name = {s["function"]["name"]: s["function"] for s in BUILDER_TOOL_SCHEMAS}
    missing = [n for n in READ_TOOLS + BUILD_TOOLS if n not in by_name]
    if missing:
        # A renamed builder tool must fail loudly here, not vanish from MCP.
        raise RuntimeError(f"MCP catalog names builder tools that do not exist: {missing}")
    tools = []
    for name in READ_TOOLS + BUILD_TOOLS:
        fn = by_name[name]
        tools.append(types.Tool(
            name=name,
            description=fn.get("description", ""),
            inputSchema=fn.get("parameters") or {"type": "object", "properties": {}},
            annotations=annotations_for(name),
        ))
    return tools
