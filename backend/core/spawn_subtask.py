"""
spawn_subtask — native tool that allows any agent to spawn ephemeral sub-agents.

Not an MCP server. Imported directly by react_engine.py and routes/tools.py.
"""

SPAWN_SUBTASK_TOOL_NAME = "spawn_subtask"

# Static schema registered in /api/tools/available so the frontend can show
# it as a toggleable capability in the agent config UI.
SPAWN_SUBTASK_STATIC_SCHEMA = {
    "name": "spawn_subtask",
    "description": (
        "Spawn an ephemeral sub-agent to complete a focused task using only the tools "
        "you specify. Call this multiple times in one response to run sub-agents in "
        "parallel. Sub-agents can only use tools that you already have access to."
    ),
    "source": "native_engine",
    "source_label": "Sub-Agent",
    "type": "mcp_native",
    "schema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Clear task description for the sub-agent",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tools to give the sub-agent (subset of your available tools)",
            },
            "name": {
                "type": "string",
                "description": "Optional label for this subtask (shown in UI)",
            },
        },
        "required": ["task", "tools"],
    },
}


def build_dynamic_spawn_subtask_tool(available_tool_names: list[str]):
    """Build the runtime VirtualTool with the correct tools enum based on what
    the calling agent actually has available."""
    from core.tools import VirtualTool

    return VirtualTool(
        name="spawn_subtask",
        description=(
            "Spawn an ephemeral sub-agent to complete a focused task. "
            "Sub-agents run in parallel when you call this multiple times in one response.\n\n"
            "Available tools you can delegate: " + ", ".join(available_tool_names)
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Task for the sub-agent to complete",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": available_tool_names},
                    "description": "Tools to give the sub-agent (must be from your available tools)",
                },
                "name": {
                    "type": "string",
                    "description": "Optional label for this subtask",
                },
            },
            "required": ["task", "tools"],
        },
    )
