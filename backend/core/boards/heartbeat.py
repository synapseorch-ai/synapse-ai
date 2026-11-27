"""A heartbeat: a schedule that runs only while there is work on its boards (F1727).

A schedule with `run_when: has_work` checks at its time and runs only when a board its run would work on holds an
open task: one nobody holds, or one whose claim lapsed. An idle heartbeat is a query, not a run, so it costs no
model call. The boards are the ones its run reads (`core/boards/resolve.py`), so it never wakes for a card its
run cannot see:

* **An agent** has its schedule's own board (unless the agent's own board is off) and the shared boards naming the
  agent or the schedule;
* **a workflow**, or an Orchestrator agent that runs one, has the workflow's own board and the shared boards naming
  the workflow, as its steps do;
* of the shared boards, the oldest three as a turn has them, and only those it may `work` (claim): a board it may
  only add to is not its work.

A task added to one of them brings a woken heartbeat's next check to now (`wake_on_new_task`, on by default), so it
runs on the scheduler's next tick, not at its interval. The scheduler installs the wake at start (`install`); a
host that fires schedules its own way installs it there, and is told of the new time through the schedule store's
save observer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.boards.config import boards_enabled
from core.store import boards as store

logger = logging.getLogger(__name__)


def waits_for_work(schedule: dict[str, Any]) -> bool:
    return (schedule.get("run_when") or "always") == "has_work"


async def _scope_of(schedule: dict[str, Any]) -> tuple[str, str, list[tuple[str, str]], bool] | None:
    """(own board's scope, its id, the principals named on shared boards, whether the own board counts) for the
    run this schedule starts, or None when it starts none (its target is gone)."""
    from core.store.resources import get_agent

    target_id = str(schedule.get("target_id") or "")
    if schedule.get("target_type") != "agent":
        return "workflow", target_id, [("workflow", target_id)], True
    agent = await get_agent(target_id) if target_id else None
    if agent is not None and agent.get("type") == "orchestrator":
        orch_id = str(agent.get("orchestration_id") or "")
        return "workflow", orch_id, [("workflow", orch_id)], True
    own_on = ((agent or {}).get("boards") or {}).get("own", True) is not False
    principals = [("schedule", str(schedule["id"]))]
    if target_id:
        principals.insert(0, ("agent", target_id))
    return "schedule", str(schedule["id"]), principals, own_on


async def work_board_ids(schedule: dict[str, Any]) -> list[str]:
    """The ids of the boards this schedule's run would work on that exist now. Empty while Boards are off."""
    if not boards_enabled():
        return []
    scope = await _scope_of(schedule)
    if scope is None:
        return []
    kind, scope_id, principals, own_on = scope
    ids: list[str] = []
    if own_on and scope_id:
        own = await store.find_scoped_board(kind, scope_id)
        if own is not None:
            ids.append(own.id)
    shared = await store.shared_boards_for([p for p in principals if p[1]])
    ids += [board.id for board, access in shared[:store.SHARED_PER_PRINCIPAL] if access == "work"]
    return ids


async def has_work(schedule: dict[str, Any]) -> bool:
    """Whether a board this schedule's run works on holds an open task."""
    return await store.has_open_task(await work_board_ids(schedule))


async def to_wake(board: store.Board, card: store.Card, schedules: list[dict[str, Any]] | None = None, *,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    """The heartbeats `card` (a task just added to `board`) should wake: on, waiting for work, woken by a new task,
    working `board`, and not already due. It only reads: no schedule is moved and no job is armed, so a host that
    fires schedules its own way wakes each one as it does (`wake_for` is this engine's way). `schedules` are the
    tenant's, or read from the store when not given. A run's own task list and Synapse AI's board are no schedule's
    work."""
    from core.store import schedules as schedule_store

    if card.kind != "task" or board.scope in ("run", "assistant") or not boards_enabled():
        return []
    now = now or datetime.now(timezone.utc)
    out: list[dict[str, Any]] = []
    for schedule in (await schedule_store.load() if schedules is None else schedules):
        if not (schedule.get("enabled") and waits_for_work(schedule)
                and schedule.get("wake_on_new_task", True) is not False):
            continue
        if board.id not in await work_board_ids(schedule):
            continue
        due = schedule.get("next_run_at")
        if due and datetime.fromisoformat(due.replace("Z", "+00:00")) <= now:
            continue  # already due: the next tick runs it
        out.append(schedule)
    return out


async def wake_for(board: store.Board, card: store.Card) -> list[str]:
    """Bring forward each heartbeat `to_wake` names to now, telling the schedule store's save observer. The ids
    brought forward."""
    from core.store import schedules as schedule_store

    now = datetime.now(timezone.utc)
    woken: list[str] = []
    for schedule in await to_wake(board, card, now=now):
        await schedule_store.bring_forward(schedule, now)
        woken.append(schedule["id"])
    if woken:
        logger.info("[Heartbeat] a new task on board %s woke %s", board.id, ", ".join(woken))
    return woken


async def _on_task_added(board: store.Board, card: store.Card) -> None:
    # Read at call time, so a test (or a host) that replaces `wake_for` is the one called.
    await wake_for(board, card)


def install() -> None:
    """Wake heartbeats when a task is added: the scheduler calls this at start."""
    store.set_task_added_observer(_on_task_added)


def uninstall() -> None:
    store.set_task_added_observer(None)
