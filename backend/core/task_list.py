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


def merge_branch_tasks(current: list[dict], before: list[dict], after: list[dict]) -> list[dict]:
    """The run's list after one agent, running beside others, rewrote its own.

    The tool is whole-list-write, so an agent's new list replaced the run's.
    Agent steps used to take turns, each starting from the list the one before
    it left, so nothing was lost. Two branches of one Parallel now run at once
    (F255): each started from the same list, and whichever wrote last wiped the
    other's plan from the run. So the tasks this agent dropped from `before`
    (its previous list) are dropped, its own tasks take the place and status it
    gave them, and everything else in the run's list stays. Tasks are matched by
    their text, since there are no task ids.
    """
    def key(task: dict) -> str:
        return str(task.get("content") or "").strip()

    dropped = {key(t) for t in before} - {key(t) for t in after}
    mine = {key(t): t for t in after}
    merged, seen = [], set()
    for task in current:
        k = key(task)
        if k in dropped or k in seen:
            continue
        merged.append(dict(mine.get(k, task)))
        seen.add(k)
    merged += [dict(t) for t in after if key(t) not in seen]
    return merged[:MAX_TASKS]
