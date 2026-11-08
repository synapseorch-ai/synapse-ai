"""
update_tasks — the native task list every agent carries.

Replaces the npx `@modelcontextprotocol/server-sequential-thinking` server,
which took a thought, echoed it back, and kept a list in its own process
memory: nothing reached the journal, nothing rendered in the UI, nothing
survived a resume. The task list is the opposite by construction — the
engine owns the state (it lives on the run and is checkpointed with it),
every change is journaled as a structural event, and the run log renders it
as a live checklist.

Not an MCP server. Imported directly by react_engine.py and routes/tools.py,
same as spawn_subtask.

The tool is whole-list-write: the model submits the complete list on every
call. One flat schema (no type unions — the exact shape that broke Gemini's
one-valued Schema.type on 2026-09-01), no task ids for the model to fumble,
and the engine's identical-args loop guard becomes effective — resubmitting
the same list verbatim is a no-op and gets blocked, which is the guard the
old server's ever-changing thoughtNumber defeated.
"""

import uuid

TASK_TOOL_NAME = "update_tasks"

# One release of grace: saved agents and shipped bundles that name the old
# tool keep working — the dispatcher routes the alias to the native handler.
LEGACY_TASK_TOOL_ALIAS = "sequentialthinking"

TASK_STATUSES = ("pending", "in_progress", "completed", "cancelled")
OPEN_STATUSES = ("pending", "in_progress")

MAX_TASKS = 50
MAX_CONTENT_CHARS = 300

_TASKS_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "description": (
                "The COMPLETE task list. Each call replaces the previous list, "
                "so include every task with its current status."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "What this task is, as a short imperative phrase",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(TASK_STATUSES),
                        "description": (
                            "pending = not started; in_progress = being worked on right now "
                            "(at most one at a time); completed = done; "
                            "cancelled = deliberately dropped, with no intent to do it"
                        ),
                    },
                },
                "required": ["content", "status"],
            },
        }
    },
    "required": ["tasks"],
}

_DESCRIPTION = (
    "Maintain your task list for this run — the working plan the user watches live. "
    "For multi-step work, write the list before acting, keep exactly one task "
    "in_progress, and update it the moment a task finishes. Every call must contain "
    "the FULL list: it replaces the previous one."
)

# Static schema registered in /api/tools/available so the frontend can show
# it as a capability in the agent config UI (it is injected for every agent).
TASK_LIST_STATIC_SCHEMA = {
    "name": TASK_TOOL_NAME,
    "description": _DESCRIPTION,
    "source": "native_engine",
    "source_label": "Task List",
    "type": "mcp_native",
    "schema": _TASKS_SCHEMA,
}


def build_task_tool():
    """The runtime VirtualTool declared to every agent."""
    from core.tools import VirtualTool

    return VirtualTool(
        name=TASK_TOOL_NAME,
        description=_DESCRIPTION,
        inputSchema=_TASKS_SCHEMA,
    )


def validate_tasks_args(args) -> tuple[list[dict] | None, str | None]:
    """Normalize a tool call's arguments into a task list, or explain the refusal.

    Returns (tasks, None) on success, (None, message) on rejection — the
    message is written for the model, so it says how to correct the call.
    """
    if not isinstance(args, dict) or "tasks" not in args:
        if isinstance(args, dict) and "thought" in args:
            # An old-schema sequentialthinking call from a stale saved prompt.
            return None, (
                f"{LEGACY_TASK_TOOL_ALIAS} has been replaced by {TASK_TOOL_NAME}. "
                'Submit your plan as {"tasks": [{"content": "...", "status": '
                '"pending|in_progress|completed|cancelled"}, ...]} — the full list, every call.'
            )
        return None, (
            f"{TASK_TOOL_NAME} requires a 'tasks' array — the complete list, "
            "which replaces the previous one."
        )

    tasks = args["tasks"]
    if not isinstance(tasks, list):
        return None, "'tasks' must be an array of {content, status} objects."
    if len(tasks) > MAX_TASKS:
        return None, f"Too many tasks ({len(tasks)}); keep the list under {MAX_TASKS}."

    normalized: list[dict] = []
    for i, task in enumerate(tasks):
        if isinstance(task, str):
            task = {"content": task}
        if not isinstance(task, dict):
            return None, f"Task {i + 1} must be an object with 'content' and 'status'."
        content = str(task.get("content") or "").strip()
        if not content:
            return None, f"Task {i + 1} has no content."
        status = str(task.get("status") or "pending").strip().lower()
        if status not in TASK_STATUSES:
            return None, (
                f"Task {i + 1} has invalid status '{status}'; "
                f"use one of: {', '.join(TASK_STATUSES)}."
            )
        normalized.append({"content": content[:MAX_CONTENT_CHARS], "status": status})
    return normalized, None


def open_tasks(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("status") in OPEN_STATUSES]


_GLYPHS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]", "cancelled": "[-]"}


def render_tasks(tasks: list[dict]) -> str:
    """Compact checklist for the model's context and tool-result previews."""
    if not tasks:
        return "(no tasks)"
    return "\n".join(
        f"{_GLYPHS.get(t.get('status', 'pending'), '[ ]')} {t.get('content', '')}"
        for t in tasks
    )


def summarize_tasks(tasks: list[dict]) -> str:
    counts = {status: 0 for status in TASK_STATUSES}
    for t in tasks:
        counts[t.get("status", "pending")] = counts.get(t.get("status", "pending"), 0) + 1
    parts = [f"{counts['completed']} done"]
    if counts["in_progress"]:
        parts.append(f"{counts['in_progress']} in progress")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    if counts["cancelled"]:
        parts.append(f"{counts['cancelled']} cancelled")
    return f"{len(tasks)} task{'s' if len(tasks) != 1 else ''} — " + ", ".join(parts)


#: The runs of a step going on now in this process, by (run, step), each by its own key (F1249). A step can run twice at
#: once (the same step in two branches of a Parallel), and each keeps its own list; any other entry of the step in the
#: run's list is an earlier run's, finished, and is replaced.
_LIVE: dict[tuple[str, str], set[str]] = {}


def _untagged(task: dict) -> dict:
    return {k: v for k, v in task.items() if k not in ("step_id", "run_of")}


def begin_step_tasks(run_id: str, step_id: str, run_tasks: list[dict] | None, *, resuming: bool
                     ) -> tuple[str, list[dict]]:
    """This run of a workflow's agent step: its key, and the list it starts with — its own, never the run's (F1249).

    Every step was seeded with the run's whole list, so the step after a scraper started holding the scraper's plan,
    and its end-of-step check ("Finish them and mark them completed") had it spend its turns on tasks that were not
    its own (Naveen's write_posts, 10-29: 2 of 3 turns). A run of a step that starts afresh starts with none, and that
    is every Loop iteration, retry and second visit as well: item 2 is a run of the step of its own, and item 1's
    finished list would put the same leftovers one level down (8a: each run of the step is its own). One that resumes
    where it stopped (a crash, a person's answer) goes on as the step's latest run, with the list it had. The run's
    entries carry their step and run (`step_id`, `run_of`); the model never sees them. `end_step_tasks` after."""
    mine = [t for t in run_tasks or [] if isinstance(t, dict) and t.get("step_id") == step_id]
    key = (mine[-1].get("run_of") if resuming and mine else None) or uuid.uuid4().hex[:12]
    _LIVE.setdefault((run_id, step_id), set()).add(key)
    return key, [_untagged(t) for t in mine if t.get("run_of") == key] if resuming else []


def end_step_tasks(run_id: str, step_id: str, key: str) -> None:
    """This run of the step is over: its entries are an earlier run's to the step's next."""
    live = _LIVE.get((run_id, step_id))
    if live is not None:
        live.discard(key)
        if not live:
            _LIVE.pop((run_id, step_id), None)


def with_step_tasks(run_tasks: list[dict] | None, run_id: str, step_id: str, key: str,
                    tasks: list[dict]) -> list[dict]:
    """The run's list after one run of a step rewrote its own (F1249): that run's entries, and the step's earlier
    finished runs', replaced where they stood, each tagged; every other step's, and another run of this step going on
    at the same time, kept as they are. So branches running at once never touch each other's (F255, F523), and the
    run's list stays the whole run's view (the run page, the API's `tasks`). A list written before steps were tagged
    is left in the view and is no step's."""
    live = _LIVE.get((run_id, step_id), set())
    mine = [{**_untagged(dict(t)), "step_id": step_id, "run_of": key} for t in (tasks or [])[:MAX_TASKS]]

    def replaced(t: dict) -> bool:
        return t.get("step_id") == step_id and (t.get("run_of") == key or t.get("run_of") not in live)

    out: list[dict] = []
    placed = False
    for t in run_tasks or []:
        if isinstance(t, dict) and replaced(t):
            if not placed:
                out += mine
                placed = True
            continue
        out.append(dict(t))
    return out if placed else out + mine
