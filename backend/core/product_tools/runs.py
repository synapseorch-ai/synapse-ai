"""Runs, as product tools (MCP2, D292): start, read, answer, stop, list, and a turn with an agent.

The run tools the MCP server has served since D125, now entries with roles: reading a run is anyone's, answering
what it waits on an approver's (AP1, D260), starting or stopping one a builder's. Their code is still
`core.mcp_server.runs`: the waiting, the refusals and the `RunBackend` seam a host installs (cloud's fleet), which
cloud and the tests reach there. Each act's audit row is written by the host's own run doors that the backend calls
(cloud's `enqueue_run`, `answer_paused_run`, `cancel_loaded_run`, `start_turn`), so the registry writes none.
"""
from __future__ import annotations

from typing import Any

from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

#: name → (kind, least role, flags)
_SHAPE: dict[str, tuple[str, Role, dict[str, bool]]] = {
    "run_orchestration": ("act", Role.BUILDER, {"open_world": True}),
    "get_run": ("read", Role.VIEWER, {}),
    "answer_run": ("act", Role.APPROVER, {"open_world": True}),
    "cancel_run": ("act", Role.BUILDER, {"destructive": True}),
    "list_runs": ("read", Role.VIEWER, {}),
    "chat_with_agent": ("act", Role.BUILDER, {"open_world": True}),
    "answer_tool_approval": ("act", Role.APPROVER, {"open_world": True}),
}


def _handler(name: str):
    async def handle(caller: Any, args: dict[str, Any]) -> dict:
        from core.mcp_server import runs
        return await runs.call_run_tool(name, args)

    return handle


def entries() -> list[ProductTool]:
    from core.mcp_server import runs

    tools = []
    for tool in runs.RUN_TOOLS:
        kind, role, flags = _SHAPE[tool.name]
        tools.append(ProductTool(
            name=tool.name, description=tool.description or "", input_schema=dict(tool.inputSchema),
            handler=_handler(tool.name), kind=kind, area="runs", min_role=role,
            scope="read" if kind == "read" else "run", self_audited=kind == "act", **flags,
        ))
    return tools
