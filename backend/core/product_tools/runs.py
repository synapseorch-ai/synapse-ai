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


async def _resume(caller: Any, args: dict[str, Any]) -> dict:
    """Resume a failed or stopped run (F1260), through the run backend: cloud's replays with its credit check and
    audit row, as its run page does."""
    from core.mcp_server import runs
    from core.product_tools.errors import ToolRefusal

    run_id = str(args.get("run_id") or "").strip()
    if not run_id or not await runs.get_run_backend().served(run_id):
        raise ToolRefusal(f"Run '{run_id}' not found")
    resume = getattr(runs.get_run_backend(), "resume", None)
    if resume is None:
        raise ToolRefusal("A run is resumed from its page here: open it and press Replay.")
    return await resume(run_id)


async def _preview_resume(caller: Any, args: dict[str, Any]) -> dict:
    from core.orchestration import replay
    from core.orchestration.state import SharedState
    from core.product_tools.errors import ToolRefusal

    run_id = str(args.get("run_id") or "").strip()
    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        raise ToolRefusal(f"Run '{run_id}' not found") from None
    if (why := await replay.why_not(run)) is not None:
        raise ToolRefusal(why)
    step = next((e.get("step_name") for e in reversed(run.step_history or [])
                 if e.get("step_id") == run.current_step_id), None) or run.current_step_id
    return {"run_id": run_id, "from_step": step,
            "note": "It runs on from that step; what the steps before it did stays as it was."}


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
    # Synapse AI's resume card (F1260), in the app only: the run page's Replay, through the backend.
    tools.append(ProductTool(
        name="resume_run", description="Resume a failed or stopped run from the step it stopped at, as its page's "
                                       "Replay does.",
        input_schema={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
        handler=_resume, kind="act", area="runs", min_role=Role.BUILDER, scope="run", open_world=True,
        app_only=True, confirmed=True, preview=_preview_resume, self_audited=True,
    ))
    return tools
