"""A schedule's "Run now", as a product tool `trigger_schedule` (ASSIST1, D291, F683).

The Schedules screen's Run now: the schedule's target runs once, now, enabled or not, and its next time does not
move (F72). Turning a schedule on or off is the builder's (`build.py`): `turn_on_schedule` and `pause_schedule`, and
the app's own card's `set_schedule_enabled` (F1451). A builder's act, as
on the screen. Where the run starts is a host's: this process's schedule manager by default; cloud's fleet is its
own (`set_trigger`).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from core.product_tools.errors import ToolRefusal
from core.product_tools.registry import ProductTool
from core.product_tools.roles import Role

#: `trigger(schedule_id) -> run id`, or None when there is no such schedule; raises ToolRefusal with why it can't.
Trigger = Callable[[str], Awaitable["str | None"]]

_trigger: Trigger | None = None


def set_trigger(trigger: Trigger | None) -> None:
    global _trigger
    _trigger = trigger


async def _this_process(schedule_id: str) -> str | None:
    import core.server as server
    from core.scheduler import TargetGone

    if server.schedule_manager is None:
        raise ToolRefusal("Schedules are not running in this install right now.")
    try:
        return await server.schedule_manager.trigger_now(schedule_id)
    except TargetGone as gone:
        raise ToolRefusal(str(gone)) from None


async def _run_now(caller: Any, args: dict[str, Any]) -> dict:
    schedule_id = str(args.get("schedule_id") or "")
    run_id = await (_trigger or _this_process)(schedule_id)
    if run_id is None:
        raise ToolRefusal("There is no such schedule.")
    return {"status": "triggered", "schedule_id": schedule_id, "run_id": run_id}


def entries() -> list[ProductTool]:
    return [ProductTool(
        # Not `run_…`: that is the name of a workflow's own MCP tool (`mcp_server/workflows.PREFIX`).
        name="trigger_schedule",
        description="Run a schedule once, now, whether or not it is on. Its next scheduled time does not move.",
        input_schema={"type": "object", "properties": {"schedule_id": {"type": "string"}},
                      "required": ["schedule_id"]},
        handler=_run_now, kind="act", area="schedules", min_role=Role.BUILDER, scope="run", open_world=True,
        additive=True,
        audit_action="schedule.run_now", target=lambda args, result: ("schedule", str(args.get("schedule_id"))),
    )]
