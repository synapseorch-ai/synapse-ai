"""A run as an API caller reads it: its state, its question, or its result.

The shape is v1's (F117): a run inside its first step is `running`, a paused run
names its question, an ended one carries `response`. MCP reads runs through this
too (D125), so both surfaces describe a run the same way.
"""
from __future__ import annotations


async def read_run(run_id: str) -> dict | None:
    """The run's public view, or None when there is no such run."""
    from core.orchestration.engine import OrchestrationEngine
    from core.orchestration.pauses import asking_step_id
    from core.orchestration.runner import _active_tasks
    from core.orchestration.state import SharedState
    from core.routes.orchestrations import _public_run_view

    try:
        run = (await SharedState.restore(run_id)).run
    except FileNotFoundError:
        # A run inside its first step has no checkpoint yet.
        task = _active_tasks.get(run_id)
        if task is not None and not task.done():
            return {"status": "running", "run_id": run_id}
        return None

    if run.status == "paused" and run.waiting_for_human:
        return {
            "status": "paused",
            "run_id": run_id,
            "human_input_required": {
                "step_id": asking_step_id(run.step_history, run.current_step_id),
                "prompt": run.human_prompt,
                "fields": run.human_fields or [],
            },
        }
    body = {
        "status": run.status,
        "run_id": run_id,
        "shared_state": _public_run_view(run)["shared_state"],
        "step_history": run.step_history,
    }
    if run.status in ("completed", "failed", "cancelled"):
        body["response"] = OrchestrationEngine._build_final_response(run)
    return body


async def run_orchestration_id(run_id: str) -> str | None:
    """The orchestration a run belongs to, from its checkpoint or its id.

    A run inside its first step has no checkpoint; its id is
    `run_{orchestration_id}_{ms}` (v1 and MCP both mint it that way).
    """
    from core.orchestration.state import SharedState

    try:
        return (await SharedState.restore(run_id)).run.orchestration_id
    except FileNotFoundError:
        if run_id.startswith("run_") and run_id.count("_") >= 2:
            return run_id[len("run_"):].rsplit("_", 1)[0]
        return None


#: The most runs one list call returns.
MAX_LIST = 200


async def list_runs(limit: int = 20, status: str | None = None,
                    orchestration_id: str | None = None) -> list[dict]:
    """Recent runs of this workspace's orchestrations, newest first.

    The app's run list's shape (`state_store.list_runs`). Runs of the AI
    builder's own orchestrations, and of orchestrations since deleted, are left
    out: neither is the workspace's to open. v1's run list and MCP's `list_runs`
    both read this (D125, D126), so the two cannot drift apart.

    `status` filters before the limit is applied, so a page of one status is
    full. The store already reads every row of the tenant to build its page, so
    asking it for all of them costs no more queries.
    """
    from core.builder_tools import _is_visible
    from core.orchestration.state import SharedState
    from core.routes.orchestrations import load_orchestrations

    limit = max(1, min(int(limit), MAX_LIST))
    ids = {o["id"] for o in await load_orchestrations() if _is_visible(o) and o.get("id")}
    if orchestration_id is not None:
        ids &= {orchestration_id}
    if not ids:
        return []
    if status is None:
        return await SharedState.list_runs(limit=limit, orchestration_ids=ids)
    runs = await SharedState.list_runs(limit=10**9, orchestration_ids=ids)
    return [r for r in runs if r.get("status") == status][:limit]
